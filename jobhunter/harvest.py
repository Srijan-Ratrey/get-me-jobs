"""Find companies that hire in India by probing public ATS board listings.

The watchlist is the pipeline's real constraint: a target it does not know about
is a job it can never surface. Guessing slugs from company names hit ~9%, so this
instead walks published lists of ATS board tokens and asks each board directly
whether it has India-located openings.

Two things make this cheap enough to be worth doing. Each board answers in a
single request, and that request can use the *listing* endpoint rather than the
adapters' full-content one — 358 KB versus 4.4 MB for a board the size of
Stripe's. Across 8,333 Greenhouse tokens that is the difference between ~3 GB and
~36 GB, which is why this module does not reuse ``JobSource.fetch``.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

import httpx

from .config import Profile, Target, append_targets, load_targets
from .http import PoliteClient, RobotsDisallowed
from .matching.scorer import _contains_word, matches_location

log = logging.getLogger(__name__)

# What counts as reachable. Shared with the scorer rather than reimplemented:
# a second location matcher is exactly the divergence that let "USA | Remote"
# into the shortlist. "Remote" is absent on purpose — matches_location already
# accepts a genuinely unanchored remote posting, and adding it here would make
# every US-remote board look like an India employer.
INDIA = ["Bengaluru", "India"]


def is_relevant_title(title: str, profile: Profile) -> bool:
    """Would this title be worth scoring at all?

    Deliberately the same two rules the scorer applies to a title — a substring
    match against ``profile.titles``, and a whole-word veto on
    ``exclude_keywords`` — so a board is kept for roles the scorer would go on to
    rank, not for roles it would immediately zero.
    """
    lowered = (title or "").lower()
    if not lowered:
        return False
    if any(_contains_word(lowered, word) for word in profile.exclude_keywords if word.strip()):
        return False
    return any(t.strip() and t.lower() in lowered for t in profile.titles)

# Listing endpoints. Deliberately not the adapters' URLs: no descriptions.
_ENDPOINTS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
    "lever": "https://api.lever.co/v0/postings/{token}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{token}",
}


@dataclass
class Probe:
    """One board's verdict. Serialised to the resume log verbatim."""

    ats: str
    token: str
    live: bool = False
    name: str | None = None
    total_jobs: int = 0
    india_jobs: int = 0
    relevant_india_jobs: int = 0
    error: str | None = None

    def as_target(self) -> Target:
        return Target(name=self.name or _name_from_token(self.token), ats=self.ats, ats_token=self.token)

    def worth_watching(self, *, min_relevant: int, min_india_jobs: int) -> bool:
        """Keep a board for a matching role now, or a durable India presence.

        The union is deliberate. Relevance alone would drop a company that posts
        nothing matching this week and an ML role next month; India volume alone
        selects for company size rather than fit, keeping Airbnb's fifteen
        non-ML India roles while dropping a five-person AI startup whose entire
        board is in Bengaluru.
        """
        return self.relevant_india_jobs >= min_relevant or self.india_jobs >= min_india_jobs


@dataclass
class HarvestResult:
    probes: list[Probe] = field(default_factory=list)
    added: int = 0
    skipped_known: int = 0
    resumed: int = 0

    @property
    def live(self) -> list[Probe]:
        return [p for p in self.probes if p.live]

    def hits(self, *, min_relevant: int = 1, min_india_jobs: int = 8) -> list[Probe]:
        return [
            p
            for p in self.live
            if p.worth_watching(min_relevant=min_relevant, min_india_jobs=min_india_jobs)
        ]

    def by_ats(self) -> dict[str, dict[str, int]]:
        """Per-ATS probed / live / India / relevant counts, for the summary table."""
        out: dict[str, dict[str, int]] = {}
        for p in self.probes:
            row = out.setdefault(p.ats, {"probed": 0, "live": 0, "india": 0, "relevant": 0})
            row["probed"] += 1
            row["live"] += int(p.live)
            row["india"] += int(p.india_jobs > 0)
            row["relevant"] += int(p.relevant_india_jobs > 0)
        return out


def _name_from_token(token: str) -> str:
    """Fallback display name. Lever and Ashby listings carry no company name."""
    return token.replace("-", " ").replace("_", " ").strip().title() or token


# --------------------------------------------------------------------------- #
# Per-ATS extraction: (company name or None, [(title, location text, remote)])
# --------------------------------------------------------------------------- #

Posting = tuple[str, str, bool]


def _from_greenhouse(data: object) -> tuple[str | None, list[Posting]]:
    jobs = (data or {}).get("jobs") or [] if isinstance(data, dict) else []
    name = next((j.get("company_name") for j in jobs if j.get("company_name")), None)
    return name, [
        (j.get("title") or "", (j.get("location") or {}).get("name") or "", False) for j in jobs
    ]


def _from_lever(data: object) -> tuple[str | None, list[Posting]]:
    jobs = data if isinstance(data, list) else []
    out: list[Posting] = []
    for job in jobs:
        categories = job.get("categories") or {}
        parts = [categories.get("location") or "", *(categories.get("allLocations") or [])]
        remote = (job.get("workplaceType") or "").lower() == "remote"
        # Lever calls the title "text".
        out.append((job.get("text") or "", ", ".join(p for p in parts if p), remote))
    return None, out


def _from_ashby(data: object) -> tuple[str | None, list[Posting]]:
    jobs = (data or {}).get("jobs") or [] if isinstance(data, dict) else []
    out: list[Posting] = []
    for job in jobs:
        parts = [job.get("location") or ""]
        for secondary in job.get("secondaryLocations") or []:
            # Shape has drifted between an object and a bare string; accept both.
            parts.append(secondary.get("location", "") if isinstance(secondary, dict) else str(secondary))
        out.append(
            (job.get("title") or "", ", ".join(p for p in parts if p), bool(job.get("isRemote")))
        )
    return None, out


