"""Run orchestration, kept separate from the CLI so it can be tested.

``cli.py`` owns all presentation; this module owns the sequencing and the error
collection. The key invariant: one target failing is data for ``runs.errors``,
never a reason to abandon the other targets.
"""

from __future__ import annotations

import asyncio
import bisect
import logging
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from . import db
from .config import Profile, Target
from .http import PoliteClient, RobotsDisallowed, SourceUnavailable
from .matching.scorer import score_job
from .models import Company, Job, Run
from .sources.registry import fetch_target, resolve

log = logging.getLogger(__name__)

Progress = Callable[[str], None]


def _noop(_: str) -> None:
    return None


_DURATION = re.compile(r"(\d+)\s*([hdw])")


def resolve_since(value: str | None) -> datetime | None:
    """Turn a --since value into a UTC cutoff. Accepts `7d`, `24h`, `2w`, `last-scan`, a date."""
    if not value:
        return None
    raw = value.strip().lower()

    if raw in {"last-scan", "last", "lastscan"}:
        from sqlalchemy import select

        with db.session_scope() as session:
            return session.scalar(
                select(Run.started_at)
                .where(Run.kind == "scan")
                .order_by(Run.started_at.desc())
                .limit(1)
            )

    if match := _DURATION.fullmatch(raw):
        count, unit = int(match.group(1)), match.group(2)
        unit_name = {"h": "hours", "d": "days", "w": "weeks"}[unit]
        return datetime.now(timezone.utc) - timedelta(**{unit_name: count})

    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(
            f"cannot parse {value!r}; use a duration (7d, 24h, 2w), 'last-scan', or a date"
        ) from exc
    # A bare date means UTC, like every timestamp the database holds.
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass
class ResolveOutcome:
    """What fingerprinting one company's careers page turned up."""

    target: Target
    bucket: str  # resolved | unsupported | no_fingerprint | unreachable
    ats: str | None = None
    token: str | None = None
    detail: str = ""


@dataclass
class ResolveResult:
    resolved: list[ResolveOutcome] = field(default_factory=list)
    unsupported: list[ResolveOutcome] = field(default_factory=list)
    no_fingerprint: list[ResolveOutcome] = field(default_factory=list)
    unreachable: list[ResolveOutcome] = field(default_factory=list)
    added: int = 0

    @property
    def misses(self) -> list[ResolveOutcome]:
        return self.unsupported + self.no_fingerprint + self.unreachable

    def by_ats(self) -> dict[str, int]:
        return dict(Counter(o.ats or "?" for o in self.resolved).most_common())

    def unsupported_by_ats(self) -> dict[str, int]:
        """Which missing adapter would unlock the most companies."""
        return dict(Counter(o.ats or "?" for o in self.unsupported).most_common())


# Subdomain labels that name a function, not a company. `careers.ansys.com` must
# yield "ansys", never "careers" — and "careers" must never be probed as a slug,
# because ATSs have real accounts by that name and a hit would attach a stranger's
# jobs to this company.
_GENERIC_LABELS = frozenset(
    """careers career jobs job apply applications hire hiring work workwithus join joinus
    talent recruiting recruitment people hr www web site portal my go get""".split()
)


def candidate_slugs(target: Target, *, limit: int = 2) -> list[str]:
    """Plausible board tokens for a company, best guess first.

    The domain's registrable label beats a squashed company name: "inmobi.com"
    gives "inmobi", the actual Greenhouse token, whereas "Bosch Global Software
    Technologies" squashes into something no ATS would use.
    """
    name = target.name.lower()
    guesses = [
        # Skip functional prefixes so careers.ansys.com resolves to "ansys".
        next(
            (
                label
                for label in (target.domain or "").lower().split(".")
                if label and label not in _GENERIC_LABELS
            ),
            "",
        ),
        re.sub(r"[^a-z0-9]", "", name),
        re.sub(r"[^a-z0-9]+", "-", name).strip("-"),
    ]
    return [g for g in dict.fromkeys(guesses) if g not in _GENERIC_LABELS and len(g) > 2][:limit]


