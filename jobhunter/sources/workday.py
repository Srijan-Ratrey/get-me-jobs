"""Workday's CXS search API, the JSON endpoint behind every *.myworkdayjobs.com board.

See docs/sources.md. A target names its board by URL, in `ats_token` or
`careers_url`:

    - name: Genpact
      ats: workday
      ats_token: https://genpact.wd108.myworkdayjobs.com/External_Careers

The listing carries no description, so each posting costs one more request, and
at one request per second a 1,779-posting board would take half an hour.
`search.locations` keeps that to the postings worth scoring: a posting is
fetched in full only when its listed location matches, and `max_details` caps
what is left. It defaults to the profile's locations (see `run_scan`), which is
exactly the scorer's gate, so the filter drops nothing that could have scored.
`search.exclude_titles` (by default the profile's exclude_keywords) skips the
description of a posting its title already zeroes; the posting is still kept. Past the cap a posting is still returned, without a description,
because leaving it out would make stale-closing mark a live job closed.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from ..config import Target
from ..http import PoliteClient, RobotsDisallowed, SourceUnavailable
from ..matching.scorer import matches_location, title_excluded
from ..models import RawJob
from .base import html_to_text, join_locations, parse_iso

log = logging.getLogger(__name__)

# The locale segment is optional and ignored: one board answers every locale.
BOARD = re.compile(
    r"^(?:https?://)?(?P<host>(?P<tenant>[\w-]+)\.wd\d+\.myworkdayjobs\.com)"
    r"(?:/[a-z]{2}-[A-Z]{2})?/(?P<site>[\w-]+)"
)
# The listing says "2 Locations" instead of naming them; only the detail does.
SEVERAL_PLACES = re.compile(r"^\d+ Locations?$")
POSTED_DAYS_AGO = re.compile(r"Posted (\d+) Days? Ago")

PAGE_SIZE = 20  # the endpoint rejects anything larger
DEFAULT_MAX_DETAILS = 50


@dataclass(frozen=True)
class Board:
    host: str
    tenant: str
    site: str

    @property
    def url(self) -> str:
        return f"https://{self.host}/{self.site}"

    @property
    def api(self) -> str:
        return f"https://{self.host}/wday/cxs/{self.tenant}/{self.site}"


def parse_board(value: str | None) -> Board | None:
    if not value or not (m := BOARD.match(value.strip())):
        return None
    return Board(host=m["host"], tenant=m["tenant"], site=m["site"])


class WorkdaySource:
    name = "workday"

    def matches(self, target: Target) -> bool:
        return (target.ats or "").lower() == self.name

    async def fetch(self, client: PoliteClient, target: Target) -> list[RawJob]:
        board = parse_board(target.ats_token) or parse_board(target.careers_url)
        if board is None:
            raise SourceUnavailable(
                f"{target.name}: workday needs a https://<tenant>.wdN.myworkdayjobs.com/<site> URL"
            )
        # The API path is never in robots.txt; the board's public path is.
        # With the trailing slash: "Disallow: /2/" does not cover "/2".
        if not await client.allowed(f"{board.url}/"):
            raise RobotsDisallowed(board.url)

        wanted = list(target.search.get("locations") or [])
        skip_titles = list(target.search.get("exclude_titles") or [])
        max_details = int(target.search.get("max_details", DEFAULT_MAX_DETAILS))

        jobs: list[RawJob] = []
        details = 0
        for posting in await _list(client, board, target.name):
            listed = posting.get("locationsText") or ""
            several = bool(SEVERAL_PLACES.match(listed))
            if wanted and not several and not matches_location(listed, False, wanted):
                continue
            info = None
            if details < max_details and not title_excluded(posting["title"], skip_titles):
                details += 1
                info = await _detail(client, board, posting["externalPath"], target.name)
            if wanted and several and not _wanted_detail(info, wanted):
                # Unconfirmed: "4 Locations" may well be four cities abroad.
                continue
            jobs.append(_to_raw(board, posting, info))

        if details >= max_details:
            log.warning(
                "%s: fetched the %d-description cap; the rest have no description",
                target.name,
                max_details,
            )
        return jobs


async def _list(client: PoliteClient, board: Board, who: str) -> list[dict]:
    postings: list[dict] = []
    seen: set[str] = set()
    total: int | None = None
    offset = 0
    while True:
        payload = {"appliedFacets": {}, "limit": PAGE_SIZE, "offset": offset, "searchText": ""}
        try:
            page = await client.post_json(f"{board.api}/jobs", payload)
        except httpx.HTTPError as exc:
            raise SourceUnavailable(f"{who}: workday fetch failed: {exc}") from exc
        rows = page.get("jobPostings") or []
        # Only the first page reports the real total; later ones send 0.
        total = total if total is not None else page.get("total")
        fresh = [r for r in rows if r.get("externalPath") and r["externalPath"] not in seen]
        seen.update(r["externalPath"] for r in fresh)
        postings.extend(fresh)
        offset += PAGE_SIZE
        # Some tenants stop paginating at offset 2000 and replay page one instead,
        # which is why a page with nothing new also ends the walk.
        if len(rows) < PAGE_SIZE or not fresh or (total and offset >= total):
            return postings


async def _detail(client: PoliteClient, board: Board, path: str, who: str) -> dict | None:
    """A posting's full record, or None: one bad posting must not cost the board."""
    try:
        data = await client.get_json(f"{board.api}{path}")
    except (httpx.HTTPError, SourceUnavailable) as exc:
        log.info("%s: workday detail %s failed: %s", who, path, exc)
        return None
    return data.get("jobPostingInfo") if isinstance(data, dict) else None


def _places(info: dict) -> str | None:
    country = (info.get("country") or {}).get("descriptor")
    return join_locations(info.get("location"), *(info.get("additionalLocations") or []), country)


def _wanted_detail(info: dict | None, wanted: list[str]) -> bool:
    return bool(info) and matches_location(_places(info), False, wanted)


def _posted_on(label: str | None, now: datetime | None = None) -> datetime | None:
    """The listing's relative date. "30+ Days Ago" has no lower bound, so None."""
    now = now or datetime.now(timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if label == "Posted Today":
        return today
    if label == "Posted Yesterday":
        return today - timedelta(days=1)
    if label and (m := POSTED_DAYS_AGO.fullmatch(label)):
        return today - timedelta(days=int(m[1]))
    return None


def _to_raw(board: Board, posting: dict, info: dict | None) -> RawJob:
    info = info or {}
    remote_type = info.get("remoteType")
    return RawJob(
        source="workday",
        # The path is the one identifier every listing row carries.
        external_id=posting["externalPath"],
        title=info.get("title") or posting["title"],
        location=_places(info) if info else posting.get("locationsText"),
        description=html_to_text(info.get("jobDescription")),
        url=info.get("externalUrl") or f"{board.url}{posting['externalPath']}",
        posted_at=parse_iso(info.get("startDate")) or _posted_on(posting.get("postedOn")),
        remote=(remote_type == "Remote") if remote_type else None,
    )
