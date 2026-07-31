"""Database session management and upsert helpers."""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from .config import Target, settings
from .models import (
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
    _Session = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


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


def close_stale_jobs(s: Session, company: Company, seen_hashes: set[str]) -> int:
    """Mark postings that were not in this run as closed.

    Only call this for a company whose fetch **succeeded**. With an empty
    ``seen_hashes`` every open job matches, so calling it after a failed fetch
    would report a whole board as filled because of one transient outage.
    """
    stale = s.scalars(
        select(Job).where(
            Job.company_id == company.id,
            Job.closed_at.is_(None),
            Job.content_hash.notin_(seen_hashes or {""}),
        )
    ).all()
    for job in stale:
        job.closed_at = utcnow()
    return len(stale)


def is_suppressed(s: Session, email: str) -> bool:
    """Has this address asked to be forgotten? Matched on hash, never plaintext."""
    return (
        s.scalar(select(Suppression).where(Suppression.email_hash == hash_email(email)))
        is not None
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


def start_run(s: Session) -> Run:
    run = Run(started_at=utcnow())
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
