"""CSV / XLSX export: one row per job with its best contact, highest score first."""
from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import db
from .models import Company, Contact, Job

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


def _best_contact(session: Session, company_id: int) -> Contact | None:
    """Highest-confidence non-suppressed contact for a company."""
    return session.scalars(
        select(Contact)
        .where(Contact.company_id == company_id, Contact.suppressed.is_(False))
        .order_by(Contact.confidence.desc())
        .limit(1)
    ).first()


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
        if limit:
            query = query.limit(limit)

        contacts: dict[int, Contact | None] = {}
        for job, company in session.execute(query).all():
            if company.id not in contacts:
                contacts[company.id] = _best_contact(session, company.id)
            contact = contacts[company.id]
            name = " ".join(
                p for p in ((contact.first_name if contact else None), (contact.last_name if contact else None)) if p
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
    return rows


def export(
    path: str | Path,
    *,
    min_score: int = 0,
    include_closed: bool = False,
    since=None,
    posted_within=None,
) -> int:
    """Write CSV or XLSX depending on the suffix. Returns the row count."""
    import pandas as pd

    target = Path(path)
    rows = collect_rows(
        min_score=min_score,
        include_closed=include_closed,
        since=since,
        posted_within=posted_within,
    )
    frame = pd.DataFrame(rows, columns=COLUMNS)

    # Excel refuses timezone-aware datetimes; drop to naive UTC for the sheet.
    for column in ("posted_at", "first_seen"):
        if column in frame and not frame[column].isna().all():
            frame[column] = pd.to_datetime(frame[column], utc=True, errors="coerce").dt.tz_localize(
                None
            )

    suffix = target.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        frame.to_excel(target, index=False, sheet_name="jobs")
    elif suffix == ".csv":
        frame.to_csv(target, index=False)
    else:
        raise ValueError(f"unsupported export format {suffix!r}; use .csv or .xlsx")

    log.info("exported %d rows to %s", len(rows), target)
    return len(rows)
