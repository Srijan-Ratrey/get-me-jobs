"""Export rows: collapsing repeat postings, flagging unverified scores, reachability."""

from __future__ import annotations

from jobhunter import db
from jobhunter.config import Target
from jobhunter.export import COLUMNS, collect_rows
from jobhunter.models import RawJob

FULL = "Python and machine learning. " * 80  # long enough to judge
WANTED = ["Bengaluru", "India", "Remote"]


def _add(session, company: str, title: str, location: str, score: int, description=FULL, n=1):
    target = db.upsert_company(session, Target(name=company))
    job, _ = db.upsert_job(
        session,
        target,
        RawJob(
            source="ashby",
            external_id=f"{company}-{title}-{location}-{n}",
            title=title,
            location=location,
            url=f"https://jobs.example/{company}/{n}",
            description=description,
        ),
    )
    job.fit_score = score
    return job


def test_collapse_folds_one_companys_postings_of_a_role(session):
    """Bjak lists one role once per country; the sheet should show it once."""
    _add(session, "Bjak", "Applied AI Engineer", "India", 87, n=1)
    _add(session, "Bjak", "Applied AI Engineer", "United Kingdom", 79, n=2)
    _add(session, "Bjak", "Applied AI Engineer", "Singapore", 79, n=3)
    _add(session, "Bjak", "Data Scientist", "India", 70, n=4)
    session.commit()

    rows = collect_rows(collapse=True)

    assert [(r["title"], r["postings"]) for r in rows] == [
        ("Applied AI Engineer", 3),
        ("Data Scientist", 1),
    ]
    assert rows[0]["location"] == "India"  # the best-scoring posting is the one kept
    assert set(rows[0]["other_locations"].split(" | ")) == {"United Kingdom", "Singapore"}
    assert len(collect_rows()) == 4  # off by default


def test_reachable_drops_postings_the_scorer_cannot_place(session):
    _add(session, "Bjak", "Applied AI Engineer", "India", 87, n=1)
    _add(session, "Bjak", "Applied AI Engineer", "United Kingdom", 79, n=2)
    session.commit()
    assert [r["location"] for r in collect_rows(location=WANTED)] == ["India"]


def test_unverified_marks_a_missing_description_and_sorts_after_a_verified_tie(session):
    _add(session, "Stub Co", "Data Engineer", "Bengaluru", 75, description="Apply now.", n=1)
    _add(session, "Real Co", "Data Engineer", "Bengaluru", 75, n=2)
    _add(session, "Better Co", "Data Engineer", "Bengaluru", 80, description=None, n=3)
    session.commit()

    rows = collect_rows()

    assert [(r["company"], r["unverified"]) for r in rows] == [
        ("Better Co", True),  # a higher score still ranks first; the flag says why to check it
        ("Real Co", False),
        ("Stub Co", True),
    ]
    assert COLUMNS[1] == "unverified" and set(COLUMNS) <= set(rows[0])