async def probe_ats_slugs(client: PoliteClient, target: Target) -> tuple[str, str, int] | None:
    """Ask each ATS directly whether it hosts a board for this company.

    The fallback for a careers URL that does not load. Recovering InMobi's
    70-job Greenhouse board this way is what justifies the extra requests: a dead
    link on a company's own site says nothing about whether their board exists.
    """
    from .sources.registry import BY_NAME

    for slug in candidate_slugs(target):
        for ats, adapter in BY_NAME.items():
            if getattr(adapter, "is_catalogue", False):
                # A keyword search, not a board: any company name "finds" jobs
                # there, so a hit would record a search as the company's ATS.
                continue
            probe = target.model_copy(update={"ats": ats, "ats_token": slug})
            try:
                jobs = await adapter.fetch(client, probe)
            except (SourceUnavailable, RobotsDisallowed):
                continue
            except Exception as exc:  # noqa: BLE001
                log.debug("%s: %s/%s probe failed: %s", target.name, ats, slug, exc)
                continue
            if jobs:
                return ats, slug, len(jobs)
    return None


async def run_resolve(
    targets: list[Target],
    *,
    companies_path: str | Path = "companies.yaml",
    dry_run: bool = False,
    probe_slugs: bool = True,
    on_progress: Progress = _noop,
) -> ResolveResult:
    """Fingerprint each company's careers page and record its ATS + board token.

    Done once and written into companies.yaml, rather than left for `scan` to
    rediscover: re-fetching every careers page on every run would be slow and
    would hammer a hundred unrelated sites to learn something that rarely changes.
    """
    from .sources.careers_page import fingerprint

    result = ResolveResult()

    async def resolve_one(client: PoliteClient, target: Target) -> ResolveOutcome:
        if not target.careers_url:
            return ResolveOutcome(target, "unreachable", detail="no careers_url")
        try:
            html = await client.get(target.careers_url)
        except RobotsDisallowed:
            return ResolveOutcome(target, "unreachable", detail="robots.txt disallows the page")
        except httpx.HTTPStatusError as exc:
            return ResolveOutcome(target, "unreachable", detail=f"HTTP {exc.response.status_code}")
        except httpx.HTTPError as exc:
            return ResolveOutcome(target, "unreachable", detail=type(exc).__name__)
        except Exception as exc:  # one bad page must not end the run
            log.exception("%s: unexpected failure fetching careers page", target.name)
            return ResolveOutcome(target, "unreachable", detail=type(exc).__name__)

        # The URL is evidence too: a careers URL that *is* an ATS page, such as a
        # Dover board, can serve a JS shell that never names its own host.
        detected = fingerprint(html) or fingerprint(target.careers_url)
        if detected is None:
            return ResolveOutcome(
                target, "no_fingerprint", detail="no ATS marker in the HTML (likely JS-rendered)"
            )
        if not detected.supported:
            return ResolveOutcome(
                target,
                "unsupported",
                ats=detected.ats,
                detail=f"uses {detected.ats}, no adapter (matched {detected.marker!r})",
            )
        if not detected.token:
            # Recognised the ATS but could not extract a board token, so there is
            # nothing to write. Reported rather than guessed at.
            return ResolveOutcome(
                target,
                "no_fingerprint",
                ats=detected.ats,
                detail=f"{detected.ats} detected but no board token in the HTML",
            )
        return ResolveOutcome(
            target, "resolved", ats=detected.ats, token=detected.token, detail=detected.marker
        )

    async with PoliteClient() as client:

        async def tracked(target: Target) -> ResolveOutcome:
            outcome = await resolve_one(client, target)
            on_progress(target.name)
            return outcome

        # All ~100 hosts differ, so the per-host buckets do not serialise these;
        # PoliteClient's semaphore is what caps concurrency.
        outcomes = await asyncio.gather(*(tracked(t) for t in targets))

    for outcome in outcomes:
        getattr(result, outcome.bucket).append(outcome)

    # A careers URL that 404s says nothing about whether the company has a board.
    if probe_slugs and result.unreachable:
        async with PoliteClient() as client:
            recovered: list[ResolveOutcome] = []
            for outcome in result.unreachable:
                on_progress(f"probing {outcome.target.name}")
                if hit := await probe_ats_slugs(client, outcome.target):
                    ats, slug, count = hit
                    outcome.bucket = "resolved"
                    outcome.ats, outcome.token = ats, slug
                    outcome.detail = (
                        f"careers URL failed; found {ats}/{slug} by probe ({count} jobs)"
                    )
                    recovered.append(outcome)
            for outcome in recovered:
                result.unreachable.remove(outcome)
                result.resolved.append(outcome)

    if result.resolved and not dry_run:
        from .config import append_targets

        enriched = [
            o.target.model_copy(update={"ats": o.ats, "ats_token": o.token})
            for o in result.resolved
        ]
        result.added = append_targets(
            companies_path,
            enriched,
            note=f"resolved by `jobhunter resolve` ({len(enriched)} fingerprinted)",
        )

    return result


