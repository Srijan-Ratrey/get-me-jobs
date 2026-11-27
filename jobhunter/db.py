"""Database session management and upsert helpers."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from .config import Target, settings
from .models import (
    Application,
    Base,
    Company,
    Contact,
    Job,
    RawJob,
    Run,
    Suppression,
    hash_email,
    utcnow,
)

log = logging.getLogger(__name__)

_engine = None
_Session: sessionmaker[Session] | None = None


def init_db(db_url: str | None = None):
    global _engine, _Session
    _engine = create_engine(db_url or settings.db_url, future=True)
    Base.metadata.create_all(_engine)
    _add_missing_columns(_engine)
    _add_missing_indexes(_engine)
    _Session = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


# Columns added after the first release. `create_all` creates missing *tables*
# and silently ignores missing *columns*, so a database built before a column
# existed keeps working until the first query names it and SQLite raises
# "no such column".
#
# This is not a migration framework and should not grow into one. It exists
# because the alternative — asking someone to delete a database holding
# thousands of postings and their first-seen dates, which are not recoverable by
# rescanning — is a genuinely bad trade for a few nullable columns. If this list
# ever needs a data transformation rather than an ALTER, that is the signal to
# adopt Alembic instead.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("outreach", "sent_at", "DATETIME"),
    ("outreach", "error", "TEXT"),
    ("outreach", "gmail_message_id", "VARCHAR(255)"),
    ("outreach", "kind", "VARCHAR(20) DEFAULT 'application'"),
    ("runs", "kind", "VARCHAR(20) DEFAULT 'scan'"),
)

# Constraints declared on the model bind only to tables `create_all` creates. An
# `outreach` table that predates the constraint keeps its old shape, so the
# uniqueness that stops a person being mailed twice about one job would hold on
# a fresh database and silently not hold on the existing one. SQLite takes
# CREATE UNIQUE INDEX without rewriting the table, so it can be added in place.
# (table, index name, columns, unique)
_ADDED_INDEXES: tuple[tuple[str, str, str, bool], ...] = (
    ("outreach", "uq_outreach_job_contact", "(job_id, contact_id)", True),
    ("jobs", "ix_jobs_url", "(url)", False),
)


def _add_missing_columns(engine) -> None:
    """Add post-release columns to an existing database. Idempotent."""
    inspector = inspect(engine)
    for table, column, sql_type in _ADDED_COLUMNS:
        if table not in inspector.get_table_names():
            continue
        if column in {c["name"] for c in inspector.get_columns(table)}:
            continue
        log.info("adding column %s.%s to an existing database", table, column)
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"))


def _add_missing_indexes(engine) -> None:
    """Add post-release indexes to an existing database. Idempotent.

    A UNIQUE index fails to build if the table already holds rows that violate
    it. That is worth surfacing rather than swallowing: it means duplicate
    outreach exists and the caller needs to look, not that the upgrade failed.
    """
    inspector = inspect(engine)
    for table, name, definition, unique in _ADDED_INDEXES:
        if table not in inspector.get_table_names():
            continue
        if name in {i["name"] for i in inspector.get_indexes(table)}:
            continue
        log.info("adding index %s on %s to an existing database", name, table)
        with engine.begin() as conn:
            kind = "UNIQUE INDEX" if unique else "INDEX"
            conn.execute(text(f"CREATE {kind} IF NOT EXISTS {name} ON {table} {definition}"))


@contextmanager
def session_scope() -> Iterator[Session]:
    if _Session is None:
        init_db()
    assert _Session is not None
    s = _Session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def upsert_company(s: Session, target: Target) -> Company:
    company = s.scalar(select(Company).where(Company.name == target.name))
    if company is None:
        company = Company(name=target.name)
        s.add(company)
    # Only overwrite with non-null values so a partial YAML entry never wipes data.
    for field in ("domain", "ats", "ats_token", "careers_url"):
        value = getattr(target, field)
        if value:
            setattr(company, field, value)
    s.flush()
    return company


def upsert_job(s: Session, company: Company, raw: RawJob) -> tuple[Job, bool]:
    """Insert or refresh a job. Returns (job, is_new)."""
    content_hash = raw.compute_hash(company.name)
    job = s.scalar(select(Job).where(Job.content_hash == content_hash))
    if job is None:
        # The hash includes location, and two sources write one place two ways:
        # Zoho's "Bangalore North | Karnataka | India" is FreeHire's "Bangalore
        # North, India". Same company, URL and title is one posting. Not the URL
        # alone: a careers page listing several roles gives them all its own URL.
        # Exact URL match: some sites keep the job id in the query string.
        job = s.scalar(
            select(Job)
            .where(
                Job.company_id == company.id,
                Job.url == raw.url,
                Job.canonical_title == raw.canonical_title(),
            )
            .order_by(Job.id)
            .limit(1)
        )
    if job is not None:
        job.last_seen = utcnow()
        job.closed_at = None  # reappeared -> reopen
        # Backfill a description if this source has one and the stored row does not.
        if raw.description and not job.description:
            job.description = raw.description
        return job, False

    job = Job(
        company_id=company.id,
        source=raw.source,
        external_id=raw.external_id,
        title=raw.title,
        canonical_title=raw.canonical_title(),
        seniority=raw.detect_seniority(),
        location=raw.location,
        remote=raw.detect_remote(),
        description=raw.description,
        url=raw.url,
        posted_at=raw.posted_at,
        content_hash=content_hash,
    )
    s.add(job)
    s.flush()
    return job, True


def merge_duplicate_urls(s: Session) -> int:
    """Collapse one company's rows that share a URL and title. Returns how many went.

    For rows stored before upsert_job matched on URL. The survivor is the row
    an application points at, else a direct-board row over a FreeHire one, else
    one with a description, else the oldest. It keeps the earliest first_seen,
    the latest last_seen, any description, and is open if any copy was.
    """
    referenced = set(s.scalars(select(Application.job_id)))
    groups = s.execute(
        select(Job.company_id, Job.url, Job.canonical_title)
        .group_by(Job.company_id, Job.url, Job.canonical_title)
        .having(func.count() > 1)
    ).all()
    removed = 0
    for company_id, url, title in groups:
        rows = s.scalars(
            select(Job).where(
                Job.company_id == company_id, Job.url == url, Job.canonical_title == title
            )
        ).all()
        keep = min(
            rows,
            key=lambda j: (
                j.id not in referenced,
                (j.source or "").startswith("freehire"),
                not j.description,
                j.id,
            ),
        )
        for other in rows:
            if other is keep or other.id in referenced:
                continue
            keep.first_seen = min(keep.first_seen, other.first_seen)
            keep.last_seen = max(keep.last_seen, other.last_seen)
            keep.description = keep.description or other.description
            if other.closed_at is None:
                keep.closed_at = None
            s.delete(other)
            removed += 1
    s.flush()
    return removed


def close_stale_jobs(s: Session, company: Company, seen_hashes: set[str]) -> int:
    """Mark postings that were not in this run as closed.

    Only call this for a company whose fetch **succeeded**. With an empty
    ``seen_hashes`` every open job matches, so calling it after a failed fetch
    would report a whole board as filled because of one transient outage.

    FreeHire rows are left alone. A company's board says nothing about what
    the catalogue found for it on Naukri or on another of its boards, and
    closing those only for the next FreeHire run to reopen them made the final
    state depend on which fetch finished last. They close through
    ``close_unseen_jobs`` once FreeHire stops listing them.
    """
    stale = s.scalars(
        select(Job).where(
            Job.company_id == company.id,
            Job.closed_at.is_(None),
            Job.content_hash.notin_(seen_hashes or {""}),
            Job.source.notlike("freehire%"),
        )
    ).all()
    for job in stale:
        job.closed_at = utcnow()
    return len(stale)


def close_unseen_jobs(s: Session, runs_back: int = 2) -> int:
    """Close open jobs that no scan has seen since the Nth most recent finished run.

    ``close_stale_jobs`` only runs after a successful fetch, so a board that
    starts 404ing (company moved ATS) keeps its jobs open forever — and they go
    on feeding the shortlist and the sender. Counting runs rather than days
    keeps this independent of how often scans happen. Only call it from a scan
    that covered every target, or a filtered scan will close the rest.
    """
    cutoff = s.scalar(
        select(Run.started_at)
        .where(Run.kind == "scan", Run.finished_at.is_not(None))
        .order_by(Run.started_at.desc())
        .offset(runs_back - 1)
        .limit(1)
    )
    if cutoff is None:
        return 0
    stale = s.scalars(select(Job).where(Job.closed_at.is_(None), Job.last_seen < cutoff)).all()
    for job in stale:
        job.closed_at = utcnow()
    return len(stale)


def persistent_failures(s: Session, runs: int = 3) -> list[str]:
    """Targets that failed in every one of the last `runs` finished scans.

    One failure is noise; the same 404 on consecutive scans is a board that has
    moved, and run 15's check found three of ten had simply migrated ATS. Without
    this they burn a request each run and are never looked at.
    """
    recent = s.scalars(
        select(Run.errors)
        .where(Run.kind == "scan", Run.finished_at.is_not(None))
        .order_by(Run.started_at.desc())
        .limit(runs)
    ).all()
    if len(recent) < runs:
        return []
    failing = [{e.get("company") for e in errors or []} for errors in recent]
    return sorted(set.intersection(*failing) - {None})


def is_suppressed(s: Session, email: str) -> bool:
    """Has this address asked to be forgotten? Matched on hash, never plaintext."""
    return (
        s.scalar(select(Suppression).where(Suppression.email_hash == hash_email(email))) is not None
    )


def add_suppression(s: Session, email: str) -> Suppression:
    """Record an address as never-to-be-rediscovered. Idempotent."""
    digest = hash_email(email)
    existing = s.scalar(select(Suppression).where(Suppression.email_hash == digest))
    if existing is not None:
        return existing
    suppression = Suppression(email_hash=digest)
    s.add(suppression)
    s.flush()
    return suppression


def purge_contact(s: Session, email: str) -> int:
    """Erasure: hard-delete every row for an address and suppress rediscovery.

    Suppressing without deleting would defeat the request; deleting without
    suppressing would let the next scan find the address again. Both, or neither
    is worth doing.
    """
    add_suppression(s, email)
    rows = s.scalars(
        select(Contact).where(func.lower(Contact.email) == email.strip().lower())
    ).all()
    for row in rows:
        s.delete(row)
    s.flush()
    return len(rows)


def upsert_contact(
    s: Session,
    company: Company,
    *,
    email: str,
    discovery_method: str,
    source_url: str | None,
    kind: str = "role",
    confidence: float = 0.5,
    verify_status: str = "unknown",
    first_name: str | None = None,
    last_name: str | None = None,
    role_title: str | None = None,
    verified_at=None,
) -> Contact | None:
    """The single write path for contacts. Returns None if suppressed.

    ``source_url`` and ``discovery_method`` are keyword-only and validated here
    rather than left to each caller, because "every contact row records its
    provenance" is a GDPR obligation and an obligation that depends on remembering
    is one that eventually gets forgotten.
    """
    if not discovery_method:
        raise ValueError("discovery_method is required: provenance is not optional")
    if not source_url:
        raise ValueError(
            f"source_url is required for {email!r}: you must be able to say where "
            "personal data came from (docs/compliance.md)"
        )
    if is_suppressed(s, email):
        log.info("skipping suppressed address for %s", company.name)
        return None

    normalized = email.strip().lower()
    contact = s.scalar(
        select(Contact).where(Contact.company_id == company.id, Contact.email == normalized)
    )
    if contact is None:
        contact = Contact(company_id=company.id, email=normalized)
        s.add(contact)

    # Keep the better-evidenced version: a later, weaker sighting of the same
    # address should not downgrade a stronger one.
    if confidence >= (contact.confidence or 0.0):
        contact.kind = kind
        contact.confidence = confidence
        contact.discovery_method = discovery_method
        contact.source_url = source_url
    contact.first_name = first_name or contact.first_name
    contact.last_name = last_name or contact.last_name
    contact.role_title = role_title or contact.role_title
    if verify_status != "unknown":
        contact.verify_status = verify_status
        contact.verified_at = verified_at or utcnow()
    s.flush()
    return contact


def start_run(s: Session, kind: str = "scan") -> Run:
    run = Run(kind=kind, started_at=utcnow())
    s.add(run)
    s.flush()
    return run


def finish_run(
    s: Session,
    run: Run,
    *,
    jobs_seen: int = 0,
    jobs_new: int = 0,
    contacts_found: int = 0,
    errors: list | None = None,
) -> Run:
    run.finished_at = utcnow()
    run.jobs_seen = jobs_seen
    run.jobs_new = jobs_new
    run.contacts_found = contacts_found
    run.errors = errors or None
    s.flush()
    return run
