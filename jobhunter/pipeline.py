"""Run orchestration, kept separate from the CLI so it can be tested.

``cli.py`` owns all presentation; this module owns the sequencing and the error
collection. The key invariant: one target failing is data for ``runs.errors``,
never a reason to abandon the other targets.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import httpx

from . import db
from .config import Profile, Target, settings
from .http import PoliteClient, RobotsDisallowed, SourceUnavailable
from .matching.scorer import score_job
from .models import Job, Run
from .sources.registry import fetch_target

log = logging.getLogger(__name__)

Progress = Callable[[str], None]


def _noop(_: str) -> None:
    return None


_DURATION = re.compile(r"(\d+)\s*([hdw])")


def resolve_since(value: str | None) -> datetime | None:
    """Turn a --since value into a cutoff. Accepts `7d`, `24h`, `2w`, `last-scan`, a date.

    Returns a **naive** UTC datetime, because SQLite's DateTime column drops the
    offset on write: comparing a stored naive value against an aware one silently
    matches nothing, which looks exactly like "no new jobs".
    """
    if not value:
        return None
    raw = value.strip().lower()

    if raw in {"last-scan", "last", "lastscan"}:
        from sqlalchemy import select

        with db.session_scope() as session:
            return session.scalar(select(Run.started_at).order_by(Run.started_at.desc()).limit(1))

    if match := _DURATION.fullmatch(raw):
        count, unit = int(match.group(1)), match.group(2)
        delta = {"h": timedelta(hours=count), "d": timedelta(days=count), "w": timedelta(weeks=count)}
        return datetime.now(timezone.utc).replace(tzinfo=None) - delta[unit]

    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(
            f"cannot parse {value!r}; use a duration (7d, 24h, 2w), 'last-scan', or a date"
        ) from exc
    return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed


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
        counts: dict[str, int] = {}
        for outcome in self.resolved:
            counts[outcome.ats or "?"] = counts.get(outcome.ats or "?", 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    def unsupported_by_ats(self) -> dict[str, int]:
        """Which missing adapter would unlock the most companies."""
        counts: dict[str, int] = {}
        for outcome in self.unsupported:
            counts[outcome.ats or "?"] = counts.get(outcome.ats or "?", 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


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
    guesses: list[str] = []
    if target.domain:
        # Skip functional prefixes so careers.ansys.com resolves to "ansys".
        for label in target.domain.lower().split("."):
            if label and label not in _GENERIC_LABELS:
                guesses.append(label)
                break
    squashed = re.sub(r"[^a-z0-9]", "", target.name.lower())
    if squashed:
        guesses.append(squashed)
    hyphenated = re.sub(r"[^a-z0-9]+", "-", target.name.lower()).strip("-")
    if hyphenated:
        guesses.append(hyphenated)

    ordered: list[str] = []
    for guess in guesses:
        if guess not in ordered and guess not in _GENERIC_LABELS and len(guess) > 2:
            ordered.append(guess)
    return ordered[:limit]


async def probe_ats_slugs(client: PoliteClient, target: Target) -> tuple[str, str, int] | None:
    """Ask each ATS directly whether it hosts a board for this company.

    The fallback for a careers URL that does not load. Recovering InMobi's
    70-job Greenhouse board this way is what justifies the extra requests: a dead
    link on a company's own site says nothing about whether their board exists.
    """
    from .sources.registry import BY_NAME

    for slug in candidate_slugs(target):
        for ats, adapter in BY_NAME.items():
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
            return ResolveOutcome(
                target, "unreachable", detail=f"HTTP {exc.response.status_code}"
            )
        except httpx.HTTPError as exc:
            return ResolveOutcome(target, "unreachable", detail=type(exc).__name__)
        except Exception as exc:  # noqa: BLE001 - one bad page must not end the run
            log.exception("%s: unexpected failure fetching careers page", target.name)
            return ResolveOutcome(target, "unreachable", detail=type(exc).__name__)

        detected = fingerprint(html)
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
                    outcome.detail = f"careers URL failed; found {ats}/{slug} by probe ({count} jobs)"
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


@dataclass
class ScanResult:
    jobs_seen: int = 0
    jobs_new: int = 0
    jobs_closed: int = 0
    errors: list[dict] = field(default_factory=list)
    per_company: dict[str, dict] = field(default_factory=dict)


async def run_scan(
    targets: list[Target],
    *,
    dry_run: bool = False,
    on_progress: Progress = _noop,
) -> ScanResult:
    """Fetch every target and persist what came back."""
    result = ScanResult()

    async with PoliteClient() as client:
        for target in targets:
            on_progress(target.name)
            try:
                raws = await fetch_target(client, target)
            except (SourceUnavailable, RobotsDisallowed) as exc:
                # Expected, per-target, recoverable. Record and carry on.
                log.warning("%s: %s", target.name, exc)
                result.errors.append(
                    {"company": target.name, "error": type(exc).__name__, "detail": str(exc)}
                )
                result.per_company[target.name] = {"error": str(exc)}
                continue
            except Exception as exc:  # noqa: BLE001 - one bad adapter must not end the run
                log.exception("%s: unexpected adapter failure", target.name)
                result.errors.append(
                    {"company": target.name, "error": type(exc).__name__, "detail": str(exc)}
                )
                result.per_company[target.name] = {"error": str(exc)}
                continue

            result.jobs_seen += len(raws)
            if dry_run:
                result.per_company[target.name] = {"seen": len(raws), "new": 0, "closed": 0}
                continue

            with db.session_scope() as session:
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
    on_progress: Progress = _noop,
) -> ContactsResult:
    """Resolve a hiring contact for every company that has open jobs."""
    from sqlalchemy import select

    from .contacts.finder import find_contacts
    from .models import Company, Suppression, hash_email

    result = ContactsResult()
    by_name = {t.name: t for t in targets}

    # Only companies with something open: harvesting contacts for a company that
    # is not hiring has no legitimate-interest basis (docs/compliance.md).
    with db.session_scope() as session:
        rows = session.execute(
            select(Company.id, Company.name, Company.catch_all, Company.email_pattern)
            .join(Job, Job.company_id == Company.id)
            .where(Job.closed_at.is_(None))
            .distinct()
        ).all()
        companies = [
            {"id": r[0], "name": r[1], "catch_all": r[2], "email_pattern": r[3]} for r in rows
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

    async with PoliteClient() as client:
        for company in companies:
            target = by_name.get(company["name"])
            if target is None:
                continue
            result.companies_checked += 1
            on_progress(company["name"])

            job_texts = [
                (description, url)
                for description, url in job_context.get(company["id"], [])
                if description
            ]
            try:
                found = await find_contacts(
                    client,
                    target,
                    job_texts=job_texts,
                    known_catch_all=company["catch_all"],
                    known_pattern=company["email_pattern"],
                    verify_emails=verify_emails,
                    is_suppressed=suppressed,
                )
            except Exception as exc:  # noqa: BLE001 - one company must not end the run
                log.exception("%s: contact discovery failed", company["name"])
                result.errors.append(
                    {"company": company["name"], "error": type(exc).__name__, "detail": str(exc)}
                )
                continue

            surfaced = found.surfaced()
            if not surfaced:
                result.companies_without_contact.append(company["name"])
                result.per_company[company["name"]] = {"found": 0, "notes": found.notes}
                continue

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

    return result


def run_score(profile: Profile, *, dry_run: bool = False) -> dict:
    """Rescore every open job. Returns a summary, including the disqualified tally."""
    from sqlalchemy import select

    scored = 0
    disqualified = 0
    buckets = {"90+": 0, "70-89": 0, "50-69": 0, "1-49": 0, "0": 0}

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
            total = score.total
            key = (
                "90+" if total >= 90
                else "70-89" if total >= 70
                else "50-69" if total >= 50
                else "1-49" if total >= 1
                else "0"
            )
            buckets[key] += 1

    return {"scored": scored, "disqualified": disqualified, "buckets": buckets}
