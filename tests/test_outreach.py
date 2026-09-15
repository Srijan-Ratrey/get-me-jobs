"""The gates that make an unattended sender defensible.

Every test here is a rule from docs/compliance.md made executable. The sender
runs from a scheduler with nobody watching, so these are the only thing between
it and a spam operation, and each one exists because the alternative was
measured in the real database rather than imagined.
"""
from __future__ import annotations

from datetime import timedelta
from email.message import EmailMessage

import pytest

from jobhunter import db
from jobhunter.config import Applicant, Profile, Target, settings
from jobhunter.models import Contact, Job, Outreach, RawJob, Suppression, hash_email, utcnow
from jobhunter.outreach import policy
from jobhunter.outreach.drafter import MIN_SPECIFIC_SKILLS, Draft, Refusal, draft_for, preflight
from jobhunter.outreach.sender import build_message, send_batch


@pytest.fixture
def session(tmp_path):
    db.init_db(f"sqlite+pysqlite:///{tmp_path / 'test.db'}")
    with db.session_scope() as s:
        yield s


@pytest.fixture
def resume(tmp_path):
    path = tmp_path / "resume.pdf"
    path.write_bytes(b"%PDF-1.4 not really a pdf")
    return path


@pytest.fixture
def profile(resume) -> Profile:
    return Profile(
        titles=["ML Engineer", "Data Scientist"],
        nice_to_have_keywords=["pytorch", "nlp", "rag", "sql", "docker"],
        min_score=55,
        applicant=Applicant(
            name="Ada Lovelace",
            email="ada@example.com",
            phone="+91 00000 00000",
            resume_path=str(resume),
            links=["https://github.com/ada"],
        ),
    )


class FakeTransport:
    """Records what would have gone out. Raises on demand, never opens a socket."""

    def __init__(self, fail_with: Exception | None = None) -> None:
        self.sent: list[EmailMessage] = []
        self.fail_with = fail_with

    def send(self, message: EmailMessage) -> str:
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append(message)
        return f"gmail-{len(self.sent)}"


def make_job(session, company_name: str, *, title="ML Engineer", score=90,
             description="We use PyTorch and RAG for NLP.", external_id=None, url=None) -> Job:
    company = db.upsert_company(session, Target(name=company_name))
    job, _ = db.upsert_job(
        session,
        company,
        RawJob(
            source="greenhouse",
            external_id=external_id or f"{company_name}-{title}",
            title=title,
            location="Bengaluru",
            url=url or f"https://example.com/{company_name}/{title}".replace(" ", "-"),
            description=description,
        ),
    )
    job.fit_score = score
    session.flush()
    return job


def make_contact(session, company_name: str, email: str, *, kind="role", confidence=0.95) -> Contact:
    company = db.upsert_company(session, Target(name=company_name))
    contact = Contact(
        company_id=company.id,
        email=email,
        kind=kind,
        source_url="https://example.com/careers",
        discovery_method="manual",
        confidence=confidence,
        verify_status="unknown",
        suppressed=False,
    )
    session.add(contact)
    session.flush()
    return contact


# --------------------------------------------------------------------------- #
# The drafter refuses rather than writing a form letter
# --------------------------------------------------------------------------- #


def test_a_posting_we_know_nothing_about_gets_no_email(session, profile):
    """compliance.md: a draft that reads identically with the company swapped is a bug.

    Enforced rather than hoped for. A posting whose text matches nothing in the
    profile cannot produce a specific message, and a generic one is the mail
    merge this project exists not to be, so the correct output is no message.
    """
    job = make_job(session, "Opaque Inc", description="A great opportunity. Apply now.")
    contact = make_contact(session, "Opaque Inc", "careers@opaque.com")
    result = draft_for(job, contact, job.company, profile)

    assert isinstance(result, Refusal)
    assert "specific" in result.reason


def test_a_draft_names_what_this_posting_actually_asked_for(session, profile):
    job = make_job(session, "Acme", description="You will work in PyTorch on RAG and NLP systems.")
    contact = make_contact(session, "Acme", "careers@acme.com")
    result = draft_for(job, contact, job.company, profile)

    assert isinstance(result, Draft)
    assert "Acme" in result.body
    assert "ML Engineer" in result.body
    # The specifics have to come from the posting, not the template.
    assert "pytorch" in result.body.lower()
    assert len(result.skills) >= MIN_SPECIFIC_SKILLS
    # compliance.md requires an opt-out and honest identification.
    assert "follow up" in result.body.lower()
    assert "Ada Lovelace" in result.body
    assert "ada@example.com" in result.body


