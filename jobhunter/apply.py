"""Fill application forms in a visible browser; the user reviews and submits.

Deliberately not an auto-applier. It opens one job's form at a time, types in
what profile.yaml says, attaches the CV, highlights what it left empty, and
stops. Submitting, CAPTCHAs, demographic surveys and every question without a
configured answer stay with the person whose name is on the application. What
they decide is recorded only when they say so.

Greenhouse, Lever and Ashby only: each serves the whole application as one
standard page. Workday and Oracle need an account per employer and are out of
scope.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import Profile, host_of
from .matching.scorer import matches_location
from .models import Application, Company, Job, company_key, utcnow

log = logging.getLogger(__name__)

# Same reasoning as the email sender's cap: a person applying carefully does a
# handful a day, and ten considered applications beat a hundred sprayed ones.
# A code constant, not a setting, so raising it takes a reviewed change.
DAILY_APPLY_CAP = 10
# One application per employer in this window, however many roles they list.
COMPANY_COOLDOWN = timedelta(days=14)


@dataclass
class QueuedJob:
    job_id: int
    company: str
    title: str
    score: int
    url: str  # the page holding the application form


def apply_url(job_url: str | None) -> str | None:
    """The application form's page, or None for a host this tool does not handle."""
    host = host_of(job_url)
    if not job_url or not host:
        return None
    parts = urlsplit(job_url)
    path = parts.path.rstrip("/")
    if host.endswith("greenhouse.io"):
        return job_url  # the form is on the job page itself
    if host == "jobs.lever.co":
        suffix = "/apply"
    elif host == "jobs.ashbyhq.com":
        suffix = "/application"
    else:
        return None
    if not path.endswith(suffix):
        path += suffix
    return urlunsplit(parts._replace(path=path))


def queue(
    s: Session, profile: Profile, *, limit: int, min_score: int | None = None
) -> list[QueuedJob]:
    """The best open, reachable jobs not yet handled, one per employer, within the cap."""
    since = utcnow() - timedelta(hours=24)
    submitted_today = s.scalar(
        select(func.count())
        .select_from(Application)
        .where(Application.status == "submitted", Application.decided_at >= since)
    )
    room = max(0, min(limit, DAILY_APPLY_CAP - (submitted_today or 0)))
    if not room:
        return []

    cooling = {
        company_key(name)
        for (name,) in s.execute(
            select(Company.name)
            .join(Job, Job.company_id == Company.id)
            .join(Application, Application.job_id == Job.id)
            .where(
                Application.status == "submitted",
                Application.decided_at >= utcnow() - COMPANY_COOLDOWN,
            )
        )
    }
    threshold = profile.min_score if min_score is None else min_score
    rows = s.execute(
        select(Job, Company)
        .join(Company, Company.id == Job.company_id)
        .where(
            Job.closed_at.is_(None),
            Job.fit_score >= threshold,
            Job.id.not_in(select(Application.job_id)),
        )
        .order_by(Job.fit_score.desc(), Job.first_seen.desc())
    ).all()

    out: list[QueuedJob] = []
    for job, company in rows:
        url = apply_url(job.url)
        key = company_key(company.name)
        if not url or key in cooling:
            continue
        if not matches_location(job.location, bool(job.remote), profile.locations, job.description):
            continue
        cooling.add(key)  # one per employer within this batch too
        out.append(QueuedJob(job.id, company.name, job.title, job.fit_score or 0, url))
        if len(out) >= room:
            break
    return out


def record(s: Session, job_id: int, status: str) -> None:
    s.add(Application(job_id=job_id, status=status))
    s.flush()


# --------------------------------------------------------------------------- #
# Matching form labels to profile values -- pure, so it is tested offline
# --------------------------------------------------------------------------- #


def _link(links: list[str], kind: str) -> str:
    for url in links:
        host = host_of(url) or ""
        if kind in host or (kind == "website" and not re.search(r"linkedin|github", host)):
            return url
    return ""


