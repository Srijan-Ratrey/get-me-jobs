"""Whether a message may be sent. Every gate lives here and nowhere else.

This module is the reason an unattended sender is defensible. `drafter` decides
what to say and `sender` knows how to talk to Gmail; neither may decide who
gets mailed. Keeping that in one file means the safety story can be read in one
sitting and tested gate by gate.

The cooldowns are not politeness. The shortlist holds 594 rows across 379
companies: Zensar appears eleven times under two spellings, pwc eleven, Google
eight. Iterating rows without a per-contact cooldown would mail one HR inbox
twenty-two times in a single morning, which is both useless to the applicant
and indistinguishable from spam to everyone else.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Company, Contact, Job, Outreach, Suppression, hash_email, utcnow

log = logging.getLogger(__name__)


@dataclass
class Decision:
    """Whether one candidate may be mailed, and why not when it may not."""

    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allowed


@dataclass
class Candidate:
    """A job, the person to tell about it, and their employer.

    `kind` is "application" when we are applying to `job`, and "speculative"
    when no role matched and `job` is merely the posting the message cites as
    evidence the company is hiring. `evidence` carries the other open postings
    a speculative note can name.
    """

    job: Job
    contact: Contact
    company: Company
    kind: str = "application"
    evidence: list[Job] = field(default_factory=list)


def daily_cap() -> int:
    """The configured cap, clamped to a ceiling settings cannot raise.

    The ceiling is in code rather than config on purpose: the cap is what keeps
    this inside `compliance.md`, and raising it should require a diff somebody
    reads, not an environment variable.
    """
    return max(0, min(settings.daily_send_cap, settings.hard_daily_send_ceiling))


def sent_recently(s: Session) -> int:
    """Messages actually sent in the trailing 24 hours.

    A rolling window rather than a calendar day. Calendar days need a timezone,
    and the obvious choices are both wrong here: UTC resets the budget at 05:30
    IST, and local time makes the count depend on where the laptop is. A
    trailing window has no boundary to get wrong and is never less strict.

    Counted from the database, never from a counter in the process, so a cron
    double-fire, a retry, or two terminals cannot each spend the full budget.
    """
    since = utcnow() - timedelta(hours=24)
    return int(
        s.scalar(
            select(func.count())
            .select_from(Outreach)
            .where(
                Outreach.status == "sent", Outreach.sent_at.is_not(None), Outreach.sent_at >= since
            )
        )
        or 0
    )


def remaining_today(s: Session) -> int:
    return max(0, daily_cap() - sent_recently(s))


def _is_suppressed(s: Session, email: str) -> bool:
    """Honour an erasure or opt-out request, whichever table recorded it."""
    digest = hash_email(email)
    return s.scalar(select(Suppression.id).where(Suppression.email_hash == digest)) is not None


def _last_sent_to_email(s: Session, email: str):
    """When this address was last mailed, across every company row it appears under.

    Keyed on the address rather than the contact row: "Zensar" and "Zensar
    Technologies" are two companies in this database sharing one HR inbox, and
    a cooldown keyed on contact_id would let each of them through.
    """
    return s.scalar(
        select(func.max(Outreach.sent_at))
        .select_from(Outreach)
        .join(Contact, Contact.id == Outreach.contact_id)
        .where(Contact.email == email, Outreach.status == "sent")
    )


def _last_sent_to_company(s: Session, company_id: int):
    return s.scalar(
        select(func.max(Outreach.sent_at))
        .select_from(Outreach)
        .join(Job, Job.id == Outreach.job_id)
        .where(Job.company_id == company_id, Outreach.status == "sent")
    )


def may_send(s: Session, candidate: Candidate) -> Decision:
    """Every per-message gate, in the order that fails cheapest first."""
    job, contact, company = candidate.job, candidate.contact, candidate.company

    if contact.suppressed:
        return Decision(False, f"{contact.email} is suppressed")
    if _is_suppressed(s, contact.email):
        return Decision(False, f"{contact.email} is on the suppression list")

    # Re-checked at send time, not only at draft time: a run drafts a batch and
    # then spends forty minutes spacing the sends out, and a posting can close
    # inside that window.
    if job.closed_at is not None:
        return Decision(False, f"job {job.id} closed since it was drafted")

    already = s.scalar(
        select(Outreach.id).where(
            Outreach.job_id == job.id, Outreach.contact_id == contact.id, Outreach.status == "sent"
        )
    )
    if already is not None:
        return Decision(False, f"already mailed {contact.email} about job {job.id}")

    last = _last_sent_to_email(s, contact.email)
    if last is not None:
        age = utcnow() - last
        if age < timedelta(days=settings.contact_cooldown_days):
            days = settings.contact_cooldown_days - age.days
            return Decision(
                False, f"{contact.email} mailed {age.days}d ago; {days}d of cooldown left"
            )

    last_company = _last_sent_to_company(s, company.id)
    if last_company is not None:
        age = utcnow() - last_company
        if age < timedelta(days=settings.company_cooldown_days):
            days = settings.company_cooldown_days - age.days
            return Decision(
                False, f"{company.name} mailed {age.days}d ago; {days}d of cooldown left"
            )

    return Decision(True)


def best_contact(s: Session, company_id: int) -> Contact | None:
    """The address to use for a company.

    Role addresses win over named individuals — hard rule 5, and the reason is
    not only deliverability: `careers@` is generally not personal data, so
    preferring it is the cheapest possible GDPR minimisation. Confidence breaks
    the tie within a kind.
    """
    return s.scalars(
        select(Contact)
        .where(Contact.company_id == company_id, Contact.suppressed.is_(False))
        .order_by((Contact.kind == "role").desc(), Contact.confidence.desc(), Contact.id)
        .limit(1)
    ).first()


def candidates(s: Session, *, min_score: int, limit: int) -> list[Candidate]:
    """Open, in-budget jobs worth mailing about, best-scoring first.

    Returns at most `limit`, and only one per company: a run that mails a
    company about its three open roles is the behaviour the cooldowns exist to
    prevent, and filtering here means the caller never has to know that.
    """
    rows = s.execute(
        select(Job, Company)
        .join(Company, Company.id == Job.company_id)
        .where(Job.closed_at.is_(None), Job.fit_score.is_not(None), Job.fit_score >= min_score)
        .order_by(Job.fit_score.desc(), Job.posted_at.desc())
    ).all()

    out: list[Candidate] = []
    seen_companies: set[int] = set()
    seen_emails: set[str] = set()
    for job, company in rows:
        if len(out) >= limit:
            break
        if company.id in seen_companies:
            continue
        contact = best_contact(s, company.id)
        if contact is None:
            continue
        # Two company rows can share one inbox; keep the higher-scoring job only.
        if contact.email in seen_emails:
            continue
        candidate = Candidate(job=job, contact=contact, company=company)
        decision = may_send(s, candidate)
        if not decision:
            log.debug("skipping %s: %s", company.name, decision.reason)
            continue
        seen_companies.add(company.id)
        seen_emails.add(contact.email)
        out.append(candidate)
    return out


# Enough of a hiring signal to bother reading a company's postings in full. A
# company whose best open role scores below this is hiring for something far
# enough from this profile that a speculative note would have nothing true to
# say. Cheap SQL filter ahead of the expensive keyword pass.
SPECULATIVE_MIN_SIGNAL = 30

# ...and at least one open role has to be in the right *family*, judged by the
# scorer's title component rather than by the description.
#
# Skills in a description are a weak signal on their own: SQL, BigQuery and GCP
# turn up in analytics-flavoured sales postings, so the first live run produced
# a note to a company whose open roles were "Technical Product Specialist" and
# "Technical Account Manager". True, and useless. Requiring a title that scores
# at all drops 592 eligible companies to 501 and removes exactly the wrong ones
# -- Technical Support Engineer, Information Security, Firmware Test, Security
# Engineer. It also costs a couple of genuine ones ("Software Engineer - Agent
# Harness"), which is the right trade when a wasted slot comes out of the same
# fifteen-a-day budget as a real application.
SPECULATIVE_MIN_TITLE_SIGNAL = 16


def speculative_candidates(
    s: Session, profile, *, min_score: int, limit: int, exclude_companies: set[int] | None = None
) -> list[Candidate]:
    """Companies worth writing to even though nothing they have open matches.

    Measured on the real database: 627 companies have a matching open role and
    1,468 are hiring with nothing that matches -- so "no opening" almost always
    means "no opening for you", which is a far better position to write from.
    Only 23 companies have no postings at all, and those are excluded, because
    the published hiring intent is the entire lawful basis for the message.

    A company qualifies when it has open postings, none of them match, and its
    postings together mention at least MIN_SPECIFIC_SKILLS profile skills --
    the same floor the drafter applies, for the same reason: without it there
    is nothing true and specific to say and the note becomes a form letter.

    Evaluated lazily, best hiring signal first, stopping at `limit`. There are
    1,468 candidate companies and a batch is fifteen; reading every posting of
    every one of them to pick a handful would be absurd.
    """
    from .drafter import MIN_SPECIFIC_SKILLS, VAGUE_SKILLS, matched_skills

    if limit <= 0:
        return []
    excluded = exclude_companies or set()

    # Companies with a matching open role get a real application, not this.
    has_match = select(Job.company_id).where(
        Job.closed_at.is_(None), Job.fit_score.is_not(None), Job.fit_score >= min_score
    )
    title_component = func.coalesce(func.json_extract(Job.fit_reasons, "$.components.title"), 0)
    ranked = s.execute(
        select(Job.company_id, func.max(Job.fit_score))
        .where(Job.closed_at.is_(None), Job.company_id.not_in(has_match))
        .group_by(Job.company_id)
        .having(func.max(Job.fit_score) >= SPECULATIVE_MIN_SIGNAL)
        .having(func.max(title_component) >= SPECULATIVE_MIN_TITLE_SIGNAL)
        .order_by(func.max(Job.fit_score).desc())
    ).all()

    out: list[Candidate] = []
    seen_emails: set[str] = set()
    for company_id, _best in ranked:
        if len(out) >= limit:
            break
        if company_id in excluded:
            continue
        contact = best_contact(s, company_id)
        if contact is None or contact.email in seen_emails:
            continue

        # Only postings in the right family are worth naming. The first live
        # render listed "Enterprise Account Executive" alongside "Data Platform
        # Engineer", which advertises that nobody read the list.
        openings = s.scalars(
            select(Job)
            .where(
                Job.company_id == company_id,
                Job.closed_at.is_(None),
                title_component >= SPECULATIVE_MIN_TITLE_SIGNAL,
            )
            .order_by(title_component.desc(), Job.fit_score.desc())
            .limit(3)
        ).all()
        if not openings:
            continue  # no published hiring intent; the basis this rests on

        # Name only the closest tier. Partial overlap is a low bar -- "Content
        # Developer - Learning and Curriculum" clears it on the word "learning"
        # -- so a company with one real match and two near-misses should be
        # described by the real one alone.
        best_title = max(
            (j.fit_reasons or {}).get("components", {}).get("title", 0) for j in openings
        )
        openings = [
            j
            for j in openings
            if (j.fit_reasons or {}).get("components", {}).get("title", 0) == best_title
        ]

        # Within a tier, richer postings first -- counting only concrete skills.
        # Ranking on the raw count put "M&E Associate" top, because Monitoring &
        # Evaluation matches the profile keyword "evaluation". Vague terms are
        # exactly the ones that match things they have nothing to do with.
        def _concrete(job) -> int:
            return sum(1 for s in matched_skills(job, profile) if s.lower() not in VAGUE_SKILLS)

        openings.sort(key=_concrete, reverse=True)
        # A partial-match tier means we are guessing at the family, so name the
        # one best role rather than three. Naming "M&E Associate" and "Content
        # Developer - Learning and Curriculum" in an ML application is worse than
        # naming nothing extra at all.
        if best_title < 40:
            openings = openings[:1]

        # Skills come from exactly the postings the message names. Computing them
        # over a wider set let the sentence "those postings ask for X" attribute
        # a skill to a posting that never mentioned it -- untrue, in a message to
        # a real person, about their own adverts.
        skills: list[str] = []
        for job in openings:
            for skill in matched_skills(job, profile):
                if skill not in skills:
                    skills.append(skill)
        if len(skills) < MIN_SPECIFIC_SKILLS:
            log.debug("company %s: no technical signal in its postings", company_id)
            continue

        company = s.get(Company, company_id)
        candidate = Candidate(
            job=openings[0],
            contact=contact,
            company=company,
            kind="speculative",
            evidence=list(openings),
        )
        decision = may_send(s, candidate)
        if not decision:
            log.debug("skipping speculative %s: %s", company.name, decision.reason)
            continue
        seen_emails.add(contact.email)
        out.append(candidate)
    return out
