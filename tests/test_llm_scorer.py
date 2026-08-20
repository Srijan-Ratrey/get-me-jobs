"""The optional LLM relevance pass, and the migration that makes room for it.

No test here reaches the network. The Anthropic client is replaced by a fake, so
the suite stays offline and runs without the `llm` extra installed.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import pytest

from jobhunter import db, pipeline
from jobhunter.config import Profile, Target
from jobhunter.matching import llm_scorer
from jobhunter.matching.llm_scorer import Verdict, build_messages, score_many, score_one
from jobhunter.models import Job, RawJob

JUNIOR = Profile(
    titles=["Data Scientist", "Machine Learning Engineer"],
    nice_to_have_keywords=["pytorch", "llm", "sql"],
    exclude_keywords=["senior", "staff", "lead"],
    locations=["Bengaluru", "India", "Remote"],
    max_years_experience=2,
)


# --------------------------------------------------------------------------- #
# A fake client, standing in for anthropic.AsyncAnthropic
# --------------------------------------------------------------------------- #


@dataclass
class FakeResponse:
    parsed_output: Verdict | None


class FakeMessages:
    def __init__(self, verdicts, error=None):
        self._verdicts = list(verdicts)
        self._error = error
        self.calls: list[dict] = []

    async def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self._error:
            raise self._error
        return FakeResponse(self._verdicts.pop(0) if self._verdicts else None)


class FakeClient:
    def __init__(self, verdicts=(), error=None):
        self.messages = FakeMessages(verdicts, error)


def verdict(fit=80, target=True, junior=True, reason="because") -> Verdict:
    return Verdict(
        is_target_role=target, is_junior_appropriate=junior, fit=fit, reason=reason
    )


JOB = {
    "id": 1,
    "company": "Acme",
    "title": "Machine Learning Engineer",
    "location": "Bengaluru",
    "description": "Train models in PyTorch.",
    "fit_score": 60,
}


# --------------------------------------------------------------------------- #
# Prompt construction
# --------------------------------------------------------------------------- #


def test_prompt_carries_the_profile_and_the_posting():
    system, user = build_messages(JOB, JUNIOR)
    assert "At most 2 years" in system
    assert "Machine Learning Engineer" in system  # target titles
    assert "pytorch" in system
    assert "Acme" in user and "Bengaluru" in user and "PyTorch" in user


def test_a_long_description_is_truncated_not_dropped():
    """Cost control must not silently discard the requirements section."""
    job = {**JOB, "description": "x" * (llm_scorer.DESCRIPTION_CHARS + 5000)}
    _, user = build_messages(job, JUNIOR)
    assert "[truncated]" in user
    assert len(user) < llm_scorer.DESCRIPTION_CHARS + 500


def test_a_missing_description_is_stated_not_blank():
    _, user = build_messages({**JOB, "description": None}, JUNIOR)
    assert "(no description provided)" in user


def test_prompt_survives_a_profile_with_nothing_set():
    system, _ = build_messages(JOB, Profile())
    assert "Early career" in system
    assert "any" in system


# --------------------------------------------------------------------------- #
# Scoring one, and failing safely
# --------------------------------------------------------------------------- #


async def test_score_one_returns_the_verdict():
    client = FakeClient([verdict(fit=88)])
    result = await score_one(client, JOB, JUNIOR)
    assert result.verdict is not None
    assert result.verdict.fit == 88
    assert result.error is None


async def test_score_one_requests_cheap_effort_and_a_bounded_response():
    client = FakeClient([verdict()])
    await score_one(client, JOB, JUNIOR, model="claude-opus-5")
    call = client.messages.calls[0]
    assert call["model"] == "claude-opus-5"
    assert call["output_format"] is Verdict
    assert call["output_config"] == {"effort": "low"}
    assert call["max_tokens"] == llm_scorer.MAX_TOKENS


async def test_an_api_failure_is_recorded_not_raised():
    """One bad posting among thousands must not end the run."""
    client = FakeClient(error=RuntimeError("503 overloaded"))
    result = await score_one(client, JOB, JUNIOR)
    assert result.verdict is None
    assert result.error == "RuntimeError"
    assert result.job_id == 1


async def test_an_unparseable_response_is_recorded_not_raised():
    client = FakeClient([None])
    result = await score_one(client, JOB, JUNIOR)
    assert result.verdict is None
    assert result.error == "unparseable"


async def test_a_verdict_cannot_carry_an_out_of_range_score():
    with pytest.raises(Exception):
        Verdict(is_target_role=True, is_junior_appropriate=True, fit=140, reason="x")


# --------------------------------------------------------------------------- #
# Scoring many
# --------------------------------------------------------------------------- #


async def test_score_many_scores_every_job_and_reports_progress():
    jobs = [{**JOB, "id": i} for i in range(5)]
    client = FakeClient([verdict(fit=i * 10) for i in range(5)])
    seen: list[int] = []
    results = await score_many(
        jobs, JUNIOR, client=client, concurrency=2, on_progress=lambda r: seen.append(r.job_id)
    )
    assert len(results) == 5
    assert len(seen) == 5
    assert len(client.messages.calls) == 5


async def test_score_many_on_an_empty_list_makes_no_calls():
    client = FakeClient()
    assert await score_many([], JUNIOR, client=client) == []
    assert client.messages.calls == []


async def test_one_failure_does_not_lose_the_other_results():
    jobs = [{**JOB, "id": i} for i in range(3)]
    client = FakeClient(error=ValueError("boom"))
    results = await score_many(jobs, JUNIOR, client=client)
    assert len(results) == 3
    assert all(r.verdict is None for r in results)


def test_the_default_model_is_not_silently_downgraded():
    """Cost is the user's call. A weak default reintroduces the false positives
    this module exists to remove."""
    assert llm_scorer.DEFAULT_MODEL == "claude-opus-5"


# --------------------------------------------------------------------------- #
# Candidate selection: the cost control that matters
# --------------------------------------------------------------------------- #


@pytest.fixture
def seeded(tmp_path):
    db.init_db(f"sqlite+pysqlite:///{tmp_path / 'x.db'}")
    with db.session_scope() as session:
        company = db.upsert_company(session, Target(name="Acme"))

        def add(title, location, disqualified=None, remote=False, external_id="1"):
            job, _ = db.upsert_job(
                session,
                company,
                RawJob(
                    source="greenhouse",
                    external_id=external_id,
                    title=title,
                    location=location,
                    url=f"https://x/{external_id}",
                    remote=remote,
                    description="Python",
                ),
            )
            job.fit_score = 60
            job.fit_reasons = {"total": 60, "disqualified": disqualified}
            return job

        add("Data Scientist", "Bengaluru", external_id="keep")
        add("Senior Data Scientist", "Bengaluru", disqualified="exclude_keyword:senior", external_id="dq")
        add("Data Scientist", "San Francisco, CA", external_id="usa")
        session.commit()
    yield


def test_candidates_exclude_disqualified_and_unreachable(seeded):
    titles = {c["title"] for c in pipeline.llm_candidates(JUNIOR)}
    assert titles == {"Data Scientist"}, "the senior one and the US one are not worth paying for"
    assert len(pipeline.llm_candidates(JUNIOR)) == 1


def test_candidates_skip_jobs_already_judged(seeded):
    results = [llm_scorer.Scored(job_id=c["id"], verdict=verdict()) for c in pipeline.llm_candidates(JUNIOR)]
    pipeline.apply_llm_scores(results)
    assert pipeline.llm_candidates(JUNIOR) == []
    assert len(pipeline.llm_candidates(JUNIOR, rescore=True)) == 1


def test_candidates_respects_limit(seeded):
    assert len(pipeline.llm_candidates(JUNIOR, limit=1)) == 1


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #


def test_apply_writes_beside_the_keyword_score_not_over_it(seeded):
    candidate = pipeline.llm_candidates(JUNIOR)[0]
    summary = pipeline.apply_llm_scores(
        [llm_scorer.Scored(job_id=candidate["id"], verdict=verdict(fit=91, reason="real ML role"))]
    )
    assert summary["written"] == 1

    with db.session_scope() as session:
        job = session.get(Job, candidate["id"])
        assert job.llm_score == 91
        assert job.llm_verdict["reason"] == "real ML role"
        assert job.fit_score == 60, "the keyword score must survive so the two can be compared"


def test_apply_counts_the_rejections_separately(seeded):
    candidate = pipeline.llm_candidates(JUNIOR)[0]
    summary = pipeline.apply_llm_scores(
        [
            llm_scorer.Scored(job_id=candidate["id"], verdict=verdict(fit=5, target=False)),
            llm_scorer.Scored(job_id=candidate["id"], verdict=verdict(fit=8, junior=False)),
            llm_scorer.Scored(job_id=candidate["id"], verdict=None, error="RuntimeError"),
        ]
    )
    assert summary == {"written": 2, "failed": 1, "off_target": 1, "too_senior": 1}


def test_apply_dry_run_writes_nothing(seeded):
    candidate = pipeline.llm_candidates(JUNIOR)[0]
    pipeline.apply_llm_scores(
        [llm_scorer.Scored(job_id=candidate["id"], verdict=verdict(fit=91))], dry_run=True
    )
    with db.session_scope() as session:
        assert session.get(Job, candidate["id"]).llm_score is None


# --------------------------------------------------------------------------- #
# The migration
# --------------------------------------------------------------------------- #


def test_columns_are_added_to_a_database_that_predates_them(tmp_path):
    """A real database holds thousands of postings and their first-seen dates,
    which rescanning cannot recover. Opening it must not require deleting it."""
    path = tmp_path / "old.db"

    # Build the schema as it was before llm_score existed, with a row in it.
    db.init_db(f"sqlite+pysqlite:///{path}")
    with db.session_scope() as session:
        company = db.upsert_company(session, Target(name="Acme"))
        db.upsert_job(
            session,
            company,
            RawJob(source="greenhouse", external_id="1", title="Data Scientist",
                   location="Bengaluru", url="https://x/1"),
        )
        session.commit()

    # Rebuild `jobs` without the new columns, so the table on disk looks like one
    # created by an earlier version of this package.
    raw = sqlite3.connect(path)
    kept = [
        r[1] for r in raw.execute("PRAGMA table_info(jobs)")
        if r[1] not in ("llm_score", "llm_verdict")
    ]
    raw.executescript(
        f"""
        CREATE TABLE jobs_old AS SELECT {", ".join(kept)} FROM jobs;
        DROP TABLE jobs;
        ALTER TABLE jobs_old RENAME TO jobs;
        """
    )
    raw.commit()
    cols = {r[1] for r in raw.execute("PRAGMA table_info(jobs)")}
    raw.close()
    assert "llm_score" not in cols, "precondition: the column is genuinely absent"

    db.init_db(f"sqlite+pysqlite:///{path}")

    check = sqlite3.connect(path)
    cols = {r[1] for r in check.execute("PRAGMA table_info(jobs)")}
    rows = check.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    check.close()
    assert {"llm_score", "llm_verdict"} <= cols
    assert rows == 1, "the existing posting must survive the migration"


def test_the_migration_is_idempotent(tmp_path):
    url = f"sqlite+pysqlite:///{tmp_path / 'y.db'}"
    for _ in range(3):
        db.init_db(url)
    with db.session_scope() as session:
        company = db.upsert_company(session, Target(name="Acme"))
        job, _ = db.upsert_job(
            session,
            company,
            RawJob(source="greenhouse", external_id="1", title="X", location="Y", url="https://x/1"),
        )
        job.llm_score = 50
        session.commit()
        assert session.get(Job, job.id).llm_score == 50
