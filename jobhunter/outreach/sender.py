"""Talk to Gmail. Knows nothing about who deserves an email.

The transport is a Protocol with the real Gmail client behind it, injected
rather than constructed inside the send loop. That is what lets the whole
sending path be tested against a fake while `conftest.py`'s socket block stays
armed — a test that can accidentally reach the network is a test that will
eventually mail a stranger.

Scope is `gmail.send` and nothing else. This process cannot read the mailbox,
which makes the blast radius of a bug in it small and bounded. Reply detection
needs a read scope and belongs to the follow-up tracker, not here.
"""
from __future__ import annotations

import base64
import logging
import mimetypes
import random
import time
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Protocol

from sqlalchemy.orm import Session

from ..config import Profile, settings
from ..models import Outreach, utcnow
from . import policy
from .drafter import Draft, Refusal, draft_for, preflight

log = logging.getLogger(__name__)

# Send-only. Deliberately not gmail.modify or gmail.readonly.
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.send"]


class MailTransport(Protocol):
    """Anything that can put one message on the wire and return its id."""

    def send(self, message: EmailMessage) -> str: ...


@dataclass
class SendReport:
    drafted: int = 0
    sent: int = 0
    skipped: int = 0
    failed: int = 0
    reasons: list[str] | None = None

    def __post_init__(self) -> None:
        if self.reasons is None:
            self.reasons = []


def build_message(*, to: str, subject: str, body: str, profile: Profile) -> EmailMessage:
    """A real multipart message with the CV attached.

    Raises rather than sending CV-less: an application email without one is
    close to useless, and silently dropping the attachment would make every
    message in the batch quietly worse in a way nobody would notice for days.
    """
    applicant = profile.applicant
    message = EmailMessage()
    message["To"] = to
    message["From"] = f"{applicant.name} <{applicant.email}>"
    message["Subject"] = subject
    message.set_content(body)

    resume = Path(applicant.resume_path).expanduser()
    if not resume.is_file():
        raise FileNotFoundError(f"resume not readable at {resume}")
    guessed, _ = mimetypes.guess_type(resume.name)
    maintype, _, subtype = (guessed or "application/octet-stream").partition("/")
    message.add_attachment(
        resume.read_bytes(), maintype=maintype, subtype=subtype, filename=resume.name
    )
    return message


class GmailTransport:
    """The real thing. Imported lazily so the core installs without the extra."""

    def __init__(self, service=None) -> None:
        self._service = service or self._build_service()

    @staticmethod
    def _build_service():
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
            from googleapiclient.discovery import build
        except ImportError as exc:  # pragma: no cover - exercised by the extra being absent
            raise RuntimeError(
                "Gmail support needs the 'email' extra: uv sync --extra email"
            ) from exc

        token_path = Path(settings.gmail_token_path)
        creds = None
        if token_path.is_file():
            creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        if not creds or not creds.valid:
            credentials_path = Path(settings.gmail_credentials_path)
            if not credentials_path.is_file():
                raise RuntimeError(
                    f"no Gmail credentials at {credentials_path}. Create an OAuth client in "
                    "Google Cloud, download it, and see README for the setup steps."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
            token_path.write_text(creds.to_json())
        return build("gmail", "v1", credentials=creds)

    def send(self, message: EmailMessage) -> str:
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        sent = self._service.users().messages().send(userId="me", body={"raw": raw}).execute()
        return str(sent.get("id", ""))


def _pause() -> None:
    """Jittered gap between sends, so fifteen messages are not a burst."""
    low, high = settings.send_spacing_min_seconds, settings.send_spacing_max_seconds
    time.sleep(random.uniform(min(low, high), max(low, high)))


def send_batch(
    s: Session,
    *,
    profile: Profile,
    transport: MailTransport | None = None,
    limit: int | None = None,
    min_score: int | None = None,
    dry_run: bool = False,
    pause: bool = True,
) -> SendReport:
    """Draft and send up to the remaining daily budget.

    Returns a report rather than printing: `cli.py` is the only module allowed
    to speak.
    """
    report = SendReport()
    budget = policy.remaining_today(s)
    if limit is not None:
        budget = min(budget, limit)
    if budget <= 0:
        report.reasons.append(
            f"daily cap reached: {policy.sent_recently(s)} sent in the last 24h, "
            f"cap is {policy.daily_cap()}"
        )
        return report

    threshold = profile.min_score if min_score is None else min_score
    batch = policy.candidates(s, min_score=threshold, limit=budget)
    consecutive_failures = 0

    for candidate in batch:
        result = draft_for(candidate.job, candidate.contact, candidate.company, profile)
        if isinstance(result, Refusal):
            report.skipped += 1
            report.reasons.append(f"{candidate.company.name}: {result.reason}")
            continue
        assert isinstance(result, Draft)
        report.drafted += 1

        # Re-check immediately before the wire: the batch was chosen up to forty
        # minutes ago and both the cap and the job's state can have moved.
        decision = policy.may_send(s, candidate)
        if not decision:
            report.skipped += 1
            report.reasons.append(f"{candidate.company.name}: {decision.reason}")
            continue

        problem = preflight(result.subject, result.body)
        if problem:
            report.skipped += 1
            report.reasons.append(f"{candidate.company.name}: {problem}")
            continue

        row = Outreach(
            job_id=candidate.job.id,
            contact_id=candidate.contact.id,
            subject=result.subject,
            body=result.body,
            status="draft",
        )

        if dry_run:
            report.skipped += 1
            report.reasons.append(f"{candidate.company.name}: dry run, not sent")
            continue

        if transport is None:
            raise RuntimeError("no mail transport configured")

        s.add(row)
        s.flush()
        try:
            message = build_message(
                to=candidate.contact.email,
                subject=result.subject,
                body=result.body,
                profile=profile,
            )
            row.gmail_message_id = transport.send(message)
            row.status = "sent"
            row.sent_at = utcnow()
            report.sent += 1
            consecutive_failures = 0
            log.info("sent to %s about %s", candidate.contact.email, candidate.job.title)
        except Exception as exc:  # one bad send must not abort the rest
            row.status = "failed"
            row.error = f"{type(exc).__name__}: {exc}"
            report.failed += 1
            report.reasons.append(f"{candidate.company.name}: send failed: {exc}")
            consecutive_failures += 1
            if consecutive_failures >= settings.send_failure_circuit_breaker:
                report.reasons.append(
                    f"stopping: {consecutive_failures} sends failed in a row"
                )
                break
        s.commit()

        if pause and candidate is not batch[-1]:
            _pause()

    return report