def _error(company: str, exc: Exception) -> dict:
    """One runs.errors entry."""
    return {"company": company, "error": type(exc).__name__, "detail": str(exc)}


@dataclass
class ScanResult:
    jobs_seen: int = 0
    jobs_new: int = 0
    jobs_closed: int = 0
    errors: list[dict] = field(default_factory=list)
    per_company: dict[str, dict] = field(default_factory=dict)


def _persist_catalogue(session, target: Target, raws: list) -> tuple[int, int]:
    """File a catalogue source's postings under their real employers.

    Filing them all under the search's own name would make the shortlist useless
    (every row reading "FreeHire") and would point contact discovery at the
    aggregator instead of the company. Using the employer also makes dedup work:
    content_hash includes the company name, so a posting seen both directly and
    through the catalogue collapses only when both agree on who is hiring.

    Returns (new, closed). Closed is always 0 — deliberately. A keyword search
    returns what matched, not everything a company has open, so running
    close_stale_jobs here would close every posting the direct adapters found at
    these same employers.
    """
    companies: dict[str, object] = {}
    hosts: dict[str, tuple[str, str | None]] = {}
    new_count = 0
    for raw in raws:
        name = (raw.company_name or "").strip() or target.name
        domain = None
        if _HOST_AS_NAME.fullmatch(name):
            host = name.split("/", 1)[0].lower()
            if host not in hosts:
                hosts[host] = _employer_behind_host(session, host)
            name, domain = hosts[host]
        if name not in companies:
            companies[name] = db.upsert_company(session, Target(name=name, domain=domain))
        _, is_new = db.upsert_job(session, companies[name], raw)
        new_count += int(is_new)
    return new_count, 0


# FreeHire sometimes gives an ATS tenant as the employer: "jpmc.fa.oraclecloud.com/CX",
# "auxoai.zohorecruit.com", "careers.fpsinc.com". A path, or two dots, is a host;
# "Rezo.ai" and "U.S. Bank" are names.
_HOST_AS_NAME = re.compile(r"[\w-]+(?:\.[\w-]+){2,}(?:/\S*)?|[\w-]+\.[\w-]+/\S*")


def _employer_behind_host(session, host: str) -> tuple[str, str | None]:
    """A readable employer name, and domain if known, for a tenant host.

    Filed under the raw host, these postings could get no contact, and one
    employer split into several ("…oraclecloud.com/CX" and "…/CX_3002" are both
    Zensar), each with its own cooldown.
    """
    from sqlalchemy import select

    from .config import domain_from_url
    from .contacts.finder import registrable_domain

    # The same tenant is often named properly on another posting.
    known = session.scalar(
        select(Company.name)
        .join(Job, Job.company_id == Company.id)
        .where(Job.url.like(f"https://{host}/%"), Company.name.not_like("%.%.%"))
        .where(Company.name.not_like("%/%"))
        .limit(1)
    )
    if known:
        return known, None
    if domain_from_url(f"https://{host}") is not None:
        # The employer's own host: careers.fpsinc.com is FPS Inc's site.
        domain = registrable_domain(host)
        return domain.split(".")[0], domain
    if "oraclecloud.com" in host:
        return host, None  # an opaque pod code; at least drop the /CX path
    return host.split(".")[0], None  # tenant subdomain: auxoai.zohorecruit.com


