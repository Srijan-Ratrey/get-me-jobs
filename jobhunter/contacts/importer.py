"""Import HR contacts researched by hand from a CSV.

The three discovery tiers find role addresses well and named recruiters badly,
so the addresses that matter most are often the ones a person found by reading
a careers page or a spreadsheet. This is the path for those.

Imported rows go through the same `scraper.rank` as discovered ones, so a
hand-entered `careers@` is classified and ranked identically rather than
arriving as a special case that the rest of the pipeline has to know about.

`source_url` is mandatory. Hard rule 4 and a GDPR obligation: if someone asks
where their address came from, "a spreadsheet" is not an answer. A row without
provenance is rejected and reported, never quietly accepted with a blank.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Target, _normalise_header, _pick_column
from ..models import Company, Contact, Suppression, hash_email
from .scraper import rank

log = logging.getLogger(__name__)

_COMPANY_HEADERS = ("company", "companyname", "employer", "organisation", "organization", "name")
_EMAIL_HEADERS = ("email", "emailaddress", "mail", "hremail", "contactemail", "address")
_NAME_HEADERS = ("name", "contactname", "person", "contact", "fullname", "hrname", "recruiter")
_ROLE_HEADERS = ("role", "title", "roletitle", "designation", "position", "jobtitle")
_SOURCE_HEADERS = ("sourceurl", "source", "url", "link", "where", "profile", "foundat")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")

# Suffixes stripped when matching a spreadsheet's company name against the
# database. "Zensar Technologies" in a sheet and "Zensar" in the DB are one
# employer, and failing to join them creates a second contact row that the
# per-contact cooldown then has to catch after the fact.
#
# Split into two lists because the failure modes are not symmetric. Attaching a
# contact to the wrong employer means mailing strangers, so over-merging is far
# worse than under-merging, and the rules are deliberately lopsided.

# Long and unambiguous: safe to strip even when run together with the name, as
# ATS slugs do ("Citigroup", "Aeratechnology"). Nothing short enough to appear
# inside an ordinary word belongs here.
_GLUED_SUFFIXES = (
    "technologies",
    "technology",
    "solutions",
    "consulting",
    "international",
    "corporation",
    "enterprises",
    "industries",
    "holdings",
    "services",
    "systems",
    "limited",
    "private",
    "group",
)

# Short and ambiguous: only ever stripped as a separate word. Stripping these by
# characters turns "Cisco" into "cis" and "Telecom" into "tele", which is how a
# join key starts merging unrelated employers.
_WORD_SUFFIXES = frozenset(
    {
        "co",
        "com",
        "inc",
        "llc",
        "ltd",
        "plc",
        "pvt",
        "corp",
        "company",
        "global",
        "india",
        "labs",
        "gmbh",
        "sa",
        "ag",
        "bv",
        "nv",
        "oy",
        "ab",
    }
)

_MIN_KEY = 3


def company_key(name: str) -> str:
    """Normalised join key for a company name. Conservative by design.

    Over-merging attaches an HR contact to an employer they do not work for and
    then mails them about it, so every rule here errs toward leaving two names
    apart rather than guessing they are one.
    """
    tokens = [t for t in re.split(r"[^a-z0-9]+", (name or "").lower()) if t]
    if not tokens:
        return ""
    # Drop trailing corporate words, but never the whole name: a company really
    # called "Group" or "Co" has to survive as itself.
    while len(tokens) > 1 and tokens[-1] in _WORD_SUFFIXES:
        tokens.pop()

    s = "".join(tokens)
    changed = True
    while changed and len(s) > _MIN_KEY:
        changed = False
        for suffix in _GLUED_SUFFIXES:
            if s.endswith(suffix) and len(s) - len(suffix) >= _MIN_KEY:
                s, changed = s[: -len(suffix)], True
    return s


@dataclass
class ImportRow:
    company: str
    email: str
    first_name: str = ""
    last_name: str = ""
    role_title: str = ""
    source_url: str = ""


@dataclass
class ImportReport:
    added: int = 0
    updated: int = 0
    skipped: int = 0
    new_companies: int = 0
    reasons: list[str] = field(default_factory=list)


def load_contact_csv(path: str | Path) -> tuple[list[ImportRow], list[str]]:
    """Parse the CSV. Returns (rows, skipped_reasons).

    Skips are returned rather than logged away, matching `load_company_csv`: a
    row that silently vanishes is a contact you believe you have and do not.
    """
    text = Path(path).read_text(encoding="utf-8-sig")  # tolerate a BOM
    reader = csv.DictReader(io.StringIO(text))
    headers = {_normalise_header(h): h for h in (reader.fieldnames or [])}

    email_col = _pick_column(headers, _EMAIL_HEADERS)
    company_col = _pick_column(headers, _COMPANY_HEADERS)
    if not email_col:
        return [], [f"no email column found in {sorted(headers.values())}"]
    if not company_col:
        return [], [f"no company column found in {sorted(headers.values())}"]

    # "name" is in both alias lists; whichever column company claimed is not
    # also the contact's name.
    name_col = _pick_column({k: v for k, v in headers.items() if v != company_col}, _NAME_HEADERS)
    role_col = _pick_column(headers, _ROLE_HEADERS)
    source_col = _pick_column(headers, _SOURCE_HEADERS)

    rows: list[ImportRow] = []
    skipped: list[str] = []
    for number, raw in enumerate(reader, start=2):  # row 1 is the header
        email = (raw.get(email_col) or "").strip().lower()
        company = (raw.get(company_col) or "").strip()
        source = (raw.get(source_col) or "").strip() if source_col else ""

        if not email and not company:
            continue  # blank spacer row
        if not email:
            skipped.append(f"row {number} ({company or 'unnamed'}): no email")
            continue
        if not _EMAIL_RE.match(email):
            skipped.append(f"row {number}: {email!r} is not an email address")
            continue
        if not company:
            skipped.append(f"row {number} ({email}): no company")
            continue
        if not source:
            skipped.append(
                f"row {number} ({email}): no source_url. Every contact needs provenance "
                "— see docs/compliance.md."
            )
            continue

        full = (raw.get(name_col) or "").strip() if name_col else ""
        first, _, last = full.partition(" ")
        rows.append(
            ImportRow(
                company=company,
                email=email,
                first_name=first.strip(),
                last_name=last.strip(),
                role_title=((raw.get(role_col) or "").strip() if role_col else ""),
                source_url=source,
            )
        )
    return rows, skipped


def _find_company(s: Session, name: str) -> Company | None:
    exact = s.scalar(select(Company).where(Company.name == name))
    if exact is not None:
        return exact
    key = company_key(name)
    if not key:
        return None
    # Small table and a normalisation SQL cannot express; scan it.
    for company in s.scalars(select(Company)).all():
        if company_key(company.name) == key:
            return company
    return None


def import_contacts(s: Session, rows: list[ImportRow], *, dry_run: bool = False) -> ImportReport:
    """Upsert imported contacts. Returns counts and every skip reason."""
    report = ImportReport()
    for row in rows:
        if s.scalar(select(Suppression.id).where(Suppression.email_hash == hash_email(row.email))):
            report.skipped += 1
            report.reasons.append(f"{row.email}: on the suppression list, not re-added")
            continue

        company = _find_company(s, row.company)
        if company is None:
            if dry_run:
                report.new_companies += 1
                report.added += 1
                continue
            from ..db import upsert_company

            company = upsert_company(s, Target(name=row.company))
            report.new_companies += 1

        confidence, kind, hint = rank(row.email, row.role_title)
        existing = s.scalar(
            select(Contact).where(Contact.company_id == company.id, Contact.email == row.email)
        )
        if existing is not None:
            # Hand research outranks a guess, so let it fill gaps and correct
            # provenance, but never resurrect a suppressed row.
            if not existing.suppressed:
                existing.first_name = row.first_name or existing.first_name
                existing.last_name = row.last_name or existing.last_name
                existing.role_title = row.role_title or hint or existing.role_title
                existing.source_url = row.source_url
                existing.discovery_method = "manual"
                existing.confidence = max(existing.confidence, confidence)
                report.updated += 1
            else:
                report.skipped += 1
                report.reasons.append(f"{row.email}: suppressed, left alone")
            continue

        if not dry_run:
            s.add(
                Contact(
                    company_id=company.id,
                    email=row.email,
                    kind=kind,
                    first_name=row.first_name or None,
                    last_name=row.last_name or None,
                    role_title=row.role_title or hint or None,
                    source_url=row.source_url,
                    discovery_method="manual",
                    confidence=confidence,
                    verify_status="unknown",
                    suppressed=False,
                )
            )
        report.added += 1
    if not dry_run:
        s.flush()
    return report
