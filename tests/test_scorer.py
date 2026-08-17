"""Fit scoring, including the three judgement calls that shape the output.

A score you cannot decompose is a score you cannot tune, so the reconciliation
test (components sum to total) matters as much as the boundary cases.
"""
from __future__ import annotations

import pytest

from jobhunter.config import Profile
from jobhunter.matching.scorer import (
    W_LOCATION,
    W_MUST_HAVE,
    W_NICE_TO_HAVE,
    W_SENIORITY,
    W_TITLE,
    min_years_required,
    score_job,
)
from jobhunter.models import Job


def make_job(**kw) -> Job:
    defaults = {
        "title": "Data Scientist",
        "canonical_title": "data scientist",
        "location": "Bengaluru",
        "remote": False,
        "description": "We use Python and machine learning.",
        "url": "https://x/1",
        "source": "test",
        "seniority": None,
    }
    return Job(**{**defaults, **kw})


JUNIOR = Profile(
    titles=["Data Scientist", "Machine Learning Engineer", "AI Engineer"],
    must_have_keywords=["python"],
    nice_to_have_keywords=["pytorch", "llm", "nlp", "sql", "docker", "gcp", "rag", "faiss"],
    exclude_keywords=["senior", "staff", "principal", "lead", "manager"],
    locations=["Bengaluru", "India", "Remote"],
    seniority=["junior", "intern"],
    max_years_experience=2,
)


def test_weights_sum_to_one_hundred():
    assert W_TITLE + W_MUST_HAVE + W_NICE_TO_HAVE + W_LOCATION + W_SENIORITY == 100


def test_components_always_reconcile_with_the_total():
    for job in (
        make_job(),
        make_job(title="Senior Data Scientist"),
        make_job(description="8+ years required"),
        make_job(location="Berlin", description="No python here"),
    ):
        score = score_job(job, JUNIOR)
        assert sum(score.components.values()) == score.total
        assert score.as_fit_reasons()["total"] == score.total


def test_perfect_match_scores_at_the_top():
    job = make_job(
        title="Data Scientist",
        seniority="junior",
        description="Python, machine learning, pytorch, llm, nlp, sql, docker, gcp, rag, faiss",
    )
    assert score_job(job, JUNIOR).total == 100


def test_empty_profile_gives_full_credit_without_dividing_by_zero():
    score = score_job(make_job(), Profile())
    assert score.total == 100
    assert score.disqualified is None


# --------------------------------------------------------------------------- #
# Decision 1: exclude_keywords match the TITLE only
# --------------------------------------------------------------------------- #


def test_excluded_title_is_a_hard_zero():
    score = score_job(make_job(title="Senior Data Scientist"), JUNIOR)
    assert score.total == 0
    assert score.disqualified == "exclude_keyword:senior"
    assert all(v == 0 for v in score.components.values())


def test_exclude_keywords_are_not_matched_against_the_description():
    """A junior posting mentioning senior colleagues must survive.

    This is the difference between a usable shortlist and an empty one: plenty of
    genuinely junior ads say "you'll work alongside senior engineers".
    """
    job = make_job(
        title="Data Scientist",
        description="Python and machine learning. You will pair with senior engineers "
        "and report to the engineering manager.",
    )
    score = score_job(job, JUNIOR)
    assert score.disqualified is None
    assert score.total > 0


def test_exclude_matching_is_whole_word():
    """"lead" must not match "leadership", "sr" must not match "srinagar"."""
    profile = Profile(titles=["Engineer"], exclude_keywords=["lead", "sr"])
    assert score_job(make_job(title="Leadership Engineer"), profile).disqualified is None
    assert score_job(make_job(title="Lead Engineer"), profile).disqualified is not None


# --------------------------------------------------------------------------- #
# Decision 2: years of experience is a gate, read as the minimum stated
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text,expected",
    [
        ("5+ years of experience", 5),
        ("3-5 years", 3),
        ("3–5 years", 3),  # en dash
        ("3—5 years", 3),  # em dash
        ("2 to 4 years", 2),
        ("at least 2 yrs", 2),
        ("4+ YOE", 4),
        ("0–3 years welcome", None),  # explicitly open to freshers
        ("founded in 2019 and grew 10x", None),
        ("no numbers here", None),
        ("", None),
        (None, None),
    ],
)
def test_min_years_required(text, expected):
    assert min_years_required(text) == expected


def test_years_gate_uses_the_smallest_figure_mentioned():
    """Postings bundle several roles' requirements; the max would over-reject."""
    job = make_job(description="Python. Junior track: 1+ years. Senior track: 8+ years.")
    assert score_job(job, JUNIOR).disqualified is None


def test_too_many_years_is_a_hard_zero_even_with_a_neutral_title():
    job = make_job(title="Data Scientist", description="Python. We need 8+ years of experience.")
    score = score_job(job, JUNIOR)
    assert score.total == 0
    assert score.disqualified == "years_required:8"


