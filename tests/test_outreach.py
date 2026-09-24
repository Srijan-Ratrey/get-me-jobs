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
from jobhunter.outreach.drafter import (
    MIN_SPECIFIC_SKILLS,
    Draft,
    Refusal,
    draft_for,
    draft_speculative,
    preflight,
)
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
            achievements=[
                "At Babbage I built the difference engine's scheduler and cut run time by 40%.",
                "At Analytical I shipped the first loop construct, taking "
                "throughput from 12 to 31 operations a second.",
            ],
            education="Mathematics, London",
            location="London",
            availability="available immediately",
            headline="scheduling and throughput, 40% faster at Babbage",
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


def make_job(
    session,
    company_name: str,
    *,
    title="ML Engineer",
    score=90,
    description="We use PyTorch and RAG for NLP.",
    external_id=None,
    url=None,
    title_component=40,
) -> Job:
    """A scored job row.

    `title_component` matters for the speculative path, which reads it to decide
    whether a company hires in the right family at all. Real rows always carry
    fit_reasons; a fixture without them silently fails that gate.
    """
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
    job.fit_reasons = {
        "total": score,
        "components": {"title": title_component},
        "reasons": [],
        "disqualified": None,
    }
    session.flush()
    return job


def make_contact(
    session, company_name: str, email: str, *, kind="role", confidence=0.95
) -> Contact:
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
    # The address itself is the From header build_message sets, not body text.
    assert "+91 00000 00000" in result.body


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
        session,
        "Bosch Group",
        title="Data Scientist",
        description="Work on LLM and LoRA fine-tuning with PyTorch for recommendation.",
    )
    job.location = "bengaluru, in"  # exactly as SmartRecruiters stores it
    session.flush()
    result = draft_for(
        job, make_contact(session, "Bosch Group", "hr@bosch.test"), job.company, profile
    )

    assert isinstance(result, Draft)
    assert "in Bengaluru," in result.body, "location was not cleaned up"
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
    assert "in Bengaluru," in result.body
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
        Outreach(
            job_id=make_job(session, "Old", description="PyTorch and RAG.").id,
            contact_id=contact.id,
            subject="s",
            body="b",
            status="sent",
            sent_at=utcnow() - timedelta(hours=25),
        )
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
        Outreach(
            job_id=job.id,
            contact_id=contact.id,
            subject="s",
            body="b",
            status="sent",
            sent_at=utcnow() - timedelta(days=2),
        )
    )
    session.flush()

    decision = policy.may_send(
        session, policy.Candidate(job=job, contact=contact, company=job.company)
    )
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

    decision = policy.may_send(
        session, policy.Candidate(job=job, contact=contact, company=job.company)
    )
    assert not decision and "suppressed" in decision.reason


def test_a_job_that_closed_after_drafting_is_dropped_at_send_time(session, profile):
    """The batch is chosen up to forty minutes before the last send goes out."""
    job = make_job(session, "Acme", description="PyTorch and RAG.")
    contact = make_contact(session, "Acme", "careers@acme.com")
    job.closed_at = utcnow()
    session.flush()

    decision = policy.may_send(
        session, policy.Candidate(job=job, contact=contact, company=job.company)
    )
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

    send_batch(
        session,
        profile=profile,
        transport=FakeTransport(fail_with=RuntimeError("boom")),
        pause=False,
    )
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


# --------------------------------------------------------------------------- #
# Speculative outreach: companies hiring, but not for anything that matches
# --------------------------------------------------------------------------- #


def test_a_company_with_a_matching_role_gets_an_application_not_a_note(
    session, profile, monkeypatch
):
    """Never write speculatively to someone advertising the job you want."""
    monkeypatch.setattr(settings, "daily_send_cap", 10)
    make_job(session, "Acme", description="PyTorch and RAG and NLP.")
    make_contact(session, "Acme", "careers@acme.com")

    transport = FakeTransport()
    report = send_batch(session, profile=profile, transport=transport, pause=False)

    assert report.sent == 1
    assert report.speculative == 0
    assert session.query(Outreach).one().kind == "application"
    assert "speculative" not in transport.sent[0]["Subject"].lower()


