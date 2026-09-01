"""The FreeHire catalogue source.

Offline like everything else. The fixture is a real response captured on
2026-09-01 from `category=ml_ai&countries=in&posted_within_days=21`.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import respx

from jobhunter.config import Target
from jobhunter.http import PoliteClient, SourceUnavailable
from jobhunter.sources.freehire import FreeHireSource, _clean_url, _posted_at

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "freehire.json").read_text())
SEARCH = "https://freehire.me/api/v1/jobs/search"


@pytest.fixture
def client(tmp_path):
    return PoliteClient(cache_dir=tmp_path / "cache", cache_ttl=0, requests_per_second=1000)


def allow_robots() -> None:
    respx.get("https://freehire.me/robots.txt").respond(404)


def target(**search) -> Target:
    return Target(name="FreeHire (India ML)", ats="freehire", search=search or {"category": "ml_ai"})


def page(rows, *, total=None, ignored=None) -> dict:
    meta = {"limit": 100, "offset": 0, "total": total if total is not None else len(rows)}
    if ignored is not None:
        meta["ignored_params"] = ignored
    return {"data": rows, "meta": meta}


# --------------------------------------------------------------------------- #
# Field mapping
# --------------------------------------------------------------------------- #


@respx.mock
async def test_maps_the_real_fixture(client):
    allow_robots()
    respx.get(url__startswith=SEARCH).respond(200, json=FIXTURE)
    async with client as c:
        jobs = await FreeHireSource().fetch(c, target())

    assert jobs, "the fixture has rows"
    job = jobs[0]
    assert job.title and job.company_name and job.url.startswith("http")
    assert job.posted_at is not None
    assert job.description and "<" not in job.description, "HTML must be flattened to text"


@respx.mock
async def test_provenance_names_the_originating_ats_not_freehire(client):
    """The export should say where the posting actually lives."""
    allow_robots()
    respx.get(url__startswith=SEARCH).respond(
        200, json=page([{"title": "ML Engineer", "company": "Acme", "source": "workday",
                         "url": "https://acme.wd1.myworkdayjobs.com/j/1", "external_id": "a:1"}])
    )
    async with client as c:
        jobs = await FreeHireSource().fetch(c, target())
    assert jobs[0].source == "freehire:workday"


def test_the_attribution_parameter_is_stripped_from_the_url():
    """The user must land on the employer's posting, not a tracked redirect."""
    assert _clean_url("https://job-boards.greenhouse.io/x/jobs/5?utm_source=freehire.me") == (
        "https://job-boards.greenhouse.io/x/jobs/5"
    )
    assert _clean_url("https://x.com/j/5?gh_jid=5&utm_source=freehire.me") == (
        "https://x.com/j/5?gh_jid=5"
    )
    assert _clean_url("https://x.com/j/5") == "https://x.com/j/5"
    assert _clean_url(None) is None


def test_a_refreshed_repost_uses_its_first_sighting_not_the_refreshed_date():
    """FreeHire re-stamps posted_at when a company reposts and flags it.

    Trusting the new date would make a months-old listing look like it appeared
    today, which silently defeats --posted-within.
    """
    row = {
        "posted_at": "2026-09-01T10:00:00Z",
        "created_at": "2026-05-02T04:00:00Z",
        "reality": {"fake_freshness": True},
    }
    assert _posted_at(row).month == 5

    honest = {**row, "reality": {"fake_freshness": False}}
    assert _posted_at(honest).month == 9


def test_posted_at_falls_back_to_created_at_when_absent():
    assert _posted_at({"created_at": "2026-05-02T04:00:00Z"}).month == 5
    assert _posted_at({}) is None


# --------------------------------------------------------------------------- #
# The silent-filter trap
# --------------------------------------------------------------------------- #


@respx.mock
async def test_an_ignored_filter_is_reported_loudly(client, caplog):
    """The API drops unknown filters instead of refusing them.

    Live proof: `categories=ml_ai` returned 22,233 rows (the whole India
    catalogue) while `category=ml_ai` returned 202. Both look like success, so
    silence here would mean scanning the wrong thing entirely.
    """
    allow_robots()
    respx.get(url__startswith=SEARCH).respond(
        200, json=page([], ignored=[{"param": "categories"}])
    )
    async with client as c:
        await FreeHireSource().fetch(c, target())
    assert "ignored" in caplog.text.lower()
    assert "categories" in caplog.text


