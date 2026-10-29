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
from .models import Application, Company, FormQuestion, Job, company_key, utcnow

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


def stored_answers(s: Session) -> dict[str, str]:
    """Answers worth typing again: single-line ones only (never a cover letter)."""
    rows = s.execute(
        select(FormQuestion.label, FormQuestion.answer).where(
            FormQuestion.answer.is_not(None), FormQuestion.multiline.is_(False)
        )
    )
    return dict(rows.all())


def remember(
    s: Session,
    profile: Profile,
    url: str,
    answered: dict[str, tuple[str, bool]],
    unanswered: list[str],
) -> int:
    """Store the form's extra questions and any answers typed. Returns how many.

    Questions profile.yaml already answers (name, email, `answers:`) are not
    extra and are left out. An empty value never erases an answer you gave on
    an earlier form.
    """
    rules = field_rules(profile)
    seen = {clean_label(label) for label in unanswered} | set(answered)
    stored = 0
    for label in sorted(seen):
        if not label or answer_for(label, rules):
            continue
        row = s.scalar(select(FormQuestion).where(FormQuestion.label == label))
        if row is None:
            row = FormQuestion(label=label, times_seen=0)
            s.add(row)
        row.times_seen += 1
        row.last_seen, row.last_url = utcnow(), url
        value, multiline = answered.get(label, ("", False))
        if value.strip():
            row.answer, row.multiline = value.strip(), multiline
        stored += 1
    s.flush()
    return stored


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
    rules = list(a.answers.items())  # user's own first
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

_TEXT_FIELDS = "input[type=text], input[type=email], input[type=tel], input[type=url], input:not([type]), textarea"
_LABEL_OF = """e => e.getAttribute('aria-label') || (e.labels && e.labels[0] && e.labels[0].innerText)
    || e.placeholder || e.name || ''"""


def _js(src: str) -> str:
    """Splice the shared selector and label expression into a page script."""
    return src.replace("TEXT_FIELDS", _TEXT_FIELDS).replace("LABEL_OF", f"({_LABEL_OF})")


# Outline every required field still empty, and return their labels.
_HIGHLIGHT_JS = _js("""() => {
  const left = [];
  for (const e of document.querySelectorAll('input[required], textarea[required], select[required], [aria-required="true"]')) {
    const visible = e.offsetParent !== null && e.type !== 'hidden';
    const empty = e.type === 'file' ? !(e.files && e.files.length) : !e.value;
    if (visible && empty) { e.style.outline = '3px solid #e5484d'; left.push(LABEL_OF(e)); }
  }
  return left;
}""")

# Every text field the person changes is reported as they leave it, so answers
# survive pressing Submit, which replaces the form before the terminal is asked.
_RECORDER_JS = _js("""document.addEventListener('change', e => {
  const el = e.target;
  if (!el.matches || !el.matches('TEXT_FIELDS')) return;
  window.__jobhunterRecord(LABEL_OF(el), el.value, el.tagName === 'TEXTAREA');
}, true);""")
_READ_FORM_JS = _js("""() => [...document.querySelectorAll('TEXT_FIELDS')]
  .filter(e => e.offsetParent !== null)
  .map(e => [LABEL_OF(e), e.value, e.tagName === 'TEXTAREA'])""")


def attach_recorder(page) -> dict[str, tuple[str, bool]]:
    """Collect {label: (value, multiline)} for every field the person edits."""
    answers: dict[str, tuple[str, bool]] = {}

    def record_change(label: str, value: str, multiline: bool) -> None:
        if label := clean_label(label):
            answers[label] = (value, multiline)

    page.expose_function("__jobhunterRecord", record_change)
    page.add_init_script(_RECORDER_JS)
    return answers


def read_form(page) -> dict[str, tuple[str, bool]]:
    """The form's current values, if it is still on screen (not after Submit)."""
    try:
        rows = page.evaluate(_READ_FORM_JS)
    except Exception:  # noqa: BLE001 - the page may have navigated or closed
        return {}
    return {key: (value, multi) for label, value, multi in rows if (key := clean_label(label))}


def fill_form(
    page, profile: Profile, stored: dict[str, str] | None = None
) -> tuple[int, list[str]]:
    """Fill what the profile or an earlier form answers.

    Returns (fields filled, labels of required fields still empty).
    """
    rules = field_rules(profile)
    stored = stored or {}
    filled = 0
    resume = page.locator(
        "input[type=file][id*=resume i], input[type=file][name*=resume i], input[type=file]"
    )
    if resume.count() and Path(profile.applicant.resume_path).is_file():
        resume.first.set_input_files(profile.applicant.resume_path)
        filled += 1
    fields = page.locator(_TEXT_FIELDS)
    for i in range(fields.count()):
        field = fields.nth(i)
        if not field.is_visible() or field.input_value():
            continue
        label = field.evaluate(_LABEL_OF)
        if value := answer_for(label, rules) or stored.get(clean_label(label)):
            field.fill(value)
            filled += 1
    return filled, page.evaluate(_HIGHLIGHT_JS)


def run(
    profile: Profile,
    jobs: list[QueuedJob],
    decide: Callable[[QueuedJob, int, int], str],
    on_record: Callable[[QueuedJob, str], None],
    on_form: Callable[[QueuedJob, dict[str, tuple[str, bool]], list[str]], None],
    stored: dict[str, str] | None = None,
) -> None:
    """Open each form in a visible browser, fill it, and hand over to the user.

    `decide` returns "submitted", "skipped", "next" or "quit"; the first two are
    passed to `on_record`. `on_form` gets every answer typed on the form and the
    required questions left empty, whatever was decided. Imported lazily:
    Playwright is the optional `spa` extra.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        typed = attach_recorder(page)
        try:
            for job in jobs:
                typed.clear()
                page.goto(job.url, wait_until="domcontentloaded")
                try:
                    page.wait_for_load_state("networkidle", timeout=15_000)  # Ashby renders late
                except Exception:  # noqa: BLE001 - a busy page is still worth filling
                    log.debug("%s: page never went idle", job.url)
                filled, unanswered = fill_form(page, profile, stored)
                outcome = decide(job, filled, len(unanswered))
                page.wait_for_timeout(200)  # deliver change events queued while we waited
                on_form(job, {**read_form(page), **typed}, unanswered)
                if outcome == "quit":
                    break
                if outcome in ("submitted", "skipped"):
                    on_record(job, outcome)
        finally:
            browser.close()