def test_two_companies_do_not_get_the_same_body(session, profile):
    """The literal test compliance.md describes."""
    a = make_job(session, "Alpha", description="PyTorch and RAG work.")
    b = make_job(session, "Beta", title="Data Scientist", description="SQL and Docker work.")
    da = draft_for(a, make_contact(session, "Alpha", "careers@alpha.com"), a.company, profile)
    dbf = draft_for(b, make_contact(session, "Beta", "careers@beta.com"), b.company, profile)

    assert isinstance(da, Draft) and isinstance(dbf, Draft)
    assert da.body != dbf.body
    # And not merely because the company name differs.
    assert da.body.replace("Alpha", "X") != dbf.body.replace("Beta", "X")


def test_the_body_reads_like_a_person_wrote_it(session, profile):
    """Every assertion here is something the first real render got wrong.

    The pipeline stores keywords lowercase and locations as whatever the ATS
    had, and dropping both straight into prose produced "in bengaluru, in" and
    "llm and lora" -- which announces a script louder than saying nothing.
    """
    profile.nice_to_have_keywords = ["llm", "lora", "recommendation", "pytorch"]
    job = make_job(
        session, "Bosch Group", title="Data Scientist",
        description="Work on LLM and LoRA fine-tuning with PyTorch for recommendation.",
    )
    job.location = "bengaluru, in"   # exactly as SmartRecruiters stores it
    session.flush()
    result = draft_for(job, make_contact(session, "Bosch Group", "hr@bosch.test"), job.company, profile)

    assert isinstance(result, Draft)
    assert "in Bengaluru." in result.body, "location was not cleaned up"
    assert "bengaluru, in" not in result.body
    assert "LLM" in result.body and "llm and" not in result.body
    assert "LoRA" in result.body
    # Concrete technologies beat vague ones for the limited slots.
    assert "recommendation" not in result.body


def test_a_multi_office_location_names_one_city(session, profile):
    job = make_job(session, "Google", description="PyTorch and NLP and RAG.")
    job.location = "Bengaluru, Karnataka, India; Hyderabad, Telangana, India"
    session.flush()
    result = draft_for(job, make_contact(session, "Google", "hr@google.test"), job.company, profile)

    assert isinstance(result, Draft)
    assert "in Bengaluru." in result.body
    assert "Telangana" not in result.body, "dumped the whole location list into a sentence"


def test_a_must_have_can_supply_the_specifics(session, profile):
    """Stub descriptions are common, and "Python" is still something true.

    Over all 1,307 open postings scoring 55+: nice-to-haves alone leave 598
    (45%) draftable, must-haves included take it to 725 (55%). The refused rest
    are short, not bad -- the 90+ band is 100% draftable at a median 4,852-char
    description, the 70-89 band 55% at a median 950.
    """
    profile.nice_to_have_keywords = ["pytorch"]
    profile.must_have_keywords = ["python|pytorch", "sql|bigquery"]
    job = make_job(session, "Acme", description="You will write Python and SQL here.")
    result = draft_for(job, make_contact(session, "Acme", "hr@acme.test"), job.company, profile)

    assert isinstance(result, Draft)
    assert "Python" in result.body and "SQL" in result.body


def test_an_incomplete_applicant_blocks_every_draft(session, resume):
    """No anonymous mail, and none without a CV to attach."""
    bare = Profile(nice_to_have_keywords=["pytorch", "nlp"], applicant=Applicant(name="Ada"))
    job = make_job(session, "Acme", description="PyTorch and NLP.")
    result = draft_for(job, make_contact(session, "Acme", "c@acme.com"), job.company, bare)
    assert isinstance(result, Refusal)
    assert "incomplete" in result.reason


@pytest.mark.parametrize(
    "subject,body,expected",
    [
        ("", "x" * 300 + "\n\nmore", "empty subject"),
        ("Hi", "", "empty body"),
        ("Application: {title}", "x" * 300 + "\n\nmore", "placeholder"),
        ("Hi", "too short", "collapsed"),
    ],
)
def test_preflight_catches_a_collapsed_template(subject, body, expected):
    problem = preflight(subject, body)
    assert problem is not None and expected in problem