def test_years_at_the_cap_is_accepted():
    job = make_job(description="Python and ML, 2+ years of experience")
    assert score_job(job, JUNIOR).disqualified is None


# --------------------------------------------------------------------------- #
# Decision 3: an unmarked title gets partial seniority credit
# --------------------------------------------------------------------------- #


def test_unmarked_seniority_gets_partial_not_zero():
    score = score_job(make_job(seniority=None), JUNIOR)
    assert 0 < score.components["seniority"] < W_SENIORITY


def test_wanted_seniority_gets_full_credit():
    assert score_job(make_job(seniority="junior"), JUNIOR).components["seniority"] == W_SENIORITY


def test_unwanted_seniority_gets_zero():
    # "mid" is not in the profile's wanted levels, but is not an excluded title.
    assert score_job(make_job(seniority="mid"), JUNIOR).components["seniority"] == 0


# --------------------------------------------------------------------------- #
# Location aliasing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "location", ["Bengaluru", "Bangalore", "Hybrid in Bangalore, India", "BLR", "Bangalore, Karnataka"]
)
def test_bengaluru_and_bangalore_are_the_same_city(location):
    """ATS boards use both spellings, sometimes in one response."""
    assert score_job(make_job(location=location), JUNIOR).components["location"] == W_LOCATION


def test_unwanted_location_scores_zero():
    assert score_job(make_job(location="Buenos Aires, Argentina"), JUNIOR).components["location"] == 0


def test_remote_makes_location_moot_only_when_it_is_not_region_locked():
    """Remote is not a free pass.

    This test used to assert that any remote posting earned the full location
    weight. It does not: a remote role anchored to Argentina or the US is remote
    *within that region*, and scoring it 10/10 floated unreachable roles above
    reachable ones. Only a remote posting naming no other region qualifies.
    """
    anchored = make_job(location="Buenos Aires, Argentina", remote=True)
    assert score_job(anchored, JUNIOR).components["location"] == 0

    unanchored = make_job(location="Remote", remote=True)
    assert score_job(unanchored, JUNIOR).components["location"] == W_LOCATION


def test_remote_only_profile_rejects_onsite():
    profile = Profile(titles=["Data Scientist"], remote_only=True)
    assert score_job(make_job(remote=False), profile).components["location"] == 0
    assert score_job(make_job(remote=True), profile).components["location"] == W_LOCATION


# --------------------------------------------------------------------------- #
# Keyword matching
# --------------------------------------------------------------------------- #


def test_keywords_match_whole_words_only():
    """"gan" must not match "organization", nor "lora" match "exploration"."""
    profile = Profile(titles=["Data Scientist"], must_have_keywords=[], nice_to_have_keywords=["gan", "lora"])
    job = make_job(description="Our organization values exploration.")
    assert score_job(job, profile).components["nice_to_have"] == 0

    job = make_job(description="We train GANs with LoRA adapters.")
    assert score_job(job, profile).components["nice_to_have"] > 0


def test_missing_must_have_is_not_a_hard_zero():
    """Only exclude_keywords hard-zero; a missing must-have costs its component."""
    job = make_job(description="We use R and Julia.")
    score = score_job(job, JUNIOR)
    assert score.components["must_have"] == 0
    assert score.total > 0
    assert score.disqualified is None


def test_partial_title_overlap_earns_partial_credit():
    score = score_job(make_job(title="Data Science Associate"), JUNIOR)
    assert 0 < score.components["title"] < W_TITLE


def test_unrelated_title_earns_nothing():
    score = score_job(make_job(title="Account Executive"), JUNIOR)
    assert score.components["title"] == 0


# --------------------------------------------------------------------------- #
# Location filtering (separate from location scoring)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "location,remote,expected",
    [
        # Wanted place, named.
        ("Bengaluru", False, True),
        ("Bangalore, Karnataka", False, True),
        ("Hybrid in Bangalore, India", False, True),
        ("Remote - India", True, True),
        # Genuinely open to anyone.
        ("Remote (Anywhere)", True, True),
        ("Worldwide", True, True),
        (None, True, True),
        ("Remote", True, True),
        # Remote, but tied to somewhere else: not reachable from Bengaluru.
        ("USA | Remote", True, False),
        ("Remote - EU", True, False),
        ("United Kingdom", True, False),
        ("Remote, United States", True, False),
        # Plainly elsewhere.
        ("San Francisco, California", False, False),
        ("Buenos Aires, Argentina", False, False),
        ("Berkeley, California, United States", False, False),
    ],
)
def test_matches_location(location, remote, expected):
    """A remote job is not automatically reachable: "USA | Remote" is US-remote."""
    from jobhunter.matching.scorer import matches_location

    assert matches_location(location, remote, ["Bengaluru", "India"]) is expected


def test_matches_location_with_no_preference_accepts_everything():
    from jobhunter.matching.scorer import matches_location

    assert matches_location("San Francisco", False, []) is True


# --------------------------------------------------------------------------- #
# "Remote" in the wanted list is a modality, not a place
# --------------------------------------------------------------------------- #

