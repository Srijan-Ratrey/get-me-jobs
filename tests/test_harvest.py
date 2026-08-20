"""Harvesting India-hiring companies from published ATS token lists.

Offline, like everything else: respx serves the three board listings and no real
board is ever contacted.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from jobhunter import harvest
from jobhunter.config import Profile, load_targets
from jobhunter.http import PoliteClient

PROFILE = Profile(
    titles=["Data Scientist", "Machine Learning Engineer", "ML Engineer", "AI Engineer"],
    exclude_keywords=["senior", "staff", "lead", "manager", "principal"],
)

GH = "https://boards-api.greenhouse.io/v1/boards/{t}/jobs"
LV = "https://api.lever.co/v0/postings/{t}?mode=json"
AB = "https://api.ashbyhq.com/posting-api/job-board/{t}"


@pytest.fixture
def client(tmp_path):
    return PoliteClient(cache_dir=tmp_path / "cache", cache_ttl=0, requests_per_second=1000)


def allow_robots(*origins: str) -> None:
    for origin in origins:
        respx.get(f"{origin}/robots.txt").respond(404)


def greenhouse_body(locations: list[str], name: str = "Acme", title: str = "X") -> dict:
    return {
        "jobs": [
            {"title": title, "company_name": name, "location": {"name": loc}} for loc in locations
        ]
    }


def greenhouse_jobs(pairs: list[tuple[str, str]], name: str = "Acme") -> dict:
    """Explicit (title, location) pairs, for relevance tests."""
    return {
        "jobs": [
            {"title": t, "company_name": name, "location": {"name": loc}} for t, loc in pairs
        ]
    }


def lever_body(locations: list[str]) -> list[dict]:
    return [{"text": "X", "categories": {"location": loc, "allLocations": [loc]}} for loc in locations]


def ashby_body(locations: list[str]) -> dict:
    return {"jobs": [{"title": "X", "location": loc, "secondaryLocations": []} for loc in locations]}


# --------------------------------------------------------------------------- #
# Probing each ATS
# --------------------------------------------------------------------------- #


@respx.mock
async def test_greenhouse_probe_counts_india_jobs_and_reads_the_company_name(client):
    allow_robots("https://boards-api.greenhouse.io")
    respx.get(GH.format(t="acme")).respond(
        200, json=greenhouse_body(["Bengaluru, India", "San Francisco, CA", "Bangalore"], name="Acme Corp")
    )
    async with client as c:
        probe = await harvest.probe_board(c, "greenhouse", "acme")

    assert probe.live is True
    assert probe.name == "Acme Corp", "the listing carries company_name, so no second request"
    assert probe.total_jobs == 3
    assert probe.india_jobs == 2


@respx.mock
async def test_lever_probe_reads_locations_from_categories(client):
    allow_robots("https://api.lever.co")
    respx.get(LV.format(t="acme")).respond(200, json=lever_body(["Bangalore, Karnataka", "Berlin"]))
    async with client as c:
        probe = await harvest.probe_board(c, "lever", "acme")
    assert (probe.total_jobs, probe.india_jobs) == (2, 1)


@respx.mock
async def test_ashby_probe_reads_location_and_secondary_locations(client):
    allow_robots("https://api.ashbyhq.com")
    body = {
        "jobs": [
            {"title": "A", "location": "New York", "secondaryLocations": [{"location": "Bengaluru"}]},
            {"title": "B", "location": "London", "secondaryLocations": ["Remote - EU"]},
        ]
    }
    respx.get(AB.format(t="acme")).respond(200, json=body)
    async with client as c:
        probe = await harvest.probe_board(c, "ashby", "acme")
    assert probe.india_jobs == 1, "a secondary location in India still means they hire in India"


@respx.mock
async def test_a_dead_token_is_recorded_not_raised(client):
    """Most tokens in a crawled list are stale. That must not end the sweep."""
    allow_robots("https://boards-api.greenhouse.io")
    respx.get(GH.format(t="gone")).respond(404)
    async with client as c:
        probe = await harvest.probe_board(c, "greenhouse", "gone")
    assert probe.live is False
    assert probe.error
    assert probe.india_jobs == 0


@respx.mock
async def test_us_remote_boards_do_not_count_as_india_employers(client):
    """The whole point of the sweep is India reach, so this must not leak."""
    allow_robots("https://boards-api.greenhouse.io")
    respx.get(GH.format(t="usonly")).respond(
        200, json=greenhouse_body(["USA | Remote", "Remote - California", "Remote-Friendly, United States"])
    )
    async with client as c:
        probe = await harvest.probe_board(c, "greenhouse", "usonly")
    assert probe.live is True
    assert probe.india_jobs == 0


@respx.mock
async def test_an_unanchored_remote_board_does_count(client):
    allow_robots("https://boards-api.greenhouse.io")
    respx.get(GH.format(t="anywhere")).respond(200, json=greenhouse_body(["Remote", "Remote (Anywhere)"]))
    async with client as c:
        probe = await harvest.probe_board(c, "greenhouse", "anywhere")
    assert probe.india_jobs == 2


# --------------------------------------------------------------------------- #
# Relevance: which India roles would the profile actually rank?
# --------------------------------------------------------------------------- #


@respx.mock
async def test_relevant_counts_only_india_roles_matching_the_profile(client):
    allow_robots("https://boards-api.greenhouse.io")
    respx.get(GH.format(t="acme")).respond(
        200,
        json=greenhouse_jobs(
            [
                ("Machine Learning Engineer", "Bengaluru"),      # counts
                ("Data Scientist", "Bangalore, India"),          # counts
                ("Account Executive", "Bengaluru"),              # India, wrong title
                ("Machine Learning Engineer", "San Francisco"),  # right title, wrong place
            ]
        ),
    )
    async with client as c:
        probe = await harvest.probe_board(c, "greenhouse", "acme", PROFILE)

    assert probe.india_jobs == 3, "three postings are in India"
    assert probe.relevant_india_jobs == 2, "only two of them are roles this profile wants"


@respx.mock
async def test_a_senior_ml_role_is_not_relevant(client):
    """Keeping a board for a role the scorer would hard-zero is pointless."""
    allow_robots("https://boards-api.greenhouse.io")
    respx.get(GH.format(t="acme")).respond(
        200,
        json=greenhouse_jobs(
            [
                ("Senior Machine Learning Engineer", "Bengaluru"),
                ("Staff Data Scientist", "Bengaluru"),
                ("Lead ML Engineer", "Bengaluru"),
            ]
        ),
    )
    async with client as c:
        probe = await harvest.probe_board(c, "greenhouse", "acme", PROFILE)

    assert probe.india_jobs == 3
    assert probe.relevant_india_jobs == 0


@respx.mock
async def test_relevance_is_skipped_without_a_profile(client):
    allow_robots("https://boards-api.greenhouse.io")
    respx.get(GH.format(t="acme")).respond(
        200, json=greenhouse_jobs([("Data Scientist", "Bengaluru")])
    )
    async with client as c:
        probe = await harvest.probe_board(c, "greenhouse", "acme")
    assert probe.india_jobs == 1
    assert probe.relevant_india_jobs == 0


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Machine Learning Engineer", True),
        ("Data Scientist II", True),  # numeral is the scorer's problem, not relevance's
        ("Senior Data Scientist", False),
        ("Engineering Manager, ML", False),
        ("Account Executive", False),
        ("", False),
    ],
)
def test_is_relevant_title(title, expected):
    assert harvest.is_relevant_title(title, PROFILE) is expected


# --------------------------------------------------------------------------- #
# The keep rule: relevance OR India volume
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "relevant,india,kept,why",
    [
        (1, 0, True, "a matching role today"),
        (0, 8, True, "a durable India presence"),
        (2, 40, True, "both"),
        (0, 7, False, "neither: no match and below the volume bar"),
        (0, 0, False, "nothing at all"),
    ],
)
def test_worth_watching_is_a_union(relevant, india, kept, why):
    probe = harvest.Probe(
        ats="greenhouse", token="x", live=True, india_jobs=india, relevant_india_jobs=relevant
    )
    assert probe.worth_watching(min_relevant=1, min_india_jobs=8) is kept, why


@respx.mock
async def test_a_small_ai_startup_survives_where_a_big_employer_does_not(companies_yaml, tmp_path, client):
    """The reason relevance exists.

    Filtering on India volume alone keeps Airbnb, which has fifteen India roles
    and none for this profile, and drops a five-person AI startup whose whole
    board is one Bengaluru ML opening. That is backwards.
    """
    allow_robots("https://boards-api.greenhouse.io")
    respx.get(GH.format(t="tinyai")).respond(
        200, json=greenhouse_jobs([("ML Engineer", "Bengaluru")], name="Tiny AI")
    )
    respx.get(GH.format(t="bigco")).respond(
        200,
        json=greenhouse_jobs(
            [("Sales Manager Intl", "Bengaluru")] * 15, name="Big Co"
        ),
    )

    async with client as c:
        await harvest.run_harvest(
            {"greenhouse": ["tinyai", "bigco"]},
            companies_path=companies_yaml,
            state_path=tmp_path / "state.jsonl",
            profile=PROFILE,
            min_relevant=1,
            min_india_jobs=20,
            client=c,
        )

    names = {t.name for t in load_targets(companies_yaml)}
    assert "Tiny AI" in names, "one matching Bengaluru role is the whole point"
    assert "Big Co" not in names, "fifteen non-matching India roles are not"


# --------------------------------------------------------------------------- #
# The sweep
# --------------------------------------------------------------------------- #


@pytest.fixture
def companies_yaml(tmp_path) -> Path:
    path = tmp_path / "companies.yaml"
    path.write_text(
        "# a comment that must survive\ncompanies:\n"
        "  - name: Sarvam AI\n    ats: ashby\n    ats_token: sarvam\n",
        encoding="utf-8",
    )
    return path


@respx.mock
async def test_sweep_adds_india_boards_and_leaves_the_rest(companies_yaml, tmp_path, client):
    allow_robots("https://boards-api.greenhouse.io")
    respx.get(GH.format(t="hiring")).respond(200, json=greenhouse_body(["Bengaluru"], name="Hiring Co"))
    respx.get(GH.format(t="uscorp")).respond(200, json=greenhouse_body(["Austin, TX"], name="US Corp"))
    respx.get(GH.format(t="dead")).respond(404)

    async with client as c:
        result = await harvest.run_harvest(
            {"greenhouse": ["hiring", "uscorp", "dead"]},
            companies_path=companies_yaml,
            state_path=tmp_path / "state.jsonl",
            min_india_jobs=1,
            client=c,
        )

    assert result.added == 1
    names = {t.name for t in load_targets(companies_yaml)}
    assert "Hiring Co" in names and "US Corp" not in names
    assert "Sarvam AI" in names, "existing entries must survive"
    assert "a comment that must survive" in companies_yaml.read_text()


@respx.mock
async def test_already_tracked_tokens_are_not_reprobed(companies_yaml, tmp_path, client):
    allow_robots("https://api.ashbyhq.com")
    route = respx.get(AB.format(t="sarvam")).respond(200, json=ashby_body(["Bengaluru"]))

    async with client as c:
        result = await harvest.run_harvest(
            {"ashby": ["sarvam"]},
            companies_path=companies_yaml,
            state_path=tmp_path / "state.jsonl",
            client=c,
        )

    assert route.call_count == 0, "Sarvam is already in companies.yaml; do not spend a request"
    assert result.skipped_known == 1
    assert result.added == 0


@respx.mock
async def test_dry_run_writes_nothing_at_all(companies_yaml, tmp_path, client):
    allow_robots("https://boards-api.greenhouse.io")
    respx.get(GH.format(t="hiring")).respond(200, json=greenhouse_body(["Bengaluru"], name="Hiring Co"))
    before = companies_yaml.read_text()
    state = tmp_path / "state.jsonl"

    async with client as c:
        result = await harvest.run_harvest(
            {"greenhouse": ["hiring"]},
            companies_path=companies_yaml,
            state_path=state,
            min_india_jobs=1,
            dry_run=True,
            client=c,
        )

    assert result.added == 0
    assert len(result.hits(min_india_jobs=1)) == 1, "a dry run still reports what it would have added"
    assert companies_yaml.read_text() == before
    assert not state.exists(), "the resume log is a write too"


@respx.mock
async def test_min_india_jobs_filters_thin_boards(companies_yaml, tmp_path, client):
    allow_robots("https://boards-api.greenhouse.io")
    respx.get(GH.format(t="one")).respond(200, json=greenhouse_body(["Bengaluru"], name="One Role"))
    respx.get(GH.format(t="many")).respond(
        200, json=greenhouse_body(["Bengaluru"] * 4, name="Many Roles")
    )

    async with client as c:
        result = await harvest.run_harvest(
            {"greenhouse": ["one", "many"]},
            companies_path=companies_yaml,
            state_path=tmp_path / "state.jsonl",
            min_india_jobs=3,
            client=c,
        )

    assert {t.name for t in load_targets(companies_yaml)} >= {"Many Roles"}
    assert "One Role" not in {t.name for t in load_targets(companies_yaml)}
    assert result.added == 1


@respx.mock
async def test_limit_caps_the_sweep(companies_yaml, tmp_path, client):
    allow_robots("https://boards-api.greenhouse.io")
    first = respx.get(GH.format(t="a")).respond(200, json=greenhouse_body(["Bengaluru"], name="A"))
    second = respx.get(GH.format(t="b")).respond(200, json=greenhouse_body(["Bengaluru"], name="B"))

    async with client as c:
        await harvest.run_harvest(
            {"greenhouse": ["a", "b"]},
            companies_path=companies_yaml,
            state_path=tmp_path / "state.jsonl",
            limit=1,
            client=c,
        )

    assert first.call_count == 1 and second.call_count == 0


# --------------------------------------------------------------------------- #
# Resume
# --------------------------------------------------------------------------- #


@respx.mock
async def test_a_resumed_sweep_does_not_reprobe(companies_yaml, tmp_path, client):
    """A two-hour sweep will be interrupted. Restarting must not start over."""
    state = tmp_path / "state.jsonl"
    state.write_text(
        json.dumps(
            {"ats": "greenhouse", "token": "hiring", "live": True, "name": "Hiring Co",
             "total_jobs": 1, "india_jobs": 1, "error": None}
        )
        + "\n",
        encoding="utf-8",
    )
    allow_robots("https://boards-api.greenhouse.io")
    route = respx.get(GH.format(t="hiring")).respond(200, json=greenhouse_body(["Bengaluru"]))

    async with client as c:
        result = await harvest.run_harvest(
            {"greenhouse": ["hiring"]},
            companies_path=companies_yaml,
            state_path=state,
            min_india_jobs=1,
            client=c,
        )

    assert route.call_count == 0, "already probed: spend no request"
    assert result.resumed == 1
    assert result.added == 1, "a resumed hit is still appended"


def test_state_survives_a_truncated_final_line(tmp_path):
    """Killing the process mid-write leaves half a line. Read the rest anyway."""
    state = tmp_path / "state.jsonl"
    state.write_text(
        json.dumps({"ats": "lever", "token": "good", "live": True, "india_jobs": 2}) + "\n"
        + '{"ats": "lever", "token": "trunc"',
        encoding="utf-8",
    )
    loaded = harvest.load_state(state)
    assert ("lever", "good") in loaded
    assert len(loaded) == 1


def test_load_state_on_a_missing_file_is_empty(tmp_path):
    assert harvest.load_state(tmp_path / "nope.jsonl") == {}


# --------------------------------------------------------------------------- #
# Token lists
# --------------------------------------------------------------------------- #


def test_load_tokens_reads_the_expected_filenames(tmp_path):
    (tmp_path / "greenhouse_companies.json").write_text('["a", "b", " c ", ""]')
    (tmp_path / "lever_companies.json").write_text('["d"]')
    tokens = harvest.load_tokens(tmp_path, ["greenhouse", "lever", "ashby"])
    assert tokens["greenhouse"] == ["a", "b", "c"], "blanks dropped, whitespace stripped"
    assert tokens["lever"] == ["d"]
    assert "ashby" not in tokens, "a missing list is skipped, not an error"


def test_name_falls_back_to_the_token_when_the_listing_has_none():
    """Lever and Ashby listings carry no company name."""
    assert harvest.Probe(ats="lever", token="acme-corp").as_target().name == "Acme Corp"