# --------------------------------------------------------------------------- #
# The daily cap
# --------------------------------------------------------------------------- #


def test_the_daily_cap_is_counted_from_the_database_not_the_process(session, profile, monkeypatch):
    """A cron double-fire, a retry, or two terminals must share one budget."""
    monkeypatch.setattr(settings, "daily_send_cap", 2)
    for n in range(5):
        make_job(session, f"Co{n}", description="PyTorch and RAG.")
        make_contact(session, f"Co{n}", f"careers@co{n}.com")

    transport = FakeTransport()
    first = send_batch(session, profile=profile, transport=transport, pause=False)
    assert first.sent == 2

    # Same day, second invocation: the budget is already spent.
    second = send_batch(session, profile=profile, transport=transport, pause=False)
    assert second.sent == 0
    assert len(transport.sent) == 2
    assert any("cap" in r for r in second.reasons)


def test_the_cap_cannot_be_raised_past_the_code_ceiling(monkeypatch):
    monkeypatch.setattr(settings, "daily_send_cap", 5000)
    assert policy.daily_cap() == settings.hard_daily_send_ceiling


def test_the_window_rolls_rather_than_resetting_at_midnight(session, profile, monkeypatch):
    monkeypatch.setattr(settings, "daily_send_cap", 1)
    make_job(session, "Acme", description="PyTorch and RAG.")
    contact = make_contact(session, "Acme", "careers@acme.com")
    session.add(
        Outreach(job_id=make_job(session, "Old", description="PyTorch and RAG.").id,
                 contact_id=contact.id, subject="s", body="b", status="sent",
                 sent_at=utcnow() - timedelta(hours=25))
    )
    session.flush()
    # The 25-hour-old send has aged out, so the budget is free again.
    assert policy.sent_recently(session) == 0


# --------------------------------------------------------------------------- #
# Cooldowns: the measured reason this design exists
# --------------------------------------------------------------------------- #


def test_one_inbox_under_two_company_names_is_mailed_once(session, profile, monkeypatch):
    """The Zensar case, measured: 594 shortlist rows over 379 companies.

    "Zensar" and "Zensar Technologies" are two company rows sharing one HR
    address. A cooldown keyed on contact_id would let both through; keyed on the
    address, the second is refused. Without this the real database would have
    sent twenty-two messages to one inbox in a morning.
    """
    monkeypatch.setattr(settings, "daily_send_cap", 10)
    make_job(session, "Zensar", title="ML Engineer", description="PyTorch and RAG.")
    make_job(session, "Zensar Technologies", title="Data Scientist", description="SQL and Docker.")
    make_contact(session, "Zensar", "hr@zensar.com")
    make_contact(session, "Zensar Technologies", "hr@zensar.com")

    transport = FakeTransport()
    report = send_batch(session, profile=profile, transport=transport, pause=False)

    assert report.sent == 1, "the same inbox was mailed twice under two company names"
    assert len(transport.sent) == 1


def test_a_company_inside_its_cooldown_is_skipped(session, profile, monkeypatch):
    monkeypatch.setattr(settings, "daily_send_cap", 10)
    job = make_job(session, "Acme", description="PyTorch and RAG.")
    contact = make_contact(session, "Acme", "careers@acme.com")
    session.add(
        Outreach(job_id=job.id, contact_id=contact.id, subject="s", body="b",
                 status="sent", sent_at=utcnow() - timedelta(days=2))
    )
    session.flush()

    decision = policy.may_send(session, policy.Candidate(job=job, contact=contact, company=job.company))
    assert not decision
    assert "cooldown" in decision.reason or "already" in decision.reason


def test_only_one_job_per_company_per_run(session, profile, monkeypatch):
    """A company with three open roles gets one email, not three."""
    monkeypatch.setattr(settings, "daily_send_cap", 10)
    for title in ("ML Engineer", "Data Scientist", "NLP Engineer"):
        make_job(session, "Acme", title=title, description="PyTorch and RAG and NLP.")
    make_contact(session, "Acme", "careers@acme.com")

    transport = FakeTransport()
    report = send_batch(session, profile=profile, transport=transport, pause=False)
    assert report.sent == 1


# --------------------------------------------------------------------------- #
# Suppression, closure, and erasure
# --------------------------------------------------------------------------- #