def test_a_company_hiring_something_else_gets_a_speculative_note(session, profile, monkeypatch):
    """1,468 companies in the real database are hiring with nothing that matches."""
    monkeypatch.setattr(settings, "daily_send_cap", 10)
    # Scores below the threshold, so no application — but the postings are technical.
    # Hires ML people, but only in San Francisco -- full title credit, no
    # location credit, so it scores below the floor and never matches.
    make_job(
        session,
        "Beta",
        title="Machine Learning Engineer",
        score=45,
        description="Python services, some PyTorch model serving, SQL.",
    )
    make_job(
        session,
        "Beta",
        title="Data Engineer",
        score=42,
        description="Docker, SQL and Python across the stack.",
    )
    make_contact(session, "Beta", "careers@beta.com")

    transport = FakeTransport()
    report = send_batch(session, profile=profile, transport=transport, min_score=55, pause=False)

    assert report.sent == 1
    assert report.speculative == 1
    row = session.query(Outreach).one()
    assert row.kind == "speculative"
    # It must name roles the company really is advertising, and say it is speculative.
    assert "speculative" in row.body.lower()
    assert "Machine Learning Engineer" in row.body or "Data Engineer" in row.body
    # And must not pretend to be answering an advert.
    assert "I'd like to apply for" not in row.body


def test_a_company_with_no_technical_signal_is_refused(session, profile, monkeypatch):
    """A speculative ML note to a firm hiring accountants has nothing true to say."""
    monkeypatch.setattr(settings, "daily_send_cap", 10)
    make_job(
        session,
        "Gamma",
        title="Office Administrator",
        score=35,
        title_component=0,
        description="Answer the phone, manage the diary, greet visitors.",
    )
    make_contact(session, "Gamma", "careers@gamma.com")

    report = send_batch(
        session, profile=profile, transport=FakeTransport(), min_score=55, pause=False
    )
    assert report.sent == 0


def test_a_company_with_no_open_postings_is_never_written_to(session, profile, monkeypatch):
    """The published hiring intent is the entire lawful basis. No postings, no basis."""
    monkeypatch.setattr(settings, "daily_send_cap", 10)
    job = make_job(
        session, "Delta", title="ML Engineer", score=95, description="PyTorch, RAG, NLP and SQL."
    )
    job.closed_at = utcnow()  # they were hiring; they are not now
    session.flush()
    make_contact(session, "Delta", "careers@delta.com")

    report = send_batch(
        session, profile=profile, transport=FakeTransport(), min_score=55, pause=False
    )
    assert report.sent == 0

    result = draft_speculative(
        make_contact(session, "Delta", "hr@delta.com"), job.company, [], profile
    )
    assert isinstance(result, Refusal)
    assert "no open postings" in result.reason


def test_real_openings_fill_the_budget_before_any_speculative(session, profile, monkeypatch):
    """A speculative note must never displace an application to a real advert."""
    monkeypatch.setattr(settings, "daily_send_cap", 2)
    for n in range(4):  # four genuine matches
        make_job(session, f"Real{n}", description="PyTorch and RAG and NLP.")
        make_contact(session, f"Real{n}", f"careers@real{n}.com")
    for n in range(4):  # and four speculative options
        make_job(
            session,
            f"Spec{n}",
            title="Data Engineer",
            score=45,
            description="Python and SQL and Docker.",
        )
        make_contact(session, f"Spec{n}", f"careers@spec{n}.com")

    report = send_batch(
        session, profile=profile, transport=FakeTransport(), min_score=55, pause=False
    )
    assert report.sent == 2
    assert report.speculative == 0, "speculative mail took a slot from a real application"


def test_speculative_can_be_turned_off_entirely(session, profile, monkeypatch):
    monkeypatch.setattr(settings, "daily_send_cap", 10)
    make_job(
        session, "Beta", title="Data Engineer", score=45, description="Python, PyTorch and SQL."
    )
    make_contact(session, "Beta", "careers@beta.com")

    report = send_batch(
        session,
        profile=profile,
        transport=FakeTransport(),
        min_score=55,
        pause=False,
        speculative=False,
    )
    assert report.sent == 0


def test_a_cooldown_spans_both_kinds(session, profile, monkeypatch):
    """Mailing a company speculatively must block a real application next week."""
    monkeypatch.setattr(settings, "daily_send_cap", 10)
    job = make_job(
        session, "Beta", title="Data Engineer", score=45, description="Python, PyTorch and SQL."
    )
    contact = make_contact(session, "Beta", "careers@beta.com")
    session.add(
        Outreach(
            job_id=job.id,
            contact_id=contact.id,
            subject="s",
            body="b",
            status="sent",
            kind="speculative",
            sent_at=utcnow() - timedelta(days=1),
        )
    )
    session.flush()

    decision = policy.may_send(
        session, policy.Candidate(job=job, contact=contact, company=job.company)
    )
    assert not decision
    assert "cooldown" in decision.reason or "already" in decision.reason


