"""Workday, Oracle, SuccessFactors and Zoho Recruit against saved live responses.

These four share a cost the original adapters do not: three of them need a
second request per posting for its description, so the tests cover the
location filter and description cap that keep that affordable, and that a
posting whose detail fails is kept rather than lost.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest
import respx

from jobhunter.config import Target
from jobhunter.http import RobotsDisallowed, SourceUnavailable
from jobhunter.sources import oracle, successfactors, workday, zoho_recruit
from jobhunter.sources.registry import fetch_target

WANTED = ["Bengaluru", "India", "Remote"]  # the shipped profile.yaml

# --------------------------------------------------------------------------- #
# Workday
# --------------------------------------------------------------------------- #

WD_ORIGIN = "https://genpact.wd108.myworkdayjobs.com"
WD_API = f"{WD_ORIGIN}/wday/cxs/genpact/External_Careers"
WD_NOIDA = "/job/1401-G-India-Stellar-STPI-Noida/Analyst---Process-Analytics---Claims---Underwriting-5B_JR10034229"


def _genpact(**search) -> Target:
    return Target(
        name="Genpact",
        ats="workday",
        ats_token=f"{WD_ORIGIN}/en-US/External_Careers",
        search=search,
    )


@respx.mock
async def test_workday_details_only_wanted_postings(polite_client, allow_robots, fixture_text):
    allow_robots(WD_ORIGIN)
    respx.post(f"{WD_API}/jobs").respond(200, text=fixture_text("workday_list.json"))
    respx.get(f"{WD_API}{WD_NOIDA}").respond(200, text=fixture_text("workday_detail.json"))
    # The Bangalore posting's detail fails: it must survive on its listing alone.
    bangalore = respx.get(url__regex=r".*/job/.*Bangalore.*").respond(500)
    abroad = respx.get(url__regex=r".*/job/.*(Pristina|Cluj|Krakow).*").respond(200, json={})

    async with polite_client() as client:
        jobs = await workday.WorkdaySource().fetch(client, _genpact(locations=WANTED))

    assert [j.location for j in jobs] == [
        "1401-G-India: Stellar, STPI, Noida | India",
        "1415-GERC: Prestige Technology Park IV, Bangalore",
    ]
    assert not abroad.called  # three postings abroad cost no detail request
    assert bangalore.called

    noida = jobs[0]
    assert noida.source == "workday"
    assert noida.external_id == WD_NOIDA
    assert noida.title == "Analyst - Process Analytics - Claims & Underwriting 5B"
    assert noida.url == f"{WD_ORIGIN}/External_Careers{WD_NOIDA}"
    assert noida.posted_at == datetime(2026, 11, 13, tzinfo=timezone.utc)
    assert noida.remote is False  # remoteType "Hybrid"
    assert noida.description and "Claims & Underwriting" in noida.description

    assert jobs[1].description is None
    assert jobs[1].url.startswith(f"{WD_ORIGIN}/External_Careers/job/")


@respx.mock
async def test_workday_several_locations_need_the_detail_to_decide(
    polite_client, allow_robots, fixture_text
):
    """ "2 Locations" names no place; PwC's turns out to be Prague and Brno."""
    origin = "https://pwc.wd3.myworkdayjobs.com"
    api = f"{origin}/wday/cxs/pwc/Global_Experienced_Careers"
    allow_robots(origin)
    respx.post(f"{api}/jobs").respond(200, text=fixture_text("workday_multi_list.json"))
    respx.get(url__startswith=f"{api}/job/").respond(
        200, text=fixture_text("workday_multi_detail.json")
    )
    target = Target(name="PwC", ats="workday", ats_token=f"{origin}/Global_Experienced_Careers")

    async with polite_client() as client:
        wanted = await workday.WorkdaySource().fetch(
            client, target.model_copy(update={"search": {"locations": WANTED}})
        )
        everything = await workday.WorkdaySource().fetch(client, target)

    assert wanted == []
    assert [j.location for j in everything] == ["Prague | Brno | Czechia"]


