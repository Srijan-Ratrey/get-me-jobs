"""FreeHire: one search across ~300 ATSs, including the ones we cannot crawl.

Every other adapter here reads one company's board. This one reads a catalogue.
That difference is the point: measured on 2026-09-01, of 645 India ML postings
from the last three weeks only 204 (31%) sat on Greenhouse, Lever, Ashby or
Workable. The other 441 were on Workday, Oracle, SmartRecruiters, Zoho Recruit,
Freshteam and Phenom — the systems Indian SMBs actually use, every one of which
either has no public API or (Darwinbox, SmartRecruiters) refuses identified bots
outright. See docs/sources.md for those two post-mortems.

Rather than lose that fight once per vendor, this reads the aggregate. FreeHire
publishes the whole catalogue as an unauthenticated JSON API and asks callers to
use it: its robots.txt says "you do not have to scrape these pages", documents
the rate budget, and requests a User-Agent naming the project — which is exactly
what PoliteClient already sends.

A FreeHire target is a saved search rather than a company:

    - name: FreeHire (India ML)
      ats: freehire
      search:
        category: [ml_ai, ai_engineering, data_science]
        countries: [in]
        posted_within_days: 21
"""
from __future__ import annotations

import logging
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx

from ..config import Target
from ..http import PoliteClient, SourceUnavailable
from ..models import RawJob
from .base import html_to_text, normalize_text, parse_iso

log = logging.getLogger(__name__)

SEARCH_URL = "https://freehire.me/api/v1/jobs/search"

# Filters the API actually reads. Anything outside this set is a typo, and a typo
# is dangerous here rather than merely wrong: FreeHire *ignores* unrecognised
# parameters instead of rejecting them, so `categories=ml_ai` (plural) silently
# returns the entire India catalogue — 22,233 rows — while `category=ml_ai`
# returns the 202 that were asked for. Both look like success. Checked live.
KNOWN_PARAMS = frozenset(
    """q category role seniority countries regions cities work_mode employment_type
    company_size company_type domains skills collections salary_currency is_tech
    visa_sponsorship relocation education_level english_level posting_language
    reality posted_within_days limit offset sort""".split()
)

# Rows whose "source" is an aggregator rather than the employer's own ATS. Their
# url is an affiliate redirect carrying utm_campaign=publisher, not the posting,
# so the link would send the user somewhere they cannot apply.
AGGREGATOR_PREFIXES = ("whatjobs",)

PAGE_SIZE = 100
DEFAULT_MAX_PAGES = 20


class FreeHireSource:
    """Search the FreeHire catalogue. Yields postings from many companies."""

    name = "freehire"

    # This source returns postings from many employers, not one board. Two
    # consequences the pipeline has to honour: each RawJob's company_name is the
    # real employer and decides which Company row it belongs to, and stale-closing
    # must be skipped — a search result is not an exhaustive list of anyone's
    # openings, so treating it as one would close every job the direct adapters
    # found at those same companies.
    is_catalogue = True

    def matches(self, target: Target) -> bool:
        return (target.ats or "").lower() == self.name

    async def fetch(self, client: PoliteClient, target: Target) -> list[RawJob]:
        search = dict(target.search or {})
        max_pages = int(search.pop("max_pages", DEFAULT_MAX_PAGES))
        params = _validated(search, target.name)

        jobs: list[RawJob] = []
        seen: set[str] = set()
        for page in range(max_pages):
            payload = await _page(client, params, offset=page * PAGE_SIZE)
            rows = payload.get("data") or []
            _warn_on_ignored(payload, target.name)
            for row in rows:
                raw = _to_raw(row)
                if raw is not None and raw.url not in seen:
                    seen.add(raw.url)
                    jobs.append(raw)
            total = (payload.get("meta") or {}).get("total")
            if len(rows) < PAGE_SIZE or (total is not None and (page + 1) * PAGE_SIZE >= total):
                break
        else:
            log.warning(
                "%s: stopped at the %d-page cap; raise search.max_pages to see the rest",
                target.name,
                max_pages,
            )
        return jobs


async def _page(client: PoliteClient, params: dict, *, offset: int) -> dict:
    query = urlencode({**params, "limit": PAGE_SIZE, "offset": offset}, doseq=True)
    try:
        payload = await client.get_json(f"{SEARCH_URL}?{query}")
    except httpx.HTTPError as exc:
        raise SourceUnavailable(f"freehire search failed: {exc}") from exc
    if not isinstance(payload, dict):
        raise SourceUnavailable(f"freehire returned {type(payload).__name__}, expected an object")
    return payload


def _validated(search: dict, who: str) -> dict:
    """Drop unknown filters before sending, and say so.

    Catching this client-side matters because the server will not: an unknown
    filter comes back applied-to-nothing with a plausible-looking total.
    """
    clean = {}
    for key, value in search.items():
        if key in KNOWN_PARAMS:
            clean[key] = value
        else:
            log.warning(
                "%s: ignoring unknown FreeHire filter %r — the API would have silently "
                "dropped it and returned far more than you asked for",
                who,
                key,
            )
    return clean


def _warn_on_ignored(payload: dict, who: str) -> None:
    ignored = (payload.get("meta") or {}).get("ignored_params")
    if ignored:
        names = [i.get("param") if isinstance(i, dict) else i for i in ignored]
        log.warning(
            "%s: FreeHire ignored %s — these results are UNFILTERED on those fields",
            who,
            names,
        )


def _clean_url(url: str | None) -> str | None:
    """Strip FreeHire's attribution parameter to get the employer's own link."""
    if not url:
        return None
    parts = urlsplit(url)
    kept = [
        pair
        for pair in parts.query.split("&")
        if pair and not pair.startswith(("utm_source=", "utm_campaign=", "utm_medium="))
    ]
    return urlunsplit(parts._replace(query="&".join(kept)))


def _posted_at(row: dict):
    """When the posting really appeared.

    FreeHire refreshes ``posted_at`` when a company reposts, and flags that in
    ``reality.fake_freshness``. Trusting the refreshed date would make a
    four-month-old listing look like it appeared today and defeat
    ``--posted-within``, so a flagged row falls back to first sighting.
    """
    reality = row.get("reality") or {}
    if reality.get("fake_freshness") and row.get("created_at"):
        return parse_iso(row["created_at"])
    return parse_iso(row.get("posted_at")) or parse_iso(row.get("created_at"))


def _to_raw(row: dict) -> RawJob | None:
    source = (row.get("source") or "").lower()
    if source.startswith(AGGREGATOR_PREFIXES):
        # An affiliate redirect, not a posting the candidate can apply through.
        return None
    url = _clean_url(row.get("url"))
    title = normalize_text(row.get("title"))
    if not url or not title:
        return None

    enrichment = row.get("enrichment") or {}
    work_mode = (enrichment.get("work_mode") or row.get("work_mode") or "").lower()
    return RawJob(
        # Provenance stays honest: the originating ATS, not "freehire", so the
        # export shows where the posting actually lives.
        source=f"freehire:{source}" if source else "freehire",
        external_id=row.get("external_id") or row.get("public_slug"),
        title=title,
        company_name=normalize_text(row.get("company")),
        location=normalize_text(row.get("location")) or _join(row.get("cities")),
        description=html_to_text(row.get("description")),
        url=url,
        posted_at=_posted_at(row),
        remote=work_mode == "remote" or None,
        seniority_hint=enrichment.get("seniority") or row.get("seniority"),
    )


def _join(values) -> str | None:
    if not isinstance(values, list):
        return None
    return normalize_text(", ".join(str(v) for v in values if v))