# The real profile.yaml list. Every case above passes ["Bengaluru", "India"] —
# a list that omits "Remote" — which is exactly why this bug survived the suite:
# adding "Remote" put the bare word into the alias set, so the substring test
# matched "USA | Remote" and 397 unreachable postings entered the shortlist.
WANTED_WITH_REMOTE = ["Bengaluru", "India", "Remote"]


@pytest.mark.parametrize(
    "location,remote,expected",
    [
        # Reachable.
        ("Bengaluru", False, True),
        ("Bangalore, Karnataka", False, True),
        ("Remote - India", True, True),
        ("Remote", True, True),
        ("Remote (Anywhere)", True, True),
        (None, True, True),
        # Region-locked remote: listing "Remote" must not make these reachable.
        ("USA | Remote", True, False),
        ("Remote - California", True, False),
        ("Remote-Friendly, United States; San Francisco, CA", True, False),
        ("Remote - EU", True, False),
        ("Remote, United States", True, False),
        ("Remote - United Kingdom", True, False),
        # The remote flag is set on plenty of onsite US postings in practice.
        ("San Francisco, CA", True, False),
    ],
)
def test_listing_remote_does_not_make_every_remote_job_reachable(location, remote, expected):
    from jobhunter.matching.scorer import matches_location

    assert matches_location(location, remote, WANTED_WITH_REMOTE) is expected


@pytest.mark.parametrize("location", ["Indianapolis, Indiana", "Indiana, United States"])
def test_india_does_not_match_indiana(location):
    """Substring aliasing filed US postings under an India-only shortlist."""
    from jobhunter.matching.scorer import matches_location

    assert matches_location(location, False, WANTED_WITH_REMOTE) is False


def test_scoring_and_filtering_agree_on_location():
    """The two used to run separate checks and disagree.

    Scoring awarded the full 10 for any posting mentioning "remote", so US-only
    roles outranked reachable Bengaluru ones. Whatever the filter rejects must
    not be collecting location points.
    """
    from jobhunter.matching.scorer import matches_location

    profile = Profile(titles=["Data Scientist"], locations=WANTED_WITH_REMOTE)
    for location, remote in [
        ("USA | Remote", True),
        ("Remote - California", True),
        ("Bengaluru", False),
        ("Remote", True),
    ]:
        job = make_job(location=location, remote=remote)
        reachable = matches_location(location, remote, WANTED_WITH_REMOTE)
        awarded = score_job(job, profile).components["location"] == W_LOCATION
        assert awarded is reachable, f"{location!r}: scored {awarded}, filtered {reachable}"


# --------------------------------------------------------------------------- #
# must-have alternatives
# --------------------------------------------------------------------------- #


def test_must_have_is_satisfied_by_any_pipe_separated_alternative():
    """One unwritten word must not forfeit the whole must-have weight.

    71% of otherwise-qualifying postings scored 0/25 because the description
    never typed "python", including ML roles whose text was all PyTorch.
    """
    profile = Profile(titles=["Data Scientist"], must_have_keywords=["python|pytorch|pandas"])

    job = make_job(description="We train models in PyTorch on a large cluster.")
    score = score_job(job, profile)
    assert score.components["must_have"] == W_MUST_HAVE
    # The reason must name what actually matched, not the group.
    assert any("pytorch" in r for r in score.reasons)
    assert not any("'python'" in r for r in score.reasons)

    absent = make_job(description="We work in Rust and Go.")
    assert score_job(absent, profile).components["must_have"] == 0


def test_must_have_without_alternatives_is_unchanged():
    """No pipe means exactly the old behaviour."""
    profile = Profile(titles=["Data Scientist"], must_have_keywords=["python"])
    assert score_job(make_job(description="Python here"), profile).components["must_have"] == W_MUST_HAVE
    assert score_job(make_job(description="PyTorch only"), profile).components["must_have"] == 0


def test_must_have_alternatives_still_match_whole_words():
    profile = Profile(titles=["Data Scientist"], must_have_keywords=["python|numpy"])
    assert score_job(make_job(description="pythonic numpyish"), profile).components["must_have"] == 0


def test_partial_must_have_credit_is_proportional_across_groups():
    profile = Profile(
        titles=["Data Scientist"], must_have_keywords=["python|pytorch", "sql|bigquery"]
    )
    job = make_job(description="We use pandas and pytorch daily.")
    assert score_job(job, profile).components["must_have"] == round(W_MUST_HAVE / 2)


def test_the_real_profile_still_scores_a_perfect_job_at_one_hundred():
    """Guards the shipped profile.yaml against a typo in the pipe group."""
    from jobhunter.config import load_profile

    profile = load_profile("profile.yaml")
    job = make_job(
        title="Machine Learning Engineer",
        location="Bengaluru",
        seniority="junior",
        description=(
            "Python, machine learning, deep learning, pytorch, llm, nlp, sql, docker, "
            "rag, embeddings, statistics, mlops"
        ),
    )
    assert score_job(job, profile).total == 100