@respx.mock
async def test_an_unknown_filter_is_dropped_before_it_is_sent(client, caplog):
    allow_robots()
    route = respx.get(url__startswith=SEARCH).respond(200, json=page([]))
    async with client as c:
        await FreeHireSource().fetch(c, target(categories="ml_ai", countries="in"))

    sent = str(route.calls[0].request.url)
    assert "categories=" not in sent, "the plural form must never reach the API"
    assert "countries=in" in sent
    assert "categories" in caplog.text


# --------------------------------------------------------------------------- #
# Aggregator rows
# --------------------------------------------------------------------------- #


@respx.mock
async def test_aggregator_rows_are_dropped(client):
    """whatjobs rows are affiliate redirects; the candidate cannot apply there."""
    allow_robots()
    respx.get(url__startswith=SEARCH).respond(
        200,
        json=page(
            [
                {"title": "ML Engineer", "company": "Real", "source": "greenhouse",
                 "url": "https://boards.greenhouse.io/r/jobs/1"},
                {"title": "ML Engineer", "company": "Via Aggregator", "source": "whatjobs-in",
                 "url": "https://in.whatjobs.com/pub_api__cpl__1?utm_campaign=publisher"},
            ]
        ),
    )
    async with client as c:
        jobs = await FreeHireSource().fetch(c, target())
    assert [j.company_name for j in jobs] == ["Real"]


@respx.mock
async def test_a_row_without_a_url_or_title_is_skipped_not_fatal(client):
    allow_robots()
    respx.get(url__startswith=SEARCH).respond(
        200,
        json=page(
            [
                {"title": "No URL", "company": "X", "source": "greenhouse"},
                {"company": "Y", "source": "greenhouse", "url": "https://x/1"},
                {"title": "Good", "company": "Z", "source": "greenhouse", "url": "https://x/2"},
            ]
        ),
    )
    async with client as c:
        jobs = await FreeHireSource().fetch(c, target())
    assert [j.title for j in jobs] == ["Good"]


# --------------------------------------------------------------------------- #
# Pagination
# --------------------------------------------------------------------------- #


@respx.mock
async def test_pagination_stops_on_a_short_page(client):
    allow_robots()
    def rows(start, n):
        return [
            {"title": f"Role {i}", "company": "A", "source": "greenhouse", "url": f"https://x/{i}"}
            for i in range(start, start + n)
        ]

    route = respx.get(url__startswith=SEARCH)
    route.side_effect = [
        respx.MockResponse(200, json=page(rows(0, 100), total=150)),
        respx.MockResponse(200, json=page(rows(100, 50), total=150)),
    ]
    async with client as c:
        jobs = await FreeHireSource().fetch(c, target())
    assert len(jobs) == 150
    assert route.call_count == 2


@respx.mock
async def test_pagination_respects_the_page_cap(client, caplog):
    allow_robots()
    full = [
        {"title": f"R{i}", "company": "A", "source": "greenhouse", "url": f"https://x/{i}"}
        for i in range(100)
    ]
    respx.get(url__startswith=SEARCH).respond(200, json=page(full, total=100000))
    async with client as c:
        jobs = await FreeHireSource().fetch(c, target(category="ml_ai", max_pages=2))
    assert len(jobs) == 100, "the same 100 urls dedup across pages"
    assert "cap" in caplog.text.lower()


@respx.mock
async def test_max_pages_is_not_sent_as_a_filter(client):
    allow_robots()
    route = respx.get(url__startswith=SEARCH).respond(200, json=page([]))
    async with client as c:
        await FreeHireSource().fetch(c, target(category="ml_ai", max_pages=3))
    assert "max_pages" not in str(route.calls[0].request.url)


# --------------------------------------------------------------------------- #
# Failure and dispatch
# --------------------------------------------------------------------------- #


@respx.mock
async def test_a_server_error_raises_source_unavailable(client):
    """One failing source must not end a scan; run_scan catches this."""
    allow_robots()
    respx.get(url__startswith=SEARCH).respond(500)
    async with client as c:
        with pytest.raises(SourceUnavailable):
            await FreeHireSource().fetch(c, target())