def field_rules(profile: Profile) -> list[tuple[re.Pattern[str], str]]:
    """Label pattern -> value, most specific first. Empty values are dropped."""
    a = profile.applicant
    first, _, last = a.name.strip().partition(" ")
    rules = [(pattern, answer) for pattern, answer in a.answers.items()]  # user's own first
    rules += [
        (r"^first name|^given name", first),
        (r"^last name|^surname|^family name", last),
        (r"^(full )?name$", a.name),
        (r"e-?mail", a.email),
        (r"phone|mobile", a.phone),
        (r"linkedin", _link(a.links, "linkedin")),
        (r"github", _link(a.links, "github")),
        (r"website|portfolio|personal site", _link(a.links, "website")),
        (r"^(current )?location|^city|where are you (currently )?based", a.location),
    ]
    return [(re.compile(p, re.IGNORECASE), v) for p, v in rules if v]


def clean_label(label: str) -> str:
    """'First Name*' / 'Full name✱' -> 'first name'."""
    return " ".join(re.sub(r"[*✱:]", " ", label).split()).lower()


def answer_for(label: str, rules: list[tuple[re.Pattern[str], str]]) -> str | None:
    text = clean_label(label)
    return next((value for pattern, value in rules if pattern.search(text)), None) if text else None


# --------------------------------------------------------------------------- #
# The browser -- thin on purpose; everything decidable is above
# --------------------------------------------------------------------------- #

_LABEL_JS = """e => e.getAttribute('aria-label') || (e.labels && e.labels[0] && e.labels[0].innerText)
    || e.placeholder || e.name || ''"""

# Outline every required field still empty, and say how many there are.
_HIGHLIGHT_JS = """() => {
  let left = 0;
  for (const e of document.querySelectorAll('input[required], textarea[required], select[required], [aria-required="true"]')) {
    const visible = e.offsetParent !== null && e.type !== 'hidden';
    const empty = e.type === 'file' ? !(e.files && e.files.length) : !e.value;
    if (visible && empty) { e.style.outline = '3px solid #e5484d'; left++; }
  }
  return left;
}"""


def fill_form(page, profile: Profile) -> tuple[int, int]:
    """Fill what the profile answers; returns (fields filled, required fields left)."""
    rules = field_rules(profile)
    filled = 0
    resume = page.locator(
        "input[type=file][id*=resume i], input[type=file][name*=resume i], input[type=file]"
    )
    if resume.count() and Path(profile.applicant.resume_path).is_file():
        resume.first.set_input_files(profile.applicant.resume_path)
        filled += 1
    fields = page.locator(
        "input[type=text], input[type=email], input[type=tel], input[type=url], "
        "input:not([type]), textarea"
    )
    for i in range(fields.count()):
        field = fields.nth(i)
        if not field.is_visible() or field.input_value():
            continue
        if value := answer_for(field.evaluate(_LABEL_JS), rules):
            field.fill(value)
            filled += 1
    return filled, page.evaluate(_HIGHLIGHT_JS)


def run(
    profile: Profile,
    jobs: list[QueuedJob],
    decide: Callable[[QueuedJob, int, int], str],
    on_record: Callable[[QueuedJob, str], None],
) -> None:
    """Open each form in a visible browser, fill it, and hand over to the user.

    `decide` returns "submitted", "skipped", "next" or "quit"; the first two are
    passed to `on_record`. Imported lazily: Playwright is the optional `spa` extra.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        try:
            for job in jobs:
                page.goto(job.url, wait_until="domcontentloaded")
                try:
                    page.wait_for_load_state("networkidle", timeout=15_000)  # Ashby renders late
                except Exception:  # noqa: BLE001 - a busy page is still worth filling
                    log.debug("%s: page never went idle", job.url)
                filled, left = fill_form(page, profile)
                outcome = decide(job, filled, left)
                if outcome == "quit":
                    break
                if outcome in ("submitted", "skipped"):
                    on_record(job, outcome)
        finally:
            browser.close()