_EXTRACTORS: dict[str, Callable[[object], tuple[str | None, list[Posting]]]] = {
    "greenhouse": _from_greenhouse,
    "lever": _from_lever,
    "ashby": _from_ashby,
}


# --------------------------------------------------------------------------- #
# Probing
# --------------------------------------------------------------------------- #


async def probe_board(
    client: PoliteClient, ats: str, token: str, profile: Profile | None = None
) -> Probe:
    """Ask one board how many of its openings are reachable from India.

    Counts both India-located postings and the subset whose titles the profile
    would actually rank, because the two answer different questions: the first
    is "do they hire here at all", the second "do they have something for me
    right now". Both come from the one response already in hand.

    A dead token is the common case, not an error: these lists are harvested from
    crawl data and go stale. Anything that fails is recorded and the sweep
    continues, because one 404 among thousands must never end the run.
    """
    probe = Probe(ats=ats, token=token)
    try:
        # use_cache=False: every board is probed exactly once per sweep and the
        # resume log — not the HTTP cache — is what stops repeat work across
        # runs. Caching here would write hundreds of MB that is never read back
        # and evict entries `scan` genuinely reuses.
        data = await client.get_json(_ENDPOINTS[ats].format(token=token), use_cache=False)
    except RobotsDisallowed:
        probe.error = "robots"
        return probe
    except (httpx.HTTPError, ValueError) as exc:
        probe.error = type(exc).__name__
        return probe

    name, postings = _EXTRACTORS[ats](data)
    india = [p for p in postings if matches_location(p[1], p[2], INDIA)]
    probe.live = True
    probe.name = name
    probe.total_jobs = len(postings)
    probe.india_jobs = len(india)
    if profile is not None:
        probe.relevant_india_jobs = sum(1 for title, _, _ in india if is_relevant_title(title, profile))
    return probe


# --------------------------------------------------------------------------- #
# Resume log
# --------------------------------------------------------------------------- #


def load_state(path: str | Path) -> dict[tuple[str, str], Probe]:
    """Read the resume log. A truncated final line is expected after a crash."""
    state: dict[tuple[str, str], Probe] = {}
    file = Path(path)
    if not file.exists():
        return state
    for line in file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            probe = Probe(**json.loads(line))
        except (json.JSONDecodeError, TypeError):
            continue
        state[(probe.ats, probe.token)] = probe
    return state


def _append_state(path: Path, probe: Probe) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(probe.__dict__, ensure_ascii=False) + "\n")


def load_tokens(directory: str | Path, ats_names: Sequence[str]) -> dict[str, list[str]]:
    """Read `<ats>_companies.json` files, each a flat JSON array of tokens."""
    out: dict[str, list[str]] = {}
    for ats in ats_names:
        file = Path(directory) / f"{ats}_companies.json"
        if not file.exists():
            log.warning("no token list for %s at %s", ats, file)
            continue
        data = json.loads(file.read_text(encoding="utf-8"))
        out[ats] = [str(t).strip() for t in data if str(t).strip()]
    return out


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


async def run_harvest(
    tokens: dict[str, list[str]],
    *,
    companies_path: str | Path = "companies.yaml",
    state_path: str | Path = ".harvest-state.jsonl",
    profile: Profile | None = None,
    min_relevant: int = 1,
    min_india_jobs: int = 8,
    limit: int | None = None,
    dry_run: bool = False,
    client: PoliteClient | None = None,
    on_progress: Callable[[Probe], None] | None = None,
) -> HarvestResult:
    """Probe every token and add the India-hiring companies to the watchlist.

    One worker per ATS rather than one task per token: PoliteClient rate-limits
    per host, so the three boards' hosts proceed in parallel while each host's
    own queue stays serial. 15,862 simultaneous coroutines contending for one
    semaphore would buy nothing.
    """
    result = HarvestResult()
    state_file = Path(state_path)
    done = load_state(state_file)
    result.resumed = len(done)

    known = {(t.ats, t.ats_token) for t in load_targets(companies_path)} if Path(companies_path).exists() else set()

    owns_client = client is None
    client = client or PoliteClient()
    if owns_client:
        await client.__aenter__()
    try:

        async def sweep(ats: str, board_tokens: list[str]) -> None:
            for token in board_tokens[:limit]:
                if (ats, token) in done:
                    result.probes.append(done[(ats, token)])
                    continue
                if (ats, token) in known:
                    result.skipped_known += 1
                    continue
                probe = await probe_board(client, ats, token, profile)
                result.probes.append(probe)
                if not dry_run:
                    _append_state(state_file, probe)
                if on_progress:
                    on_progress(probe)

        await asyncio.gather(*(sweep(ats, t) for ats, t in tokens.items() if ats in _ENDPOINTS))
    finally:
        if owns_client:
            await client.__aexit__(None, None, None)

    hits = [
        p
        for p in result.hits(min_relevant=min_relevant, min_india_jobs=min_india_jobs)
        if (p.ats, p.token) not in known
    ]
    if hits and not dry_run:
        matching = sum(1 for p in hits if p.relevant_india_jobs)
        result.added = append_targets(
            companies_path,
            [p.as_target() for p in hits],
            note=(
                f"appended by `jobhunter harvest` ({len(hits)} boards: {matching} with a matching "
                f"India role, {len(hits) - matching} on India volume alone)"
            ),
        )
    return result
