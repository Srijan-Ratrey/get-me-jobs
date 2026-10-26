"""`jobhunter apply`: queue selection, label matching, and filling a local form.

No test opens a real site. The form tests render HTML shaped like the live
Greenhouse and Lever forms with `set_content`, which makes no network request,
and are skipped where Playwright (the optional `spa` extra) is not installed.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from jobhunter import apply, db
from jobhunter.config import Applicant, Profile, Target
from jobhunter.models import Application, Job, RawJob, utcnow


@pytest.fixture
def profile(tmp_path) -> Profile:
    resume = tmp_path / "cv.pdf"
    resume.write_bytes(b"%PDF-1.4")
    return Profile(
        titles=["Data Scientist"],
        locations=["Bengaluru", "India", "Remote"],
        min_score=55,
        applicant=Applicant(
            name="Ada Lovelace",
            email="ada@example.com",
            phone="+91 00000 00000",
            resume_path=str(resume),
            links=["https://linkedin.com/in/ada", "https://github.com/ada", "https://ada.dev"],
            location="Bengaluru",
            answers={"notice period|available to start": "Immediately"},
        ),
    )


def make_job(session, company: str, *, title: str = "Data Scientist", score: int = 90) -> Job:
    job, _ = db.upsert_job(
        session,
        db.upsert_company(session, Target(name=company)),
        RawJob(
            source="greenhouse",
            external_id=title,
            title=title,
            location="Bengaluru",
            url=f"https://example.com/{company}/{title}",
        ),
    )
    job.fit_score = score
    session.flush()
    return job


def gh(session, company: str, n: int, *, score: int = 90) -> Job:
    job = make_job(session, company, title=f"Data Scientist {n}", score=score)
    job.url = f"https://job-boards.greenhouse.io/{company.lower()}/jobs/{n}"
    session.flush()
    return job


# --------------------------------------------------------------------------- #
# Which page holds the form
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "url,expected",
    [
        (
            "https://job-boards.greenhouse.io/acme/jobs/1",
            "https://job-boards.greenhouse.io/acme/jobs/1",
        ),
        ("https://jobs.lever.co/acme/abc", "https://jobs.lever.co/acme/abc/apply"),
        ("https://jobs.lever.co/acme/abc/apply", "https://jobs.lever.co/acme/abc/apply"),
        (
            "https://jobs.ashbyhq.com/acme/x?src=1",
            "https://jobs.ashbyhq.com/acme/x/application?src=1",
        ),
        ("https://acme.wd1.myworkdayjobs.com/x/job/1", None),  # an account per employer
        (None, None),
    ],
)
def test_apply_url(url, expected):
    assert apply.apply_url(url) == expected


# --------------------------------------------------------------------------- #
# The queue
# --------------------------------------------------------------------------- #


def test_queue_is_one_per_employer_best_first_and_skips_handled_jobs(session, profile):
    gh(session, "Acme", 1, score=90)
    gh(session, "Acme", 2, score=95)
    handled = gh(session, "Beta", 3, score=99)
    gh(session, "Gamma", 4, score=70)
    session.add(Application(job_id=handled.id, status="skipped"))
    session.flush()

    picked = apply.queue(session, profile, limit=10)
    assert [(q.company, q.score) for q in picked] == [("Acme", 95), ("Gamma", 70)]


def test_queue_respects_the_daily_cap_and_the_company_cooldown(session, profile):
    for n in range(apply.DAILY_APPLY_CAP - 1):
        done = gh(session, f"Done{n}", n, score=90)
        session.add(Application(job_id=done.id, status="submitted"))
    recent = gh(session, "Acme", 100, score=90)
    session.add(
        Application(job_id=recent.id, status="submitted", decided_at=utcnow() - timedelta(days=30))
    )  # outside 24h, inside nothing
    gh(session, "Acme", 101, score=95)  # Acme's cooldown ended 16 days ago
    gh(session, "Beta", 102, score=80)
    session.flush()

    picked = apply.queue(session, profile, limit=10)
    assert len(picked) == 1, "nine submitted today leaves room for one"
    assert picked[0].company == "Acme"


def test_queue_leaves_out_unreachable_and_unsupported_jobs(session, profile):
    far = gh(session, "Far", 1, score=99)
    far.location = "San Francisco"
    wd = make_job(session, "Wd", score=98)
    wd.url = "https://wd.wd1.myworkdayjobs.com/x/job/1"
    gh(session, "Near", 2, score=60)
    session.flush()
    assert [q.company for q in apply.queue(session, profile, limit=10)] == ["Near"]


# --------------------------------------------------------------------------- #
# Labels -> values
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "label,expected",
    [
        ("First Name*", "Ada"),
        ("Last Name*", "Lovelace"),
        ("Full name✱", "Ada Lovelace"),
        ("Email*", "ada@example.com"),
        ("Phone", "+91 00000 00000"),
        ("LinkedIn Profile*", "https://linkedin.com/in/ada"),
        ("urls[GitHub]", "https://github.com/ada"),
        ("Website", "https://ada.dev"),
        ("Location (City)*", "Bengaluru"),
        ("What would be your Notice Period?*", "Immediately"),
        ("What are your salary expectations around the role?", None),  # never guessed
        ("Have you fine-tuned a model using parameter-efficient methods?", None),
        ("", None),
    ],
)
def test_answer_for(profile, label, expected):
    assert apply.answer_for(label, apply.field_rules(profile)) == expected


# --------------------------------------------------------------------------- #
# Filling a local form shaped like the live ones
# --------------------------------------------------------------------------- #

GREENHOUSE_LIKE = """
<form>
  <input id="first_name" aria-label="First Name" required>
  <input id="last_name" aria-label="Last Name" required>
  <input id="email" aria-label="Email" required>
  <input id="phone" aria-label="Phone" required>
  <input id="resume" type="file">
  <input id="question_1" aria-label="What would be your Notice Period?" required>
  <input id="question_2" aria-label="What are your salary expectations around the role?" required>
  <input id="question_3" aria-label="LinkedIn Profile" required>
  <button type="submit">Submit application</button>
</form>
"""


def test_fill_form_fills_what_it_knows_and_leaves_the_rest(profile):
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001 - browser binary not downloaded
            pytest.skip(f"chromium unavailable: {exc}")
        page = browser.new_page()
        page.set_content(GREENHOUSE_LIKE)
        page.on("request", lambda r: pytest.fail(f"unexpected request {r.url}"))

        filled, left = apply.fill_form(page, profile)

        assert page.input_value("#first_name") == "Ada"
        assert page.input_value("#question_1") == "Immediately"
        assert page.input_value("#question_2") == "", "salary must be left to the person"
        assert page.evaluate("document.querySelector('#resume').files.length") == 1
        assert (filled, left) == (7, 1)
        browser.close()