def test_matches_only_its_own_targets():
    source = FreeHireSource()
    assert source.matches(Target(name="x", ats="freehire"))
    assert source.matches(Target(name="x", ats="FreeHire"))
    assert not source.matches(Target(name="x", ats="greenhouse"))
    assert not source.matches(Target(name="x"))


def test_the_registry_dispatches_to_it():
    from jobhunter.sources.registry import resolve

    assert resolve(Target(name="x", ats="freehire")) is not None
    assert resolve(Target(name="x", ats="freehire")).name == "freehire"


# --------------------------------------------------------------------------- #
# Dedup across sources
# --------------------------------------------------------------------------- #


def test_the_same_posting_from_two_sources_collapses_to_one_row(tmp_path):
    """A Greenhouse job seen directly and via FreeHire must not double-count."""
    from jobhunter import db
    from jobhunter.models import Job, RawJob

    db.init_db(f"sqlite+pysqlite:///{tmp_path / 'd.db'}")
    with db.session_scope() as session:
        company = db.upsert_company(session, Target(name="Acme"))
        common = dict(
            title="Machine Learning Engineer",
            location="Bengaluru",
            url="https://boards.greenhouse.io/acme/jobs/1",
            description="Train models.",
        )
        db.upsert_job(session, company, RawJob(source="greenhouse", external_id="1", **common))
        _, is_new = db.upsert_job(
            session, company, RawJob(source="freehire:greenhouse", external_id="acme:1", **common)
        )
        assert is_new is False
        assert session.query(Job).count() == 1


# --------------------------------------------------------------------------- #
# Catalogue persistence
# --------------------------------------------------------------------------- #


def test_catalogue_rows_are_filed_under_the_real_employer(tmp_path):
    """Filing them under the search's own name makes the shortlist unreadable."""
    from jobhunter import db
    from jobhunter.models import Company, Job, RawJob
    from jobhunter.pipeline import _persist_catalogue

    db.init_db(f"sqlite+pysqlite:///{tmp_path / 'c.db'}")
    raws = [
        RawJob(source="freehire:workday", external_id="1", title="ML Engineer",
               company_name="Acme", location="Bengaluru", url="https://a/1"),
        RawJob(source="freehire:freshteam", external_id="2", title="Data Scientist",
               company_name="Globex", location="Bengaluru", url="https://g/2"),
    ]
    with db.session_scope() as session:
        new, closed = _persist_catalogue(session, target(), raws)
        assert (new, closed) == (2, 0)
    with db.session_scope() as session:
        names = {c.name for c in session.query(Company).all()}
        assert {"Acme", "Globex"} <= names
        assert "FreeHire (India ML)" not in names


def test_a_catalogue_scan_never_closes_an_employers_other_jobs(tmp_path):
    """The dangerous edge.

    A keyword search returns what matched, not everything a company has open. If
    stale-closing ran over these rows it would close every posting the direct
    adapters found at the same employer.
    """
    from jobhunter import db
    from jobhunter.models import Job, RawJob
    from jobhunter.pipeline import _persist_catalogue

    db.init_db(f"sqlite+pysqlite:///{tmp_path / 'c2.db'}")
    with db.session_scope() as session:
        acme = db.upsert_company(session, Target(name="Acme"))
        db.upsert_job(session, acme, RawJob(source="greenhouse", external_id="99",
                                            title="Backend Engineer", location="Bengaluru",
                                            url="https://a/99"))
        session.commit()

    with db.session_scope() as session:
        _persist_catalogue(session, target(), [
            RawJob(source="freehire:workday", external_id="1", title="ML Engineer",
                   company_name="Acme", location="Bengaluru", url="https://a/1"),
        ])

    with db.session_scope() as session:
        backend = session.query(Job).filter(Job.title == "Backend Engineer").one()
        assert backend.closed_at is None, "the search must not close jobs it never looked for"


def test_a_row_with_no_company_name_falls_back_to_the_search_name(tmp_path):
    from jobhunter import db
    from jobhunter.models import Company, RawJob
    from jobhunter.pipeline import _persist_catalogue

    db.init_db(f"sqlite+pysqlite:///{tmp_path / 'c3.db'}")
    with db.session_scope() as session:
        _persist_catalogue(session, target(), [
            RawJob(source="freehire:x", external_id="1", title="T", url="https://a/1"),
        ])
    with db.session_scope() as session:
        assert session.query(Company).filter(Company.name == "FreeHire (India ML)").count() == 1