@respx.mock
async def test_workday_past_the_cap_postings_are_kept_without_description(
    polite_client, allow_robots, fixture_text
):
    allow_robots(WD_ORIGIN)
    respx.post(f"{WD_API}/jobs").respond(200, text=fixture_text("workday_list.json"))
    detail = respx.get(url__startswith=f"{WD_API}/job/").respond(
        200, text=fixture_text("workday_detail.json")
    )

    async with polite_client() as client:
        jobs = await workday.WorkdaySource().fetch(
            client, _genpact(locations=WANTED, max_details=1)
        )

    assert detail.call_count == 1
    assert len(jobs) == 2 and jobs[1].description is None


@respx.mock
async def test_workday_skips_descriptions_for_titles_the_profile_excludes(
    polite_client, allow_robots, fixture_text
):
    """The scorer zeroes these on title alone, so their descriptions are never read."""
    allow_robots(WD_ORIGIN)
    respx.post(f"{WD_API}/jobs").respond(200, text=fixture_text("workday_list.json"))
    detail = respx.get(url__startswith=f"{WD_API}/job/").respond(
        200, text=fixture_text("workday_detail.json")
    )

    target = _genpact(locations=WANTED, exclude_titles=["analyst"])
    async with polite_client() as client:
        jobs = await workday.WorkdaySource().fetch(client, target)

    # The Noida "Analyst - Process Analytics" posting is kept, but undescribed.
    assert len(jobs) == 2
    assert jobs[0].description is None
    assert detail.call_count == 1  # only the Bangalore posting


@respx.mock
async def test_workday_respects_robots_on_the_board_path(polite_client):
    """robots.txt names boards, never /wday/cxs/; Citi disallows its `/2/` board."""
    respx.get("https://citi.wd5.myworkdayjobs.com/robots.txt").respond(
        200, text="User-agent: *\nDisallow: /2/\n"
    )
    api = respx.post(url__startswith="https://citi.wd5.myworkdayjobs.com/wday/")
    target = Target(name="Citi", ats="workday", ats_token="https://citi.wd5.myworkdayjobs.com/2")

    async with polite_client() as client:
        with pytest.raises(RobotsDisallowed):
            await workday.WorkdaySource().fetch(client, target)
    assert not api.called


@respx.mock
async def test_workday_stops_when_a_page_repeats(polite_client, allow_robots):
    """Some tenants answer offsets past 2000 with page one again."""
    allow_robots(WD_ORIGIN)
    page = {
        "total": 5000,
        "jobPostings": [
            {"title": f"Role {i}", "externalPath": f"/job/Pune/Role_{i}", "locationsText": "Pune"}
            for i in range(workday.PAGE_SIZE)
        ],
    }
    route = respx.post(f"{WD_API}/jobs").respond(200, json=page)
    respx.get(url__startswith=f"{WD_API}/job/").respond(200, json={})

    async with polite_client() as client:
        jobs = await workday.WorkdaySource().fetch(client, _genpact())

    assert route.call_count == 2 and len(jobs) == workday.PAGE_SIZE


@pytest.mark.parametrize(
    "label,expected",
    [
        ("Posted Today", datetime(2026, 11, 13, tzinfo=timezone.utc)),
        ("Posted Yesterday", datetime(2026, 11, 12, tzinfo=timezone.utc)),
        ("Posted 5 Days Ago", datetime(2026, 11, 8, tzinfo=timezone.utc)),
        ("Posted 30+ Days Ago", None),
        (None, None),
    ],
)
def test_workday_relative_posted_dates(label, expected):
    now = datetime(2026, 11, 13, 15, 30, tzinfo=timezone.utc)
    assert workday._posted_on(label, now) == expected


@pytest.mark.parametrize(
    "value,site",
    [
        ("https://genpact.wd108.myworkdayjobs.com/External_Careers", "External_Careers"),
        (
            "https://genpact.wd108.myworkdayjobs.com/en-US/External_Careers/job/x",
            "External_Careers",
        ),
        ("genpact.wd108.myworkdayjobs.com/External_Careers", "External_Careers"),
        ("https://acme.myworkdayjobs.com/careers", None),  # no wdN instance
    ],
)
def test_workday_board_urls(value, site):
    board = workday.parse_board(value)
    assert (board.site if board else None) == site