def test_a_suppressed_address_is_never_mailed(session, profile, monkeypatch):
    monkeypatch.setattr(settings, "daily_send_cap", 10)
    make_job(session, "Acme", description="PyTorch and RAG.")
    make_contact(session, "Acme", "careers@acme.com")
    session.add(Suppression(email_hash=hash_email("careers@acme.com")))
    session.flush()

    transport = FakeTransport()
    report = send_batch(session, profile=profile, transport=transport, pause=False)
    assert report.sent == 0
    assert transport.sent == []


def test_a_contact_flagged_suppressed_is_never_mailed(session, profile, monkeypatch):
    monkeypatch.setattr(settings, "daily_send_cap", 10)
    job = make_job(session, "Acme", description="PyTorch and RAG.")
    contact = make_contact(session, "Acme", "careers@acme.com")
    contact.suppressed = True
    session.flush()

    decision = policy.may_send(session, policy.Candidate(job=job, contact=contact, company=job.company))
    assert not decision and "suppressed" in decision.reason


def test_a_job_that_closed_after_drafting_is_dropped_at_send_time(session, profile):
    """The batch is chosen up to forty minutes before the last send goes out."""
    job = make_job(session, "Acme", description="PyTorch and RAG.")
    contact = make_contact(session, "Acme", "careers@acme.com")
    job.closed_at = utcnow()
    session.flush()

    decision = policy.may_send(session, policy.Candidate(job=job, contact=contact, company=job.company))
    assert not decision and "closed" in decision.reason


# --------------------------------------------------------------------------- #
# Sending mechanics
# --------------------------------------------------------------------------- #


def test_dry_run_sends_nothing_and_writes_nothing(session, profile, monkeypatch):
    monkeypatch.setattr(settings, "daily_send_cap", 10)
    make_job(session, "Acme", description="PyTorch and RAG.")
    make_contact(session, "Acme", "careers@acme.com")

    transport = FakeTransport()
    report = send_batch(session, profile=profile, transport=transport, dry_run=True, pause=False)

    assert report.sent == 0
    assert transport.sent == []
    assert session.query(Outreach).count() == 0


def test_the_cv_is_attached_and_the_sender_refuses_without_it(profile, tmp_path):
    message = build_message(to="hr@acme.com", subject="s", body="b", profile=profile)
    attachments = [p for p in message.iter_attachments()]
    assert len(attachments) == 1
    assert attachments[0].get_filename() == "resume.pdf"

    profile.applicant.resume_path = str(tmp_path / "missing.pdf")
    with pytest.raises(FileNotFoundError):
        build_message(to="hr@acme.com", subject="s", body="b", profile=profile)


def test_the_circuit_breaker_stops_a_run_that_keeps_failing(session, profile, monkeypatch):
    """Do not hammer a server that is rejecting us."""
    monkeypatch.setattr(settings, "daily_send_cap", 10)
    monkeypatch.setattr(settings, "send_failure_circuit_breaker", 3)
    for n in range(8):
        make_job(session, f"Co{n}", description="PyTorch and RAG.")
        make_contact(session, f"Co{n}", f"careers@co{n}.com")

    transport = FakeTransport(fail_with=RuntimeError("550 rejected"))
    report = send_batch(session, profile=profile, transport=transport, pause=False)

    assert report.sent == 0
    assert report.failed == 3, "should have stopped after three consecutive failures"
    assert any("failed in a row" in r for r in report.reasons)


def test_a_failed_send_records_why(session, profile, monkeypatch):
    monkeypatch.setattr(settings, "daily_send_cap", 1)
    make_job(session, "Acme", description="PyTorch and RAG.")
    make_contact(session, "Acme", "careers@acme.com")

    send_batch(session, profile=profile, transport=FakeTransport(fail_with=RuntimeError("boom")),
               pause=False)
    row = session.query(Outreach).one()
    assert row.status == "failed"
    assert "boom" in row.error
    assert row.sent_at is None


def test_role_addresses_outrank_named_individuals(session):
    """Hard rule 5, and the cheapest GDPR minimisation available."""
    make_job(session, "Acme")
    make_contact(session, "Acme", "anna.schmidt@acme.com", kind="person", confidence=0.99)
    make_contact(session, "Acme", "careers@acme.com", kind="role", confidence=0.50)

    company = db.upsert_company(session, Target(name="Acme"))
    assert policy.best_contact(session, company.id).email == "careers@acme.com"