def test_two_speculative_notes_do_not_read_alike(session, profile):
    """Same rule as applications: swap the company and the text must change."""
    a = make_job(
        session,
        "Alpha",
        title="ML Engineer",
        score=45,
        description="PyTorch and RAG model serving.",
    )
    b = make_job(
        session,
        "Beta",
        title="Data Platform Engineer",
        score=45,
        description="SQL, Docker and NLP pipelines.",
    )
    da = draft_speculative(make_contact(session, "Alpha", "c@alpha.com"), a.company, [a], profile)
    dbf = draft_speculative(make_contact(session, "Beta", "c@beta.com"), b.company, [b], profile)

    assert isinstance(da, Draft) and isinstance(dbf, Draft)
    assert da.body.replace("Alpha", "X") != dbf.body.replace("Beta", "X")


def test_requisition_noise_is_stripped_from_named_roles(session, profile):
    job = make_job(
        session,
        "Alpha",
        title="Data Engineer (R4633)",
        score=45,
        description="Python and SQL and Docker.",
    )
    result = draft_speculative(
        make_contact(session, "Alpha", "c@alpha.com"), job.company, [job], profile
    )
    assert isinstance(result, Draft)
    assert "Data Engineer" in result.body
    assert "R4633" not in result.body


# --------------------------------------------------------------------------- #
# The evidence-led template
# --------------------------------------------------------------------------- #


def test_a_draft_carries_the_applicant_s_own_numbers(session, profile):
    """Recruiters screen on outcomes. Asserting competence is what everyone does."""
    job = make_job(session, "Acme", description="PyTorch, RAG and NLP work.")
    result = draft_for(job, make_contact(session, "Acme", "hr@acme.test"), job.company, profile)

    assert isinstance(result, Draft)
    assert "cut run time by 40%" in result.body
    assert "12 to 31 operations a second" in result.body
    assert "Mathematics, London" in result.body
    assert "Based in London, available immediately." in result.body


def test_a_profile_with_no_achievements_is_refused(session, profile):
    """An application with no evidence in it is worse than not sending one."""
    profile.applicant.achievements = []
    job = make_job(session, "Acme", description="PyTorch, RAG and NLP work.")
    result = draft_for(job, make_contact(session, "Acme", "hr@acme.test"), job.company, profile)

    assert isinstance(result, Refusal)
    assert "achievements" in result.reason


def test_the_ask_is_a_question_not_a_plea(session, profile):
    """ "Who should I talk to?" earns a forward; "please consider me" does not."""
    job = make_job(session, "Acme", description="PyTorch, RAG and NLP work.")
    result = draft_for(job, make_contact(session, "Acme", "hr@acme.test"), job.company, profile)

    assert isinstance(result, Draft)
    assert "point me to whoever is?" in result.body


def test_the_subject_drops_the_role_qualifier_before_the_headline(session, profile):
    """Past ~72 chars the inbox truncates, and the evidence is what earns the open."""
    job = make_job(session, "Acme", description="PyTorch, RAG and NLP work.")
    job.title = "Data Scientist - Online Ads / Bidding Marketplaces"
    session.flush()
    result = draft_for(job, make_contact(session, "Acme", "hr@acme.test"), job.company, profile)

    assert isinstance(result, Draft)
    assert result.subject == "Data Scientist — scheduling and throughput, 40% faster at Babbage"
    assert len(result.subject) <= 72


def test_a_short_title_keeps_its_full_form(session, profile):
    job = make_job(session, "Acme", description="PyTorch, RAG and NLP work.")
    job.title = "Data Scientist"
    session.flush()
    result = draft_for(job, make_contact(session, "Acme", "hr@acme.test"), job.company, profile)

    assert isinstance(result, Draft)
    assert result.subject.startswith("Data Scientist — ")


def test_the_portfolio_link_is_named_once(session, profile):
    """Body and signature both listing GitHub reads like a template seam."""
    job = make_job(session, "Acme", description="PyTorch, RAG and NLP work.")
    result = draft_for(job, make_contact(session, "Acme", "hr@acme.test"), job.company, profile)

    assert isinstance(result, Draft)
    assert result.body.count("github.com/ada") == 1
    assert "my work is at github.com/ada" in result.body
    # Schemes are noise in a signature.
    assert "https://" not in result.body.split("Thanks for your time,")[1]


def test_an_applicant_with_no_phone_or_links_still_leaves_a_reply_path(session, profile):
    profile.applicant.phone = ""
    profile.applicant.links = []
    job = make_job(session, "Acme", description="PyTorch, RAG and NLP work.")
    result = draft_for(job, make_contact(session, "Acme", "hr@acme.test"), job.company, profile)

    assert isinstance(result, Draft)
    assert "ada@example.com" in result.body