# --------------------------------------------------------------------------- #
# Oracle
# --------------------------------------------------------------------------- #

ORC_HOST = "https://egug.fa.us2.oraclecloud.com"
ORC_BOARD = f"{ORC_HOST}/hcmUI/CandidateExperience/en/sites/jobsearch"


@respx.mock
async def test_oracle_details_only_wanted_postings(polite_client, allow_robots, fixture_text):
    allow_robots(ORC_HOST)
    respx.get(url__regex=r".*/recruitingCEJobRequisitions\?.*").respond(
        200, text=fixture_text("oracle_list.json")
    )
    respx.get(url__regex=r".*/recruitingCEJobRequisitionDetails\?.*26015720.*").respond(
        200, text=fixture_text("oracle_detail.json")
    )
    missing = respx.get(url__regex=r".*/recruitingCEJobRequisitionDetails\?.*26015867.*").respond(
        404
    )
    others = respx.get(url__regex=r".*/recruitingCEJobRequisitionDetails\?.*").respond(200, json={})

    target = Target(name="Amex", ats="oracle", ats_token=ORC_BOARD, search={"locations": WANTED})
    async with polite_client() as client:
        jobs = await oracle.OracleSource().fetch(client, target)

    assert [j.external_id for j in jobs] == ["26015720", "26015867"]
    assert missing.called and not others.called

    job = jobs[0]
    assert job.source == "oracle"
    assert job.title == "Senior Manager - Data Science"
    assert job.location == "Bengaluru, KA, India | Gurugram, HR, India"
    assert job.url == f"{ORC_BOARD}/job/26015720"
    assert job.posted_at == datetime(2026, 11, 13, 5, 51, 37, tzinfo=timezone.utc)
    assert job.remote is False  # ORA_HYBRID
    # Description, responsibilities and qualifications are three separate fields.
    assert job.description and "5+ years of relevant work experience" in job.description

    assert jobs[1].description is None
    assert jobs[1].posted_at is not None  # the listing's PostedDate stands in


@respx.mock
async def test_oracle_without_a_site_url_is_unavailable(polite_client):
    target = Target(name="Amex", ats="oracle", ats_token="https://egug.fa.us2.oraclecloud.com")
    async with polite_client() as client:
        with pytest.raises(SourceUnavailable, match="CandidateExperience"):
            await oracle.OracleSource().fetch(client, target)


# --------------------------------------------------------------------------- #
# SuccessFactors
# --------------------------------------------------------------------------- #

SF_ORIGIN = "https://careers.capgemini.com"


@respx.mock
async def test_successfactors_searches_each_place_and_reads_the_posting(
    polite_client, allow_robots, fixture_text
):
    allow_robots(SF_ORIGIN)
    tiles = respx.get(url__startswith=f"{SF_ORIGIN}/tile-search-results/").mock(
        side_effect=lambda request: respx.MockResponse(
            200,
            text=fixture_text("successfactors_tiles.html")
            if "startrow=0" in str(request.url)
            else "",
        )
    )
    respx.get(f"{SF_ORIGIN}/job/Bangalore-SAP-BRIM-Technical-Consultant/1400369333/").respond(
        200, text=fixture_text("successfactors_detail.html")
    )
    respx.get(url__regex=rf"{re.escape(SF_ORIGIN)}/job/.*").respond(404)

    target = Target(
        name="Capgemini", ats="successfactors", ats_token=SF_ORIGIN, search={"locations": WANTED}
    )
    async with polite_client() as client:
        jobs = await successfactors.SuccessFactorsSource().fetch(client, target)

    searched = {re.search(r"locationsearch=(\w+)", str(c.request.url))[1] for c in tiles.calls}
    assert searched == {"Bengaluru", "India"}  # never "Remote"
    assert [j.external_id for j in jobs] == ["1400369333", "1367727133", "1122131801"]

    job = jobs[0]
    assert job.source == "successfactors"
    assert job.title == "SAP BRIM Technical Consultant"
    assert job.location == "Pune, IN | Mumbai, IN | Gandhinagar, IN | Bangalore, IN"
    assert job.url == f"{SF_ORIGIN}/job/Bangalore-SAP-BRIM-Technical-Consultant/1400369333/"
    assert job.posted_at == datetime(2026, 10, 26, 2, 0, tzinfo=timezone.utc)
    assert job.description and len(job.description) > 500

    # The posting page failed: title from the tile, nothing invented.
    assert jobs[1].title == "SAP BTP CAP Lead"
    assert jobs[1].description is None and jobs[1].location is None


