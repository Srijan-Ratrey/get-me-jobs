"""Adapter parser tests against saved live responses.

Fixtures in tests/fixtures/ are real captured responses, so a parser passing
here is a parser that works on production data — which is the whole point of
capturing them rather than hand-writing the shapes.
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
import respx

from jobhunter.config import Target
from jobhunter.http import SourceUnavailable
from jobhunter.sources import ashby, careers_page, greenhouse, lever, workable
from jobhunter.sources.registry import fetch_target, resolve

GH_ORIGIN = "https://boards-api.greenhouse.io"
LV_ORIGIN = "https://api.lever.co"
AB_ORIGIN = "https://api.ashbyhq.com"


# --------------------------------------------------------------------------- #
# Greenhouse
# --------------------------------------------------------------------------- #


@respx.mock
async def test_greenhouse_parses_fields(polite_client, allow_robots, fixture_text):
    allow_robots(GH_ORIGIN)
    url = greenhouse.BOARD_URL.format(token="phonepe")
    respx.get(url).respond(200, text=fixture_text("greenhouse.json"))

    target = Target(name="PhonePe", ats="greenhouse", ats_token="phonepe")
    async with polite_client() as client:
        jobs = await greenhouse.GreenhouseSource().fetch(client, target)

    assert len(jobs) == 69
    job = jobs[0]
    assert job.source == "greenhouse"
    assert job.external_id == "7650503003"
    assert job.title == "AI Creative Lead"
    assert job.location == "Bangalore"
    assert job.url == "https://job-boards.greenhouse.io/phonepe/jobs/7650503003"
    assert job.company_name == "PhonePe"
    # first_published, not updated_at (2026-06-15) - see docs/sources.md.
    assert job.posted_at == datetime(2026, 3, 6, 9, 36, 13, tzinfo=timezone.utc)
    assert job.remote is None  # Greenhouse exposes no remote flag


@respx.mock
async def test_greenhouse_unescapes_before_stripping(polite_client, allow_robots, fixture_text):
    """`content` is entity-escaped HTML; stripping tags first leaves &lt;p&gt; behind."""
    allow_robots(GH_ORIGIN)
    respx.get(greenhouse.BOARD_URL.format(token="phonepe")).respond(
        200, text=fixture_text("greenhouse.json")
    )
    async with polite_client() as client:
        jobs = await greenhouse.GreenhouseSource().fetch(
            client, Target(name="PhonePe", ats="greenhouse", ats_token="phonepe")
        )

    assert all(j.description for j in jobs)
    for job in jobs:
        assert "&lt;" not in job.description
        assert "<p>" not in job.description
        assert "<div" not in job.description


async def test_greenhouse_without_token_is_unavailable(polite_client):
    async with polite_client() as client:
        with pytest.raises(SourceUnavailable, match="ats_token"):
            await greenhouse.GreenhouseSource().fetch(client, Target(name="X", ats="greenhouse"))


# --------------------------------------------------------------------------- #
# Lever
# --------------------------------------------------------------------------- #


@respx.mock
async def test_lever_parses_fields(polite_client, allow_robots, fixture_text):
    allow_robots(LV_ORIGIN)
    respx.get(lever.POSTINGS_URL.format(token="meesho")).respond(
        200, text=fixture_text("lever.json")
    )
    async with polite_client() as client:
        jobs = await lever.LeverSource().fetch(
            client, Target(name="Meesho", ats="lever", ats_token="meesho")
        )

    assert len(jobs) == 49
    job = jobs[0]
    assert job.external_id == "7d9af9b5-c1c7-48ec-bbb5-9b25e49f6596"
    # Lever's title lives in `text`, not `title`.
    assert job.title == "AM/ Manager - Risk & Decision Science"
    assert job.location == "Bangalore, Karnataka"
    assert job.url == "https://jobs.lever.co/meesho/7d9af9b5-c1c7-48ec-bbb5-9b25e49f6596"
    # createdAt is epoch milliseconds.
    assert job.posted_at == datetime.fromtimestamp(1757916149.833, tz=timezone.utc)
    assert job.remote is False  # workplaceType == "onsite"


@respx.mock
async def test_lever_description_includes_lists(polite_client, allow_robots, fixture_json, fixture_text):
    """descriptionPlain alone omits requirements, which live in lists[]."""
    allow_robots(LV_ORIGIN)
    respx.get(lever.POSTINGS_URL.format(token="meesho")).respond(
        200, text=fixture_text("lever.json")
    )
    async with polite_client() as client:
        jobs = await lever.LeverSource().fetch(
            client, Target(name="Meesho", ats="lever", ats_token="meesho")
        )

    raw = fixture_json("lever.json")[0]
    heading = raw["lists"][0]["text"]
    assert heading in jobs[0].description
    # The concatenated description must be strictly longer than descriptionPlain,
    # or the requirements the scorer reads have been dropped on the floor.
    assert len(jobs[0].description) > len(raw["descriptionPlain"])
    assert "<li>" not in jobs[0].description


@respx.mock
async def test_lever_rejects_non_array(polite_client, allow_robots):
    allow_robots(LV_ORIGIN)
    respx.get(lever.POSTINGS_URL.format(token="acme")).respond(200, json={"jobs": []})
    async with polite_client() as client:
        with pytest.raises(SourceUnavailable, match="not a list"):
            await lever.LeverSource().fetch(
                client, Target(name="Acme", ats="lever", ats_token="acme")
            )


# --------------------------------------------------------------------------- #
# Ashby
# --------------------------------------------------------------------------- #


@respx.mock
async def test_ashby_parses_fields(polite_client, allow_robots, fixture_text):
    allow_robots(AB_ORIGIN)
    respx.get(ashby.BOARD_URL.format(token="sarvam")).respond(
        200, text=fixture_text("ashby.json")
    )
    async with polite_client() as client:
        jobs = await ashby.AshbySource().fetch(
            client, Target(name="Sarvam", ats="ashby", ats_token="sarvam")
        )

    assert len(jobs) == 63
    job = jobs[0]
    assert job.external_id == "3d479c06-8537-40ee-bcbb-a7d337013da4"
    assert job.title == "Head of Growth Marketing"
    assert job.location == "Bengaluru"
    assert job.url == "https://jobs.ashbyhq.com/sarvam/3d479c06-8537-40ee-bcbb-a7d337013da4"
    assert job.posted_at == datetime(2026, 6, 3, 17, 19, 8, 998000, tzinfo=timezone.utc)
    assert job.remote is False  # isRemote is authoritative


@respx.mock
async def test_ashby_filters_unlisted(polite_client, allow_robots):
    """isListed == false is an internal posting and must never surface."""
    allow_robots(AB_ORIGIN)
    respx.get(ashby.BOARD_URL.format(token="acme")).respond(
        200,
        json={
            "apiVersion": "1",
            "jobs": [
                {
                    "id": "listed",
                    "title": "Data Scientist",
                    "location": "Bengaluru",
                    "jobUrl": "https://jobs.ashbyhq.com/acme/listed",
                    "isListed": True,
                    "isRemote": False,
                    "descriptionPlain": "text",
                },
                {
                    "id": "hidden",
                    "title": "Secret Internal Role",
                    "location": "Bengaluru",
                    "jobUrl": "https://jobs.ashbyhq.com/acme/hidden",
                    "isListed": False,
                    "isRemote": False,
                    "descriptionPlain": "text",
                },
            ],
        },
    )
    async with polite_client() as client:
        jobs = await ashby.AshbySource().fetch(
            client, Target(name="Acme", ats="ashby", ats_token="acme")
        )

    assert [j.external_id for j in jobs] == ["listed"]


@respx.mock
async def test_ashby_joins_secondary_locations(polite_client, allow_robots):
    allow_robots(AB_ORIGIN)
    respx.get(ashby.BOARD_URL.format(token="acme")).respond(
        200,
        json={
            "jobs": [
                {
                    "id": "1",
                    "title": "ML Engineer",
                    "location": "Bengaluru",
                    "secondaryLocations": [
                        {"location": "Mumbai", "address": {"postalAddress": {}}},
                        {"location": "Delhi"},
                    ],
                    "jobUrl": "https://jobs.ashbyhq.com/acme/1",
                    "isListed": True,
                    "isRemote": False,
                    "descriptionPlain": "text",
                }
            ]
        },
    )
    async with polite_client() as client:
        jobs = await ashby.AshbySource().fetch(
            client, Target(name="Acme", ats="ashby", ats_token="acme")
        )

    assert jobs[0].location == "Bengaluru | Mumbai | Delhi"


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "ats,expected",
    [("greenhouse", "greenhouse"), ("lever", "lever"), ("ashby", "ashby"), ("GREENHOUSE", "greenhouse")],
)
def test_registry_resolves_by_ats(ats, expected):
    adapter = resolve(Target(name="X", ats=ats, ats_token="t"))
    assert adapter is not None and adapter.name == expected


def test_registry_returns_none_for_unknown_ats():
    assert resolve(Target(name="X", ats="workday", ats_token="t")) is None


async def test_fetch_target_unknown_ats_raises_source_unavailable(polite_client):
    async with polite_client() as client:
        with pytest.raises(SourceUnavailable, match="no adapter"):
            await fetch_target(client, Target(name="X", ats="workday"))


@respx.mock
async def test_http_error_becomes_source_unavailable(polite_client, allow_robots):
    """A 404 board must be a skippable per-target failure, not a crash."""
    allow_robots(GH_ORIGIN)
    respx.get(greenhouse.BOARD_URL.format(token="nope")).respond(404)
    async with polite_client() as client:
        with pytest.raises(SourceUnavailable):
            await fetch_target(client, Target(name="Nope", ats="greenhouse", ats_token="nope"))


# --------------------------------------------------------------------------- #
# Workable
# --------------------------------------------------------------------------- #


@respx.mock
async def test_workable_parses_fields(polite_client, allow_robots, fixture_text):
    """The live shape has no `id`, no `requirements`, no `benefits`."""
    allow_robots("https://apply.workable.com")
    respx.get(workable.WIDGET_URL.format(token="acme")).respond(
        200, text=fixture_text("workable.json")
    )
    async with polite_client() as client:
        jobs = await workable.WorkableSource().fetch(
            client, Target(name="Acme", ats="workable", ats_token="acme")
        )

    job = jobs[0]
    assert job.external_id == "401786A940"  # shortcode; there is no id field
    assert job.title == "Implementation Manager"
    assert job.location == "Boston | Massachusetts | United States"
    assert job.url == "https://apply.workable.com/j/401786A940"
    assert job.remote is False  # telecommuting
    assert job.seniority_hint == "Mid-Senior level"
    assert job.description and "<p>" not in job.description


@respx.mock
async def test_workable_empty_board_is_not_an_error(polite_client, allow_robots):
    """A valid account with nothing open returns [], not an exception."""
    allow_robots("https://apply.workable.com")
    respx.get(workable.WIDGET_URL.format(token="quiet")).respond(
        200, json={"name": "Quiet", "description": None, "jobs": []}
    )
    async with polite_client() as client:
        assert await workable.WorkableSource().fetch(
            client, Target(name="Quiet", ats="workable", ats_token="quiet")
        ) == []


@respx.mock
async def test_workable_falls_back_to_v3_on_404(polite_client, allow_robots):
    allow_robots("https://apply.workable.com")
    respx.get(workable.WIDGET_URL.format(token="acme")).respond(404)
    respx.get(workable.V3_URL.format(token="acme")).respond(
        200,
        json={
            "jobs": [
                {
                    "title": "Data Scientist",
                    "shortcode": "V3CODE",
                    "url": "https://apply.workable.com/j/V3CODE",
                    "city": "Bengaluru",
                    "country": "India",
                    "telecommuting": True,
                    "published_on": "2026-07-01",
                    "description": "<p>Python and ML</p>",
                }
            ]
        },
    )
    async with polite_client() as client:
        jobs = await workable.WorkableSource().fetch(
            client, Target(name="Acme", ats="workable", ats_token="acme")
        )
    assert [j.external_id for j in jobs] == ["V3CODE"]
    assert jobs[0].remote is True


# --------------------------------------------------------------------------- #
# Fingerprinting and the careers-page crawler
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "html,ats,token,supported",
    [
        ('<a href="https://boards.greenhouse.io/acme">Jobs</a>', "greenhouse", "acme", True),
        ('<iframe src="https://job-boards.greenhouse.io/acme/x">', "greenhouse", "acme", True),
        ('<a href="https://jobs.lever.co/meesho/abc">', "lever", "meesho", True),
        ('<a href="https://jobs.ashbyhq.com/sarvam/1">', "ashby", "sarvam", True),
        ('<a href="https://apply.workable.com/acme/">', "workable", "acme", True),
        ('<a href="https://acme.myworkdayjobs.com/careers">', "workday", None, False),
        ('<a href="https://acme.darwinbox.in/ms/candidate">', "darwinbox", None, False),
        ('<a href="https://acme.keka.com/careers">', "keka", None, False),
        ('<a href="https://jobs.smartrecruiters.com/Acme">', "smartrecruiters", None, False),
    ],
)
def test_fingerprint_detects_ats(html, ats, token, supported):
    detected = careers_page.fingerprint(html)
    assert detected is not None
    assert detected.ats == ats
    assert detected.token == token
    assert detected.supported is supported


def test_fingerprint_returns_none_for_plain_page():
    assert careers_page.fingerprint("<html><body><p>We have no jobs</p></body></html>") is None


def test_spa_detection():
    shell = '<html><head><script src="/app.js"></script></head><body><div id="root"></div></body></html>'
    assert careers_page.looks_like_spa(shell) is True
    assert careers_page.looks_like_spa("") is True
    real = "<html><body>" + ("Senior Data Scientist in Bengaluru. " * 40) + "</body></html>"
    assert careers_page.looks_like_spa(real) is False


def test_extract_jsonld():
    html = """
    <html><body><script type="application/ld+json">
    {"@context":"https://schema.org","@type":"JobPosting","title":"ML Engineer",
     "datePosted":"2026-07-01","description":"<p>Build <b>models</b></p>",
     "hiringOrganization":{"@type":"Organization","name":"Acme"},
     "identifier":{"@type":"PropertyValue","value":"REQ-7"},
     "url":"/careers/ml-engineer",
     "jobLocation":{"@type":"Place","address":{"@type":"PostalAddress",
       "addressLocality":"Bengaluru","addressCountry":"India"}}}
    </script></body></html>
    """
    jobs = careers_page.extract_jsonld(html, "https://acme.com/careers")
    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "ML Engineer"
    assert job.external_id == "REQ-7"
    assert job.location == "Bengaluru, India"
    assert job.url == "https://acme.com/careers/ml-engineer"
    assert job.company_name == "Acme"
    assert "<b>" not in job.description and "models" in job.description


def test_extract_jsonld_handles_graph_and_arrays():
    html = """
    <script type="application/ld+json">
    {"@graph":[{"@type":"Organization","name":"Acme"},
               {"@type":"JobPosting","title":"Data Scientist","url":"/jobs/1"}]}
    </script>
    <script type="application/ld+json">[{"@type":"JobPosting","title":"AI Engineer","url":"/jobs/2"}]</script>
    <script type="application/ld+json">{ not json at all </script>
    """
    titles = {j.title for j in careers_page.extract_jsonld(html, "https://acme.com/")}
    assert titles == {"Data Scientist", "AI Engineer"}


def test_harvest_links_is_same_origin_and_capped():
    anchors = "".join(f'<a href="/careers/job-{i}">Role {i}</a>' for i in range(80))
    html = f"""<html><body>
      <a href="https://evil.com/jobs/leak">Offsite</a>
      <a href="/careers/">Index</a>
      <a href="mailto:x@y.com">Mail</a>
      {anchors}
    </body></html>"""
    links = careers_page.harvest_links(html, "https://acme.com/careers")
    assert len(links) == careers_page.MAX_DETAIL_LINKS
    assert all(l.startswith("https://acme.com/careers/job-") for l in links)


def test_extract_repeated_structure():
    html = """<html><body><nav><a href="/about">About</a></nav>
    <ul id="openings">
      <li><a href="/careers/ml-engineer">ML Engineer</a></li>
      <li><a href="/careers/data-scientist">Data Scientist</a></li>
      <li><a href="/careers/ai-engineer">AI Engineer</a></li>
    </ul></body></html>"""
    jobs = careers_page.extract_repeated(html, "https://acme.com/careers")
    assert {j.title for j in jobs} == {"ML Engineer", "Data Scientist", "AI Engineer"}
    # external_id must be stable across runs, so a path rather than an index.
    assert all(j.external_id.startswith("/careers/") for j in jobs)


@respx.mock
async def test_registry_hands_off_to_fingerprinted_ats(polite_client, allow_robots, fixture_text):
    """A careers page pointing at Greenhouse must use the API, not be scraped."""
    allow_robots("https://acme.com", GH_ORIGIN)
    respx.get("https://acme.com/careers").respond(
        200,
        text='<html><body><p>' + "Join us. " * 60
        + '</p><a href="https://boards.greenhouse.io/phonepe">See roles</a></body></html>',
    )
    respx.get(greenhouse.BOARD_URL.format(token="phonepe")).respond(
        200, text=fixture_text("greenhouse.json")
    )
    async with polite_client() as client:
        jobs = await fetch_target(client, Target(name="Acme", careers_url="https://acme.com/careers"))

    assert len(jobs) == 69
    assert jobs[0].source == "greenhouse"


@respx.mock
async def test_registry_reports_unsupported_fingerprint(polite_client, allow_robots):
    allow_robots("https://acme.com")
    respx.get("https://acme.com/careers").respond(
        200,
        text="<html><body><p>" + "We are hiring. " * 60
        + '</p><a href="https://acme.myworkdayjobs.com/x">Apply</a></body></html>',
    )
    async with polite_client() as client:
        with pytest.raises(SourceUnavailable, match="workday"):
            await fetch_target(client, Target(name="Acme", careers_url="https://acme.com/careers"))


@respx.mock
async def test_registry_reports_spa_rather_than_silently_finding_nothing(
    polite_client, allow_robots
):
    allow_robots("https://acme.com")
    respx.get("https://acme.com/careers").respond(
        200, text='<html><head><script src="/a.js"></script></head><body><div id="root"></div></body></html>'
    )
    async with polite_client() as client:
        with pytest.raises(SourceUnavailable, match="client-rendered"):
            await fetch_target(client, Target(name="Acme", careers_url="https://acme.com/careers"))


async def test_target_with_neither_ats_nor_careers_url(polite_client):
    async with polite_client() as client:
        with pytest.raises(SourceUnavailable, match="careers_url"):
            await fetch_target(client, Target(name="Bare"))
