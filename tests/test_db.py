"""Persistence: idempotent upserts, stale closing, and the erasure path."""
from __future__ import annotations

import pytest

from jobhunter import db
from jobhunter.config import Target
from jobhunter.models import Contact, Job, RawJob


@pytest.fixture
def session(tmp_path, monkeypatch):
    """A throwaway SQLite file per test."""
    db.init_db(f"sqlite+pysqlite:///{tmp_path / 'test.db'}")
    with db.session_scope() as s:
        yield s


def raw(title: str, location: str = "Bengaluru", **kw) -> RawJob:
    return RawJob(
        source=kw.pop("source", "greenhouse"),
        external_id=kw.pop("external_id", "1"),
        title=title,
        location=location,
        url=kw.pop("url", "https://x/1"),
        **kw,
    )


# --------------------------------------------------------------------------- #
# Companies and jobs
# --------------------------------------------------------------------------- #


def test_upsert_company_does_not_wipe_data_with_a_sparse_target(session):
    full = Target(name="Acme", domain="acme.com", ats="greenhouse", ats_token="acme")
    company = db.upsert_company(session, full)
    assert company.domain == "acme.com"

    # A later YAML entry with only a name must not blank out what we know.
    again = db.upsert_company(session, Target(name="Acme"))
    assert again.id == company.id
    assert again.domain == "acme.com"
    assert again.ats_token == "acme"


def test_upsert_job_is_idempotent(session):
    company = db.upsert_company(session, Target(name="Acme"))
    job, is_new = db.upsert_job(session, company, raw("Data Scientist"))
    assert is_new is True
    first_seen = job.first_seen

    same, is_new = db.upsert_job(session, company, raw("Data Scientist"))
    assert is_new is False
    assert same.id == job.id
    assert same.first_seen == first_seen
    assert session.query(Job).count() == 1


def test_reappearing_job_is_reopened_and_description_backfilled(session):
    company = db.upsert_company(session, Target(name="Acme"))
    job, _ = db.upsert_job(session, company, raw("Data Scientist"))
    db.close_stale_jobs(session, company, {"something-else"})
    assert job.closed_at is not None

    # A source that carries a description fills the gap left by one that did not.
    revived, is_new = db.upsert_job(
        session, company, raw("Data Scientist", source="ashby", description="Python and ML")
    )
    assert is_new is False
    assert revived.closed_at is None
    assert revived.description == "Python and ML"


def test_close_stale_jobs_marks_absent_postings(session):
    company = db.upsert_company(session, Target(name="Acme"))
    kept, _ = db.upsert_job(session, company, raw("Data Scientist"))
    gone, _ = db.upsert_job(session, company, raw("ML Engineer"))

    closed = db.close_stale_jobs(session, company, {kept.content_hash})
    assert closed == 1
    assert kept.closed_at is None
    assert gone.closed_at is not None
    # Rows are never deleted: a posting vanishing is itself signal.
    assert session.query(Job).count() == 2


def test_close_stale_jobs_with_empty_set_closes_everything(session):
    """Documents the hazard the scan pipeline must avoid.

    An empty seen-set means "nothing was found", which for this function means
    "close it all". That is correct only after a *successful* fetch, which is why
    pipeline.run_scan skips this call for any target that raised.
    """
    company = db.upsert_company(session, Target(name="Acme"))
    db.upsert_job(session, company, raw("Data Scientist"))
    db.upsert_job(session, company, raw("ML Engineer"))
    assert db.close_stale_jobs(session, company, set()) == 2


def test_close_stale_jobs_is_scoped_to_one_company(session):
    acme = db.upsert_company(session, Target(name="Acme"))
    globex = db.upsert_company(session, Target(name="Globex"))
    db.upsert_job(session, acme, raw("Data Scientist"))
    other, _ = db.upsert_job(session, globex, raw("Data Scientist"))

    db.close_stale_jobs(session, acme, set())
    assert other.closed_at is None


# --------------------------------------------------------------------------- #
# Contacts, provenance, suppression
# --------------------------------------------------------------------------- #


def test_upsert_contact_requires_provenance(session):
    company = db.upsert_company(session, Target(name="Acme"))
    with pytest.raises(ValueError, match="source_url is required"):
        db.upsert_contact(
            session, company, email="careers@acme.com", discovery_method="scraped", source_url=None
        )
    with pytest.raises(ValueError, match="discovery_method is required"):
        db.upsert_contact(
            session,
            company,
            email="careers@acme.com",
            discovery_method="",
            source_url="https://acme.com/careers",
        )
    assert session.query(Contact).count() == 0