async def run_scan(
    targets: list[Target],
    *,
    dry_run: bool = False,
    sweep_unseen: bool = False,
    on_progress: Progress = _noop,
) -> ScanResult:
    """Fetch every target and persist what came back.

    ``sweep_unseen`` closes jobs missing from several consecutive scans; pass it
    only when ``targets`` is the full list (see ``db.close_unseen_jobs``).
    """
    result = ScanResult()

    async def scan_one(client: PoliteClient, target: Target) -> None:
        try:
            raws = await fetch_target(client, target)
        except Exception as exc:  # one bad adapter must not end the run
            if isinstance(exc, (SourceUnavailable, RobotsDisallowed)):
                log.warning("%s: %s", target.name, exc)  # expected, per-target
            else:
                log.exception("%s: unexpected adapter failure", target.name)
            result.errors.append(_error(target.name, exc))
            result.per_company[target.name] = {"error": str(exc)}
            return

        result.jobs_seen += len(raws)
        if dry_run:
            result.per_company[target.name] = {"seen": len(raws), "new": 0, "closed": 0}
            return

        # Synchronous and never awaits, so concurrent fetches cannot interleave
        # two sessions on the same SQLite file.
        adapter = resolve(target)
        with db.session_scope() as session:
            if getattr(adapter, "is_catalogue", False):
                new_count, closed = _persist_catalogue(session, target, raws)
            else:
                company = db.upsert_company(session, target)
                seen_hashes: set[str] = set()
                new_count = 0
                for raw in raws:
                    job, is_new = db.upsert_job(session, company, raw)
                    seen_hashes.add(job.content_hash)
                    new_count += int(is_new)
                # Reached only because the fetch above succeeded, which is what
                # makes closing the absent postings safe.
                closed = db.close_stale_jobs(session, company, seen_hashes)

        result.jobs_new += new_count
        result.jobs_closed += closed
        result.per_company[target.name] = {
            "seen": len(raws),
            "new": new_count,
            "closed": closed,
        }

    async with PoliteClient() as client:

        async def tracked(target: Target) -> None:
            await scan_one(client, target)
            on_progress(target.name)

        # Targets span a handful of ATS hosts; PoliteClient's per-host buckets
        # keep each at its rate limit while the hosts overlap.
        await asyncio.gather(*(tracked(t) for t in targets))

    if sweep_unseen and not dry_run:
        with db.session_scope() as session:
            result.jobs_closed += db.close_unseen_jobs(session)

    return result


@dataclass
class ContactsResult:
    companies_checked: int = 0
    contacts_found: int = 0
    companies_without_contact: list[str] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    per_company: dict[str, dict] = field(default_factory=dict)


