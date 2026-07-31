"""Normalization and dedup: canonical_title, compute_hash, seniority, remote.

The hash is what decides whether two postings are the same opening. Getting it
wrong in one direction shows a duplicate; wrong in the other silently loses a job
you could have applied to. The second failure is the one these tests are for.
"""
from __future__ import annotations

from jobhunter.models import RawJob, hash_email


def job(title: str, location: str | None = None, **kw) -> RawJob:
    return RawJob(source=kw.pop("source", "test"), title=title, location=location, url="https://x/1", **kw)


# --------------------------------------------------------------------------- #
# canonical_title
# --------------------------------------------------------------------------- #


def test_strips_location_and_gender_tags():
    assert job("Senior Backend Engineer (Remote) - Berlin", "Berlin, Germany").canonical_title() == (
        "senior backend engineer"
    )
    assert job("Senior Backend Engineer m/f/d", "Berlin").canonical_title() == (
        "senior backend engineer"
    )
    assert job("Data Scientist (m/w/d)", "Munich").canonical_title() == "data scientist"
    assert job("ML Engineer [US]", "New York").canonical_title() == "ml engineer"


def test_keeps_role_discriminators():
    """The qualifier after the dash is usually the role, not the place."""
    assert job("Engineering Manager - Platform Team", "Bangalore").canonical_title() == (
        "engineering manager platform team"
    )
    assert job("ML Engineer (Training Infra), Foundational Models", "Bengaluru").canonical_title() == (
        "ml engineer training infra foundational models"
    )
    assert job("Site Reliability Engineer (4+ YOE)", "Bangalore").canonical_title() == (
        "site reliability engineer 4+ yoe"
    )
    assert job("AM/ Manager - Risk & Decision Science", "Bangalore").canonical_title() == (
        "am manager risk decision science"
    )


def test_strips_city_named_in_title_when_it_matches_the_location():
    assert job("Engineering Manager - Bangalore", "Bangalore").canonical_title() == (
        "engineering manager"
    )
    assert job("Data Scientist, Bengaluru", "Bengaluru").canonical_title() == "data scientist"


def test_strips_recognised_cities_even_when_location_differs():
    # Greenhouse reports this posting as Bangalore; the title says Pune.
    assert job("Engineering Manager - Pune", "Bangalore").canonical_title() == "engineering manager"


def test_handles_zero_width_characters():
    assert job("Data​Scientist – Analytics​", "Bengaluru").canonical_title() == (
        "datascientist analytics"
    )


# --------------------------------------------------------------------------- #
# compute_hash
# --------------------------------------------------------------------------- #


def test_same_role_across_sources_collapses():
    """The acceptance criterion from TASKS.md 1.3."""
    a = job("Senior Backend Engineer (Remote) - Berlin", "Berlin, Germany", source="greenhouse")
    b = job("Senior Backend Engineer m/f/d", "Berlin, Germany", source="lever")
    assert a.compute_hash("Acme") == b.compute_hash("Acme")


def test_hash_ignores_source_and_external_id():
    a = job("Data Scientist", "Bengaluru", source="greenhouse", external_id="111")
    b = job("Data Scientist", "Bengaluru", source="ashby", external_id="zzz")
    assert a.compute_hash("Acme") == b.compute_hash("Acme")


def test_distinct_roles_get_distinct_hashes():
    a = job("Engineering Manager - Platform Team", "Bangalore")
    b = job("Engineering Manager - Payments", "Bangalore")
    assert a.compute_hash("PhonePe") != b.compute_hash("PhonePe")


def test_same_title_in_different_cities_stays_distinct():
    """Both arrive from Greenhouse with location "Bangalore"; only the title differs.

    Without the title-location hint in the hash these collide and one of two real
    openings disappears.
    """
    blr = job("Engineering Manager - Bangalore", "Bangalore")
    pune = job("Engineering Manager - Pune", "Bangalore")
    assert blr.compute_hash("PhonePe") != pune.compute_hash("PhonePe")
    assert pune._title_location_hints() == ["pune"]
    assert blr._title_location_hints() == []


def test_remote_does_not_create_a_phantom_variant():
    """"(Remote)" must not split one posting into two rows."""
    a = job("Data Scientist (Remote)", "Bengaluru")
    b = job("Data Scientist", "Bengaluru")
    assert a.compute_hash("Acme") == b.compute_hash("Acme")


def test_different_companies_never_collide():
    a = job("Data Scientist", "Bengaluru")
    assert a.compute_hash("Acme") != a.compute_hash("Globex")


# --------------------------------------------------------------------------- #
# seniority and remote
# --------------------------------------------------------------------------- #


def test_detect_seniority_from_title():
    assert job("Senior Data Scientist").detect_seniority() == "senior"
    assert job("Junior ML Engineer").detect_seniority() == "junior"
    assert job("Staff Engineer").detect_seniority() == "staff"
    assert job("Engineering Manager").detect_seniority() == "lead"
    assert job("Data Science Intern").detect_seniority() == "intern"
    # No level word at all: the common case for junior-appropriate postings.
    assert job("Data Scientist").detect_seniority() is None


def test_source_seniority_hint_beats_the_title():
    assert job("Data Scientist", seniority_hint="Entry level").detect_seniority() == "junior"
    assert job("Data Scientist", seniority_hint="Mid-Senior level").detect_seniority() == "senior"


def test_detect_remote_prefers_the_source_flag():
    # Ashby's isRemote / Lever's workplaceType are authoritative.
    assert job("Data Scientist", "Bengaluru", remote=True).detect_remote() is True
    assert job("Data Scientist (Remote)", "Remote", remote=False).detect_remote() is False
    # No flag: fall back to text.
    assert job("Data Scientist", "Remote - India").detect_remote() is True
    assert job("Data Scientist", "Bengaluru").detect_remote() is False


# --------------------------------------------------------------------------- #
# hash_email
# --------------------------------------------------------------------------- #


def test_hash_email_normalizes():
    assert hash_email("Careers@Acme.com") == hash_email("  careers@acme.com  ")
    assert hash_email("a@x.com") != hash_email("b@x.com")
    assert len(hash_email("a@x.com")) == 64
