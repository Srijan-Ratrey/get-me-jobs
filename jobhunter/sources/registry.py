"""Dispatch a Target to the adapter that handles it.

Import direction is one-way (registry -> adapters), which is why the
fingerprint handoff lives here rather than inside careers_page.
"""

from __future__ import annotations

import logging

import httpx

from ..config import Target
from ..http import PoliteClient, RobotsDisallowed, SourceUnavailable
from ..models import RawJob
from .ashby import AshbySource
from .base import JobSource
from .careers_page import CareersPageSource, fingerprint
from .freehire import FreeHireSource
from .greenhouse import GreenhouseSource
from .lever import LeverSource
from .oracle import OracleSource
from .successfactors import SuccessFactorsSource
from .workable import WorkableSource
from .workday import WorkdaySource
from .zoho_recruit import ZohoRecruitSource

log = logging.getLogger(__name__)

ADAPTERS: list[JobSource] = [
    GreenhouseSource(),
    LeverSource(),
    AshbySource(),
    WorkableSource(),
    WorkdaySource(),
    OracleSource(),
    SuccessFactorsSource(),
    ZohoRecruitSource(),
    FreeHireSource(),
]

# Adapters that read a board from its URL, so a fingerprint with no token can
# still hand off: the careers page they were found on is the board.
ADDRESSED_BY_URL = frozenset({"successfactors"})

BY_NAME: dict[str, JobSource] = {a.name: a for a in ADAPTERS}

CAREERS_PAGE = CareersPageSource()


def resolve(target: Target) -> JobSource | None:
    """Pick the adapter for a target by its declared `ats`."""
    for adapter in ADAPTERS:
        if adapter.matches(target):
            return adapter
    return None


async def fetch_target(client: PoliteClient, target: Target) -> list[RawJob]:
    """Fetch one target's postings, fingerprinting the careers page if needed."""
    if adapter := resolve(target):
        return await adapter.fetch(client, target)

    if target.ats:
        # An explicit but unsupported ATS is a configuration answer, not a crawl.
        raise SourceUnavailable(
            f"{target.name}: no adapter for ats={target.ats!r}. "
            "See docs/sources.md for which sources are supported."
        )

    if not target.careers_url:
        raise SourceUnavailable(
            f"{target.name}: needs either a supported ats + ats_token, or a careers_url"
        )

    try:
        html = await client.get(target.careers_url)
    except RobotsDisallowed:
        raise
    except httpx.HTTPError as exc:
        raise SourceUnavailable(f"{target.name}: careers page fetch failed: {exc}") from exc

    if detected := fingerprint(html) or fingerprint(target.careers_url):
        if detected.supported and (detected.token or detected.ats in ADDRESSED_BY_URL):
            # Always better than scraping: hand off to the real API.
            log.info(
                "%s: fingerprinted %s (token=%s), handing off",
                target.name,
                detected.ats,
                detected.token,
            )
            handoff = target.model_copy(update={"ats": detected.ats, "ats_token": detected.token})
            if adapter := resolve(handoff):
                return await adapter.fetch(client, handoff)
        if not detected.supported:
            raise SourceUnavailable(
                f"{target.name}: uses {detected.ats}, which has no adapter "
                f"(matched {detected.marker!r})"
            )
        log.info(
            "%s: %s detected but no token found; falling back to crawling",
            target.name,
            detected.ats,
        )

    return await CAREERS_PAGE.parse(client, target, html, target.careers_url)