def test_upsert_contact_normalizes_and_dedupes(session):
    company = db.upsert_company(session, Target(name="Acme"))
    first = db.upsert_contact(
        session,
        company,
        email="Careers@Acme.com",
        discovery_method="scraped:text",
        source_url="https://acme.com/careers",
        confidence=0.95,
    )
    assert first is not None and first.email == "careers@acme.com"

    again = db.upsert_contact(
        session,
        company,
        email="careers@acme.com",
        discovery_method="scraped:mailto",
        source_url="https://acme.com/jobs",
        confidence=0.95,
    )
    assert again.id == first.id
    assert session.query(Contact).count() == 1


def test_weaker_sighting_does_not_downgrade_a_stronger_one(session):
    company = db.upsert_company(session, Target(name="Acme"))
    db.upsert_contact(
        session,
        company,
        email="careers@acme.com",
        discovery_method="scraped:mailto",
        source_url="https://acme.com/careers",
        confidence=0.95,
    )
    db.upsert_contact(
        session,
        company,
        email="careers@acme.com",
        discovery_method="pattern:first.last",
        source_url="https://acme.com/team",
        confidence=0.14,
    )
    contact = session.query(Contact).one()
    assert contact.confidence == 0.95
    assert contact.discovery_method == "scraped:mailto"


def test_purge_deletes_and_suppresses_rediscovery(session):
    company = db.upsert_company(session, Target(name="Acme"))
    db.upsert_contact(
        session,
        company,
        email="anna@acme.com",
        discovery_method="scraped:text",
        source_url="https://acme.com/team",
        confidence=0.6,
    )
    assert db.purge_contact(session, "anna@acme.com") == 1
    assert session.query(Contact).count() == 0
    assert db.is_suppressed(session, "anna@acme.com") is True

    # A later run must not bring the address back.
    assert (
        db.upsert_contact(
            session,
            company,
            email="ANNA@acme.com",
            discovery_method="scraped:text",
            source_url="https://acme.com/team",
            confidence=0.9,
        )
        is None
    )
    assert session.query(Contact).count() == 0


def test_suppression_stores_a_hash_not_the_address(session):
    from jobhunter.models import Suppression

    db.add_suppression(session, "secret@acme.com")
    stored = session.query(Suppression).one()
    assert "secret" not in stored.email_hash
    assert "acme" not in stored.email_hash
    assert len(stored.email_hash) == 64


def test_add_suppression_is_idempotent(session):
    from jobhunter.models import Suppression

    db.add_suppression(session, "x@acme.com")
    db.add_suppression(session, "X@Acme.com  ")
    assert session.query(Suppression).count() == 1


# --------------------------------------------------------------------------- #
# Runs
# --------------------------------------------------------------------------- #


def test_run_lifecycle(session):
    run = db.start_run(session)
    assert run.finished_at is None
    db.finish_run(session, run, jobs_seen=10, jobs_new=3, errors=[{"company": "X"}])
    assert run.finished_at is not None
    assert (run.jobs_seen, run.jobs_new) == (10, 3)
    assert run.errors == [{"company": "X"}]


# --------------------------------------------------------------------------- #
# --since parsing
# --------------------------------------------------------------------------- #


def test_resolve_since_durations_and_dates(session):
    from datetime import datetime, timedelta, timezone

    from jobhunter.pipeline import resolve_since

    assert resolve_since(None) is None

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    week = resolve_since("7d")
    assert week is not None and abs((now - week) - timedelta(days=7)).total_seconds() < 5
    assert abs((now - resolve_since("24h")) - timedelta(hours=24)).total_seconds() < 5
    assert abs((now - resolve_since("2w")) - timedelta(weeks=2)).total_seconds() < 5

    # SQLite stores these columns naive, so the cutoff must be naive too or the
    # comparison silently matches nothing.
    for value in ("7d", "2026-07-30", "2026-07-30T12:00:00+05:30"):
        assert resolve_since(value).tzinfo is None


def test_resolve_since_last_scan_uses_the_latest_run(session):
    from jobhunter.pipeline import resolve_since

    db.start_run(session)
    run = db.start_run(session)
    session.commit()

    cutoff = resolve_since("last-scan")
    # Naive, because that is how SQLite handed it back — and a naive cutoff is
    # what the column can actually be compared against.
    assert cutoff is not None and cutoff.tzinfo is None
    assert cutoff == run.started_at.replace(tzinfo=None)


def test_resolve_since_rejects_nonsense(session):
    from jobhunter.pipeline import resolve_since

    with pytest.raises(ValueError, match="cannot parse"):
        resolve_since("whenever")


def test_since_filter_selects_only_newer_jobs(session):
    from datetime import datetime, timedelta, timezone

    from jobhunter.export import collect_rows

    company = db.upsert_company(session, Target(name="Acme"))
    old, _ = db.upsert_job(session, company, raw("Old Role"))
    new, _ = db.upsert_job(session, company, raw("New Role"))
    old.first_seen = datetime(2020, 1, 1)
    session.commit()

    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
    titles = {r["title"] for r in collect_rows(since=cutoff)}
    assert titles == {"New Role"}
    assert len(collect_rows()) == 2
