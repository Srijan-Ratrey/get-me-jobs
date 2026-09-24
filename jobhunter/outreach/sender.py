"""Talk to Gmail. Knows nothing about who deserves an email.

The transport is a Protocol with the real Gmail client behind it, injected
rather than constructed inside the send loop. That is what lets the whole
sending path be tested against a fake while `conftest.py`'s socket block stays
armed — a test that can accidentally reach the network is a test that will
eventually mail a stranger.

Two transports, and they are not equally contained. `GmailTransport` asks for
`gmail.send` and nothing else, so that process cannot read the mailbox and the
blast radius of a bug in it is small and bounded. `SmtpTransport` uses an app
password, which is not scopeable: the same credential opens IMAP, so it can
read and delete mail too. The app password buys a much shorter setup and costs
that containment. Setting one selects it; see `build_transport`.

Either way, reply detection is not here — it needs a read path of its own and
belongs to the follow-up tracker.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import random
import smtplib
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr
from pathlib import Path
from typing import Protocol

from sqlalchemy.orm import Session

from ..config import Profile, settings
from ..google_auth import load_credentials
from ..models import Outreach, utcnow
from . import policy
from .drafter import Draft, Refusal, draft_for, draft_speculative, preflight

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
    speculative: int = 0
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
            from googleapiclient.discovery import build
        except ImportError as exc:  # pragma: no cover - exercised by the extra being absent
            raise RuntimeError(
                "Gmail support needs the 'email' extra: uv sync --extra email"
            ) from exc

        creds = load_credentials(
            GMAIL_SCOPES,
            Path(settings.gmail_token_path),
            Path(settings.gmail_credentials_path),
            purpose="Gmail send",
        )
        return build("gmail", "v1", credentials=creds)

    def send(self, message: EmailMessage) -> str:
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        sent = self._service.users().messages().send(userId="me", body={"raw": raw}).execute()
        return str(sent.get("id", ""))


class SmtpTransport:
    """Gmail over SMTP with an app password. Stdlib only, no GCP project.

    Cheaper to set up than the OAuth client, and the tradeoff is worth naming
    rather than burying: an app password is *not* send-only. The same sixteen
    characters authenticate to IMAP, so a leaked `.env` gives up read access to
    the mailbox, which `gmail.send` never does. Prefer `GmailTransport` where
    the setup cost is acceptable.

    `connect` is injectable so the send path is testable with the suite's
    socket block armed.
    """

    def __init__(self, *, password=None, host=None, port=None, connect=None) -> None:
        secret = password if password is not None else settings.gmail_app_password
        if secret is None:
            raise RuntimeError(
                "no app password set. Put APP_PASSWORD in .env, or set up the OAuth "
                "client instead — see README."
            )
        raw = secret.get_secret_value() if hasattr(secret, "get_secret_value") else str(secret)
        # Google shows app passwords in four space-separated groups, and that is
        # how they get pasted. Stripping here beats an auth error nobody can read.
        self._password = raw.replace(" ", "")
        self._host = host or settings.gmail_smtp_host
        self._port = port or settings.gmail_smtp_port
        self._connect = connect or (lambda: smtplib.SMTP_SSL(self._host, self._port))

    def send(self, message: EmailMessage) -> str:
        # SMTP hands back no id of its own, so set one before sending and record
        # that. Without it the outreach row has no handle on what went out.
        if not message["Message-ID"]:
            message["Message-ID"] = make_msgid()
        account = parseaddr(message["From"])[1]
        with self._connect() as server:
            server.login(account, self._password)
            refused = server.send_message(message)
        if refused:
            raise RuntimeError(f"SMTP refused a recipient: {sorted(refused)}")
        return str(message["Message-ID"])


def build_transport() -> MailTransport:
    """Whichever transport the configuration selects.

    Setting an app password *is* the choice, so there is no separate mode flag
    that could contradict it.
    """
    if settings.gmail_app_password is not None:
        log.info("sending over SMTP as %s", settings.gmail_smtp_host)
        return SmtpTransport()
    log.info("sending over the Gmail API with a send-only OAuth token")
    return GmailTransport()


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
    speculative: bool = True,
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

    # Real openings fill the budget first; speculative takes only what is left.
    # That is what "one shared budget" has to mean in practice -- a speculative
    # note must never displace an application to an actual advertised role, and
    # speculative candidates carry no fit_score to interleave on anyway. A day
    # with fifteen matching openings sends no speculative mail at all.
    if speculative and len(batch) < budget:
        batch = batch + policy.speculative_candidates(
            s,
            profile,
            min_score=threshold,
            limit=budget - len(batch),
            exclude_companies={c.company.id for c in batch},
        )

    consecutive_failures = 0

    for candidate in batch:
        if candidate.kind == "speculative":
            result = draft_speculative(
                candidate.contact, candidate.company, candidate.evidence, profile
            )
        else:
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
            kind=candidate.kind,
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
            report.speculative += int(candidate.kind == "speculative")
            consecutive_failures = 0
            log.info("sent to %s about %s", candidate.contact.email, candidate.job.title)
        except Exception as exc:  # noqa: BLE001 - one bad send must not abort the rest
            row.status = "failed"
            row.error = f"{type(exc).__name__}: {exc}"
            report.failed += 1
            report.reasons.append(f"{candidate.company.name}: send failed: {exc}")
            consecutive_failures += 1
            if consecutive_failures >= settings.send_failure_circuit_breaker:
                report.reasons.append(f"stopping: {consecutive_failures} sends failed in a row")
                break
        s.commit()

        if pause and candidate is not batch[-1]:
            _pause()

    return report