async def run_contacts(
    targets: list[Target],
    *,
    dry_run: bool = False,
    verify_emails: bool | None = None,
    min_score: int | None = None,
    on_progress: Progress = _noop,
) -> ContactsResult:
    """Resolve a hiring contact for every company that has open jobs.

    Companies absent from companies.yaml -- every employer the FreeHire catalogue
    files postings under -- are included too, built from their database row. A
    missing domain is inferred from the company's own postings when one matches
    its name, and saved, since Tier 1 can do nothing without it.
    """
    from sqlalchemy import select

    from .contacts.finder import find_contacts, infer_domain
    from .models import Suppression, hash_email

    result = ContactsResult()
    by_name = {t.name: t for t in targets}

    # Only companies with something open: harvesting contacts for a company that
    # is not hiring has no legitimate-interest basis (docs/compliance.md).
    with db.session_scope() as session:
        query = (
            select(
                Company.id, Company.name, Company.catch_all, Company.email_pattern, Company.domain
            )
            .join(Job, Job.company_id == Company.id)
            .where(Job.closed_at.is_(None))
            .distinct()
        )
        if min_score is not None:
            query = query.where(Job.fit_score >= min_score)
        companies = [
            {"id": r[0], "name": r[1], "catch_all": r[2], "email_pattern": r[3], "domain": r[4]}
            for r in session.execute(query).all()
        ]
        job_context = {
            c["id"]: session.execute(
                select(Job.description, Job.url)
                .where(Job.company_id == c["id"], Job.closed_at.is_(None))
                .limit(25)
            ).all()
            for c in companies
        }
        # Loaded once so the finder can drop erased addresses without needing a
        # session, and without a query per candidate.
        suppressed_hashes = set(session.scalars(select(Suppression.email_hash)).all())

    def suppressed(email: str) -> bool:
        return hash_email(email) in suppressed_hashes

    async def resolve_one(client: PoliteClient, company: dict) -> None:
        target = by_name.get(company["name"]) or Target(name=company["name"])
        context = job_context.get(company["id"], [])
        domain = target.domain or company["domain"]
        if not domain:
            domain = infer_domain(
                company["name"], [url for _, url in context], [d for d, _ in context if d]
            )
            if domain and not dry_run:
                with db.session_scope() as session:
                    if row := session.get(Company, company["id"]):
                        row.domain = domain
        target = target.model_copy(update={"domain": domain})
        result.companies_checked += 1

        try:
            found = await find_contacts(
                client,
                target,
                job_texts=[(text, url) for text, url in context if text],
                known_catch_all=company["catch_all"],
                known_pattern=company["email_pattern"],
                verify_emails=verify_emails,
                is_suppressed=suppressed,
            )
        except Exception as exc:  # one company must not end the run
            log.exception("%s: contact discovery failed", company["name"])
            result.errors.append(_error(company["name"], exc))
            return

        surfaced = found.surfaced()
        if not surfaced:
            result.companies_without_contact.append(company["name"])
            result.per_company[company["name"]] = {"found": 0, "notes": found.notes}
            return

        if not dry_run:
            with db.session_scope() as session:
                row = session.get(Company, company["id"])
                if row is not None:
                    if found.catch_all is not None:
                        row.catch_all = found.catch_all
                    if found.email_pattern:
                        row.email_pattern = found.email_pattern
                    for candidate in surfaced:
                        db.upsert_contact(
                            session,
                            row,
                            email=candidate.email,
                            kind=candidate.kind,
                            confidence=candidate.confidence,
                            discovery_method=candidate.discovery_method,
                            source_url=candidate.source_url,
                            first_name=candidate.first_name,
                            last_name=candidate.last_name,
                            role_title=candidate.role_title,
                        )
        result.contacts_found += len(surfaced)
        result.per_company[company["name"]] = {
            "found": len(surfaced),
            "best": surfaced[0].email,
            "confidence": surfaced[0].confidence,
            "method": surfaced[0].discovery_method,
        }

    async with PoliteClient() as client:

        async def tracked(company: dict) -> None:
            await resolve_one(client, company)
            on_progress(company["name"])

        # Each company is its own host, so PoliteClient's per-host buckets keep
        # every site at its rate limit while different sites overlap. SMTP
        # probing (opt-in) is synchronous and so still runs one at a time.
        await asyncio.gather(*(tracked(c) for c in companies))

    return result


# Score bands for the summary: a score at or above each floor falls in the next band.
_BAND_FLOORS = (1, 50, 70, 90)
_BANDS = ("0", "1-49", "50-69", "70-89", "90+")


def run_score(profile: Profile, *, dry_run: bool = False) -> dict:
    """Rescore every open job. Returns a summary, including the disqualified tally."""
    from sqlalchemy import select

    scored = 0
    disqualified = 0
    buckets = dict.fromkeys(reversed(_BANDS), 0)  # printed best band first

    with db.session_scope() as session:
        jobs = session.scalars(select(Job).where(Job.closed_at.is_(None))).all()
        for job in jobs:
            score = score_job(job, profile)
            scored += 1
            if score.disqualified:
                disqualified += 1
            if not dry_run:
                job.fit_score = score.total
                job.fit_reasons = score.as_fit_reasons()
            buckets[_BANDS[bisect.bisect_right(_BAND_FLOORS, score.total)]] += 1

    return {"scored": scored, "disqualified": disqualified, "buckets": buckets}
