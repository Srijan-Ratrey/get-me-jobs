"""SAP SuccessFactors career sites (Recruiting Marketing), read through their tile search.

See docs/sources.md. These are the branded boards large employers run on their
own domains (careers.capgemini.com, careers.ey.com, jobs.sap.com), all served
by one backend whose `/tile-search-results/` returns the listing as an HTML
fragment. A target names the site:

    - name: Capgemini
      ats: successfactors
      ats_token: https://careers.capgemini.com

Unlike Workday and Oracle, location filtering happens on the server
(`locationsearch=`, one search per place), so a global board costs only its
matching pages. Like theirs, `search.locations` defaults to the profile's. Tiles
carry little beyond a title, so each posting's page is fetched for its
schema.org fields, up to `max_details`.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from urllib.parse import urlencode, urljoin, urlparse

import httpx
from selectolax.lexbor import LexborHTMLParser

from ..config import Target
from ..http import PoliteClient, RobotsDisallowed, SourceUnavailable
from ..matching.scorer import place_aliases, title_excluded
from ..models import RawJob
from .base import join_locations, normalize_text

log = logging.getLogger(__name__)

JOB_ID = re.compile(r"/(\d+)/?$")
MAX_PAGES = 200  # a runaway guard; startrow paging can loop on a misbehaving tenant
DEFAULT_MAX_DETAILS = 50


class SuccessFactorsSource:
    name = "successfactors"

    def matches(self, target: Target) -> bool:
        return (target.ats or "").lower() == self.name

    async def fetch(self, client: PoliteClient, target: Target) -> list[RawJob]:
        origin = _origin(target.ats_token) or _origin(target.careers_url)
        if origin is None:
            raise SourceUnavailable(f"{target.name}: successfactors needs the career site's URL")

        max_details = int(target.search.get("max_details", DEFAULT_MAX_DETAILS))
        # "Remote" is a willingness, not a place: as a search it returns remote
        # roles worldwide. The scorer's location gate makes the same distinction.
        # Expanded with the scorer's aliases: a site that writes "Bangalore"
        # returns nothing for "Bengaluru". Short forms ("blr") are not searches.
        places = sorted(
            p for p in place_aliases(target.search.get("locations") or []) if len(p) > 3
        )
        tiles: dict[str, tuple[str, str, str | None]] = {}
        for place in places or [None]:
            for job_id, (title, path) in (await _tiles(client, origin, place, target.name)).items():
                tiles.setdefault(job_id, (title, path, place))

        skip_titles = list(target.search.get("exclude_titles") or [])
        jobs: list[RawJob] = []
        details = 0
        for job_id, (title, path, place) in tiles.items():
            url = urljoin(origin, path)
            page = None
            if details < max_details and not title_excluded(title, skip_titles):
                details += 1
                page = await _page(client, url, target.name)
            jobs.append(_to_raw(job_id, title, url, page, place))
        if details >= max_details:
            log.warning(
                "%s: fetched the %d-description cap; the rest have no description",
                target.name,
                max_details,
            )
        return jobs


def _origin(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlparse(value if "://" in value else f"https://{value}")
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else None


async def _tiles(
    client: PoliteClient, origin: str, place: str | None, who: str
) -> dict[str, tuple[str, str]]:
    """job id -> (title, path), walking startrow until a page adds nothing."""
    found: dict[str, tuple[str, str]] = {}
    startrow = 0
    for _ in range(MAX_PAGES):
        query = (
            {"q": "", "locationsearch": place, "startrow": startrow}
            if place
            else {"startrow": startrow}
        )
        try:
            html = await client.get(f"{origin}/tile-search-results/?{urlencode(query)}")
        except httpx.HTTPError as exc:
            raise SourceUnavailable(f"{who}: successfactors fetch failed: {exc}") from exc
        rows = parse_tiles(html)
        fresh = {k: v for k, v in rows.items() if k not in found}
        if not fresh:
            return found
        found.update(fresh)
        startrow += len(rows)
    log.warning("%s: stopped at the %d-page guard", who, MAX_PAGES)
    return found


def parse_tiles(html: str) -> dict[str, tuple[str, str]]:
    rows: dict[str, tuple[str, str]] = {}
    for tile in LexborHTMLParser(html).css("li.job-tile"):
        path = tile.attributes.get("data-url") or ""
        link = tile.css_first("a.jobTitle-link")
        if not (m := JOB_ID.search(path)) or link is None:
            continue
        rows[m[1]] = (normalize_text(link.text()) or "", path)
    return rows


async def _page(client: PoliteClient, url: str, who: str) -> str | None:
    try:
        return await client.get(url)
    except (httpx.HTTPError, RobotsDisallowed) as exc:
        log.info("%s: successfactors posting %s failed: %s", who, url, exc)
        return None


def _posted(value: str | None) -> datetime | None:
    """datePosted reads like "Mon Oct 26 02:00:00 UTC 2026"."""
    try:
        return datetime.strptime(value or "", "%a %b %d %H:%M:%S UTC %Y").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def _to_raw(job_id: str, title: str, url: str, page: str | None, place: str | None) -> RawJob:
    """Title and location feed content_hash, so they come from what every scan
    sees: the tile's title, and the place whose search found it. The posting
    page, fetched on some scans and not others, only adds description and date;
    its fuller address list is kept in the description's opening line.
    """
    description = posted = None
    if page:
        tree = LexborHTMLParser(page)
        places = [n.attributes.get("content") for n in tree.css('[itemprop="streetAddress"]')]
        body = tree.css_first('[itemprop="description"]')
        description = normalize_text(
            "\n\n".join(
                filter(
                    None,
                    [
                        f"Locations: {join_locations(*places)}" if any(places) else None,
                        body.text(separator="\n") if body else None,
                    ],
                )
            )
        )
        if node := tree.css_first('[itemprop="datePosted"]'):
            posted = _posted(node.attributes.get("content"))
    return RawJob(
        source="successfactors",
        external_id=job_id,
        title=title,
        location=place,
        description=description,
        url=url,
        posted_at=posted,
    )
