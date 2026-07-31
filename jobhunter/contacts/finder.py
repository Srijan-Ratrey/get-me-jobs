"""Orchestrate the three tiers.

Goal: **one high-confidence contact per company, not fifty guesses.** A guessed
address presented as a real one is worse than no answer, because guesses bounce
and bounces damage the sending reputation you are trying to spend.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from ..config import Target, settings
from ..http import PoliteClient
from . import patterns, verify
from .scraper import Candidate, scrape_company

log = logging.getLogger(__name__)

# Stop as soon as something this good is in hand: no reason to run a more
# speculative, more expensive tier.
GOOD_ENOUGH = 0.85
# Below this, say "no contact found" rather than offer a guess.
SURFACE_THRESHOLD = 0.55


@dataclass
class FindResult:
    candidates: list[Candidate] = field(default_factory=list)
    catch_all: bool | None = None
    email_pattern: str | None = None
    notes: list[str] = field(default_factory=list)

    def surfaced(self) -> list[Candidate]:
        """Only Tier-1 hits and verified Tier-2 hits are actionable."""
        return [c for c in self.candidates if c.confidence >= SURFACE_THRESHOLD]


def _domain_for(target: Target, job_urls: list[str]) -> str | None:
    """Prefer the configured domain; fall back to nothing rather than guessing.

    Deriving the domain from a job URL would yield the ATS's domain
    (jobs.lever.co), which is not the company and must never be probed as if it
    were.
    """
    if target.domain:
        return target.domain.lower().strip().removeprefix("www.")
    return None


async def find_contacts(
    client: PoliteClient,
    target: Target,
    *,
    job_texts: list[tuple[str, str]] | None = None,
    known_addresses: list[tuple[str, str]] | None = None,
    known_catch_all: bool | None = None,
    known_pattern: str | None = None,
    person_names: list[tuple[str, str]] | None = None,
    verify_emails: bool | None = None,
    is_suppressed: Callable[[str], bool] | None = None,
) -> FindResult:
    """Run the tiers in order, stopping as soon as the answer is good enough.

    ``known_addresses`` are (full_name, email) pairs already trusted for this
    domain, used to infer the house pattern. ``person_names`` are (full_name,
    source_url) pairs found on a team page or in a posting — never invented.
    """
    result = FindResult(catch_all=known_catch_all, email_pattern=known_pattern)
    domain = _domain_for(target, [])
    if not domain:
        result.notes.append("no domain configured; cannot scrape or verify")
        return result

    def keep(email: str) -> bool:
        """Drop anything on the suppression list.

        Applied at the point of discovery, not just before writing: an address
        someone asked to be forgotten must not be displayed, exported, or — worst
        of all — SMTP-probed. Refusing the DB write alone is not erasure.
        """
        if is_suppressed is not None and is_suppressed(email):
            log.info("dropping suppressed address for %s", target.name)
            return False
        return True

    # ---- Tier 1: published addresses ------------------------------------- #
    tier1 = [
        c
        for c in await scrape_company(
            client,
            domain=domain,
            extra_pages=target.contact_pages,
            job_texts=job_texts,
        )
        if keep(c.email)
    ]
    result.candidates.extend(tier1)
    if tier1:
        result.notes.append(f"tier1: {len(tier1)} published address(es)")

    best = max((c.confidence for c in result.candidates), default=0.0)
    if best >= GOOD_ENOUGH:
        # A role address on the company domain: stop here, it is the right answer.
        result.candidates.sort(key=lambda c: c.confidence, reverse=True)
        return result

    # ---- Tier 2: pattern generation -------------------------------------- #
    if not settings.enable_pattern_guessing:
        result.notes.append("pattern guessing disabled")
    elif not person_names:
        # Never generate patterns from a name you invented.
        result.notes.append("tier2 skipped: no real person name available")
    else:
        # Infer before you guess. Any known-good address at this domain turns
        # twelve low-confidence guesses into one high-confidence candidate.
        directory = list(known_addresses or [])
        directory += [
            (f"{c.first_name} {c.last_name}", c.email)
            for c in tier1
            if c.kind == "person" and c.first_name and c.last_name
        ]
        pattern = known_pattern or patterns.infer_pattern_from_directory(directory)
        if pattern:
            result.email_pattern = pattern
            result.notes.append(f"tier2: inferred house pattern {pattern!r}")

        for full_name, source_url in person_names:
            for email, confidence, used in patterns.generate(
                full_name, domain, known_pattern=pattern
            ):
                if not keep(email):
                    continue
                parts = patterns.normalize_name(full_name)
                result.candidates.append(
                    Candidate(
                        email=email,
                        kind="person",
                        confidence=confidence,
                        discovery_method=f"pattern:{used}" if not pattern else f"inferred:{used}",
                        source_url=source_url,
                        first_name=parts.first.capitalize() if parts else None,
                        last_name=parts.last.capitalize() if parts else None,
                    )
                )

    # ---- Tier 3: verification (opt-in) ----------------------------------- #
    should_verify = settings.verify_emails if verify_emails is None else verify_emails
    unproven = [c for c in result.candidates if c.confidence < SURFACE_THRESHOLD]
    if not should_verify:
        if unproven:
            result.notes.append(
                f"{len(unproven)} guess(es) left unverified (verify_emails is off)"
            )
    elif unproven:
        ordered = sorted(unproven, key=lambda c: c.confidence, reverse=True)
        verdicts, catch_all = verify.verify_candidates(
            domain,
            [c.email for c in ordered],
            known_catch_all=result.catch_all,
        )
        result.catch_all = catch_all
        for candidate in ordered:
            verdict = verdicts.get(candidate.email)
            if verdict is None:
                continue
            if verdict.status == verify.VALID:
                # Promoted: a probe confirmed a real mailbox.
                candidate.confidence = max(candidate.confidence, GOOD_ENOUGH)
                candidate.discovery_method = f"{candidate.discovery_method}+verified"
            elif verdict.status == verify.INVALID:
                candidate.confidence = 0.0
            result.notes.append(f"verify {candidate.email}: {verdict.status} ({verdict.detail})")
        if catch_all:
            result.notes.append("catch-all domain: guesses stay risky, never valid")

    result.candidates = [c for c in result.candidates if c.confidence > 0.0]
    result.candidates.sort(key=lambda c: c.confidence, reverse=True)
    return result