def test_successfactors_tiles_parse(fixture_text):
    rows = successfactors.parse_tiles(fixture_text("successfactors_tiles.html"))
    assert rows["1122131801"] == (
        "Compensation and Benefits Specialist - B",
        "/job/Noida-Compensation-and-Benefits-Specialist-B/1122131801/",
    )


@respx.mock
async def test_registry_hands_a_successfactors_site_to_its_adapter(polite_client, allow_robots):
    """The marker is the page's own CDN, so the careers URL is the board."""
    origin = "https://careers.acme.com"
    allow_robots(origin)
    respx.get(f"{origin}/").respond(
        200,
        text='<html><head><link href="https://rmkcdn.successfactors.com/x.css"></head>'
        f"<body><p>{'Join us. ' * 60}</p></body></html>",
    )
    tiles = respx.get(url__startswith=f"{origin}/tile-search-results/").respond(200, text="")

    async with polite_client() as client:
        jobs = await fetch_target(client, Target(name="Acme", careers_url=f"{origin}/"))

    assert jobs == [] and tiles.called


# --------------------------------------------------------------------------- #
# Zoho Recruit
# --------------------------------------------------------------------------- #

ZOHO_ORIGIN = "https://auxoai.zohorecruit.com"


@respx.mock
async def test_zoho_reads_embedded_records(polite_client, allow_robots, fixture_text):
    allow_robots(ZOHO_ORIGIN)
    respx.get(f"{ZOHO_ORIGIN}/jobs/Careers").respond(200, text=fixture_text("zoho_recruit.html"))

    target = Target(name="AuxoAI", ats="zoho_recruit", careers_url=f"{ZOHO_ORIGIN}/jobs/Careers")
    async with polite_client() as client:
        jobs = await zoho_recruit.ZohoRecruitSource().fetch(client, target)

    # Five records in the page; the one with Publish=false is not an open posting.
    assert len(jobs) == 4
    assert "Data Scientist / AI/ML Engineer" not in [j.title for j in jobs]
    job = jobs[0]
    assert job.source == "zoho_recruit"
    assert job.title == "Senior Data Engineer - GCP"
    assert job.location == "Bangalore North | Karnataka | India"
    assert job.url == f"{ZOHO_ORIGIN}/jobs/Careers/{job.external_id}"
    assert job.description and len(job.description) > 200
    assert job.posted_at is not None


@respx.mock
async def test_zoho_page_without_records_is_unavailable(polite_client, allow_robots):
    allow_robots(ZOHO_ORIGIN)
    respx.get(f"{ZOHO_ORIGIN}/jobs/Careers").respond(200, text="<html><body>Gone</body></html>")
    target = Target(name="AuxoAI", ats="zoho_recruit", ats_token=ZOHO_ORIGIN)
    async with polite_client() as client:
        with pytest.raises(SourceUnavailable, match="no job records"):
            await zoho_recruit.ZohoRecruitSource().fetch(client, target)


def test_zoho_rejects_a_host_that_is_not_zoho():
    assert zoho_recruit._origin("https://careers.acme.com") is None
    assert (
        zoho_recruit._origin("https://acme.zohorecruit.in/jobs/Careers")
        == "https://acme.zohorecruit.in"
    )
