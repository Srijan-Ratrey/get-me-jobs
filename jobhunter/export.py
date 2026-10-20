"""CSV / XLSX export: one row per job with its best contact, highest score first."""

from __future__ import annotations

import csv
import logging
from datetime import datetime, timezone
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font
from sqlalchemy import select

from . import db
from .matching.scorer import matches_location
from .models import Company, Contact, Job
from .outreach.policy import best_contact

log = logging.getLogger(__name__)

COLUMNS = [
    "score",
    "company",
    "title",
    "seniority",
    "location",
    "remote",
    "url",
    "posted_at",
    "first_seen",
    "contact_email",
    "contact_kind",
    "contact_name",
    "contact_confidence",
    "contact_verify_status",
    "contact_source_url",
    "discovery_method",
    "why",
    "source",
]


def _why(job: Job) -> str:
    """Flatten fit_reasons into one readable cell."""
    reasons = (job.fit_reasons or {}).get("reasons") or []
    return " | ".join(reasons)


def collect_rows(
    *,
    min_score: int = 0,
    include_closed: bool = False,
    limit: int | None = None,
    since=None,
    posted_within=None,
    location: list[str] | None = None,
) -> list[dict]:
    """Build export rows. Kept separate from writing so it is testable.

    ``since`` filters on when *we* first saw a posting; ``posted_within`` on when
    the company published it. Both are useful and they are not the same question:
    a role posted in March that you only discovered today is new to you but stale
    to the market.
    """
    rows: list[dict] = []
    with db.session_scope() as session:
        query = (
            select(Job, Company)
            .join(Company, Job.company_id == Company.id)
            .order_by(Job.fit_score.desc().nullslast(), Job.first_seen.desc())
        )
        if not include_closed:
            query = query.where(Job.closed_at.is_(None))
        if min_score:
            query = query.where(Job.fit_score >= min_score)
        if since is not None:
            query = query.where(Job.first_seen >= since)
        if posted_within is not None:
            query = query.where(Job.posted_at >= posted_within)

        contacts: dict[int, Contact | None] = {}
        for job, company in session.execute(query).all():
            # Applied in Python, not SQL: matching "bengaluru" against "Bangalore"
            # needs the alias table, which a LIKE cannot express.
            if location and not matches_location(
                job.location, job.remote, location, job.description
            ):
                continue
            if company.id not in contacts:
                # The same choice outreach makes, so the sheet never lists an
                # address that would not actually be mailed.
                contacts[company.id] = best_contact(session, company.id)
            contact = contacts[company.id]
            name = " ".join(
                p
                for p in (
                    (contact.first_name if contact else None),
                    (contact.last_name if contact else None),
                )
                if p
            )
            rows.append(
                {
                    "score": job.fit_score,
                    "company": company.name,
                    "title": job.title,
                    "seniority": job.seniority,
                    "location": job.location,
                    "remote": job.remote,
                    "url": job.url,
                    "posted_at": job.posted_at,
                    "first_seen": job.first_seen,
                    "contact_email": contact.email if contact else None,
                    "contact_kind": contact.kind if contact else None,
                    "contact_name": name or None,
                    "contact_confidence": contact.confidence if contact else None,
                    "contact_verify_status": contact.verify_status if contact else None,
                    "contact_source_url": contact.source_url if contact else None,
                    "discovery_method": contact.discovery_method if contact else None,
                    "why": _why(job),
                    "source": job.source,
                }
            )
            # After the location filter, so --limit N with --location still gives N.
            if limit and len(rows) >= limit:
                break
    return rows


def _naive_utc(value):
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def export(
    path: str | Path,
    *,
    min_score: int = 0,
    include_closed: bool = False,
    since=None,
    posted_within=None,
    location: list[str] | None = None,
) -> int:
    """Write CSV or XLSX depending on the suffix. Returns the row count."""
    target = Path(path)
    suffix = target.suffix.lower()
    if suffix not in (".csv", ".xlsx", ".xlsm"):
        raise ValueError(f"unsupported export format {suffix!r}; use .csv or .xlsx")
    rows = collect_rows(
        min_score=min_score,
        include_closed=include_closed,
        since=since,
        posted_within=posted_within,
        location=location,
    )
    # Naive UTC throughout: Excel refuses timezone-aware datetimes, and the CSV
    # keeps the same "2026-10-15 05:53:48" text it always had.
    table = [[_naive_utc(row[c]) for c in COLUMNS] for row in rows]

    if suffix == ".csv":
        with target.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(COLUMNS)
            writer.writerows(table)
    else:
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "jobs"
        sheet.append(COLUMNS)
        for cell in sheet[1]:
            cell.font = Font(bold=True)
        for values in table:
            sheet.append(values)
        workbook.save(target)

    log.info("exported %d rows to %s", len(rows), target)
    return len(rows)


# Narrowest scope that works: drive.file grants access only to files this tool
# created, so it can neither read nor clobber anything else in the Drive.
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]
SHEET_MIME = "application/vnd.google-apps.spreadsheet"
_UPLOAD_MIME = {
    ".csv": "text/csv",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
}


def _drive_service():
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:  # pragma: no cover - exercised by the extra being absent
        raise RuntimeError(
            "Sheets upload needs the Google client library: uv sync --extra email"
        ) from exc

    from .config import settings
    from .google_auth import load_credentials

    creds = load_credentials(
        DRIVE_SCOPES,
        Path(settings.sheets_token_path),
        Path(settings.gmail_credentials_path),
        purpose="Sheets upload",
    )
    return build("drive", "v3", credentials=creds)


def upload_to_sheets(
    path: str | Path, *, title: str | None = None, service=None, media=None
) -> str:
    """Upload an exported file to Drive as a Google Sheet. Returns its URL.

    Overwrites the previous upload of the same name rather than making a new
    sheet each run, so a bookmarked link keeps working after a rescan. The
    lookup is safe under drive.file: it can only ever match our own uploads.

    `service` and `media` are injectable so the create-or-replace decision is
    testable without the Google client installed, the way GmailTransport is.
    """
    target = Path(path)
    mime = _UPLOAD_MIME.get(target.suffix.lower())
    if mime is None:
        raise ValueError(f"cannot upload {target.suffix!r} to Sheets; export .csv or .xlsx")
    if not target.is_file():
        raise FileNotFoundError(f"{target} does not exist; export it before uploading")

    service = service or _drive_service()
    name = title or target.stem
    if media is None:
        from googleapiclient.http import MediaFileUpload

        media = MediaFileUpload(str(target), mimetype=mime, resumable=True)

    escaped = name.replace("\\", "\\\\").replace("'", "\\'")
    found = (
        service.files()
        .list(q=f"name = '{escaped}' and trashed = false", fields="files(id)", pageSize=1)
        .execute()
        .get("files", [])
    )
    if found:
        file = (
            service.files()
            .update(fileId=found[0]["id"], media_body=media, fields="id, webViewLink")
            .execute()
        )
        log.info("replaced the Sheet at %s", file.get("webViewLink"))
    else:
        file = (
            service.files()
            .create(
                body={"name": name, "mimeType": SHEET_MIME},
                media_body=media,
                fields="id, webViewLink",
            )
            .execute()
        )
        log.info("created a Sheet at %s", file.get("webViewLink"))
    return str(file.get("webViewLink", ""))
