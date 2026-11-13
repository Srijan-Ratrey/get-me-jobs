"""Oracle Recruiting Cloud's public requisitions API. See docs/sources.md.

A target names its board by the Candidate Experience URL, whose path carries the
site number the API needs:

    - name: American Express
      ats: oracle
      ats_token: https://egug.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/jobsearch

Like Workday, the listing has no description: each posting costs a second
request, so `search.locations` (by default the profile's) decides which
postings get one. Some tenants sit
behind a WAF that refuses an identified bot outright (JPMorgan's answers our
User-Agent with "Blocked by WAF4SaaS", robots.txt included); those surface as
SourceUnavailable and are not worth working around.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import httpx

from ..config import Target
from ..http import PoliteClient, RobotsDisallowed, SourceUnavailable
from ..matching.scorer import matches_location, title_excluded
from ..models import RawJob
from .base import html_to_text, join_locations, join_parts, parse_iso

log = logging.getLogger(__name__)

BOARD = re.compile(
    r"^(?:https?://)?(?P<host>[\w.-]+\.oraclecloud\.com)"
    r"/hcmUI/CandidateExperience/(?P<lang>[\w-]+)/sites/(?P<site>[\w-]+)"
)
# Without `expand` the API returns a count and an empty requisition list.
LIST_QUERY = (
    "onlyData=true&expand=requisitionList.secondaryLocations"
    "&finder=findReqs;siteNumber={site},limit={limit},sortBy=POSTING_DATES_DESC,offset={offset}"
)
DETAIL_QUERY = 'expand=all&onlyData=true&finder=ById;Id="{id}",siteNumber={site}'
API = "https://{host}/hcmRestApi/resources/latest"

PAGE_SIZE = 25
DEFAULT_MAX_DETAILS = 50


@dataclass(frozen=True)
class Board:
    host: str
    lang: str
    site: str

    @property
    def url(self) -> str:
        return f"https://{self.host}/hcmUI/CandidateExperience/{self.lang}/sites/{self.site}"


def parse_board(value: str | None) -> Board | None:
    if not value or not (m := BOARD.match(value.strip())):
        return None
    return Board(host=m["host"], lang=m["lang"], site=m["site"])


class OracleSource:
    name = "oracle"

    def matches(self, target: Target) -> bool:
        return (target.ats or "").lower() == self.name

    async def fetch(self, client: PoliteClient, target: Target) -> list[RawJob]:
        board = parse_board(target.ats_token) or parse_board(target.careers_url)
        if board is None:
            raise SourceUnavailable(
                f"{target.name}: oracle needs a .../hcmUI/CandidateExperience/<lang>/sites/<site> URL"
            )
        # With the trailing slash: "Disallow: /2/" does not cover "/2".
        if not await client.allowed(f"{board.url}/"):
            raise RobotsDisallowed(board.url)

        wanted = list(target.search.get("locations") or [])
        skip_titles = list(target.search.get("exclude_titles") or [])
        max_details = int(target.search.get("max_details", DEFAULT_MAX_DETAILS))

        jobs: list[RawJob] = []
        details = 0
        for req in await _list(client, board, target.name):
            if wanted and not matches_location(_places(req), _remote(req), wanted):
                continue
            info = None
            if details < max_details and not title_excluded(req["Title"], skip_titles):
                details += 1
                info = await _detail(client, board, req["Id"], target.name)
            jobs.append(_to_raw(board, req, info))
        if details >= max_details:
            log.warning(
                "%s: fetched the %d-description cap; the rest have no description",
                target.name,
                max_details,
            )
        return jobs


async def _list(client: PoliteClient, board: Board, who: str) -> list[dict]:
    reqs: list[dict] = []
    seen: set[str] = set()
    offset = 0
    while True:
        query = LIST_QUERY.format(site=board.site, limit=PAGE_SIZE, offset=offset)
        try:
            data = await client.get_json(
                f"{API.format(host=board.host)}/recruitingCEJobRequisitions?{query}"
            )
        except httpx.HTTPError as exc:
            raise SourceUnavailable(f"{who}: oracle fetch failed: {exc}") from exc
        search = (data.get("items") or [{}])[0]
        rows = search.get("requisitionList") or []
        fresh = [r for r in rows if r.get("Id") and r["Id"] not in seen]
        seen.update(r["Id"] for r in fresh)
        reqs.extend(fresh)
        offset += PAGE_SIZE
        total = search.get("TotalJobsCount")
        if len(rows) < PAGE_SIZE or not fresh or (total is not None and offset >= total):
            return reqs


async def _detail(client: PoliteClient, board: Board, req_id: str, who: str) -> dict | None:
    query = DETAIL_QUERY.format(id=req_id, site=board.site)
    try:
        data = await client.get_json(
            f"{API.format(host=board.host)}/recruitingCEJobRequisitionDetails?{query}"
        )
    except (httpx.HTTPError, SourceUnavailable) as exc:
        log.info("%s: oracle detail %s failed: %s", who, req_id, exc)
        return None
    items = data.get("items") if isinstance(data, dict) else None
    return items[0] if items else None


def _places(req: dict) -> str | None:
    secondary = [s.get("Name") for s in req.get("secondaryLocations") or []]
    return join_locations(req.get("PrimaryLocation"), *secondary)


def _remote(req: dict) -> bool:
    return req.get("WorkplaceTypeCode") == "ORA_REMOTE"


def _to_raw(board: Board, req: dict, info: dict | None) -> RawJob:
    info = info or {}
    description = join_parts(
        html_to_text(info.get("ExternalDescriptionStr")),
        html_to_text(info.get("ExternalResponsibilitiesStr")),
        html_to_text(info.get("ExternalQualificationsStr")),
    )
    return RawJob(
        source="oracle",
        external_id=req["Id"],
        title=req["Title"],
        location=_places(req),
        description=description or html_to_text(req.get("ShortDescriptionStr")),
        url=f"{board.url}/job/{req['Id']}",
        posted_at=parse_iso(info.get("ExternalPostedStartDate") or req.get("PostedDate")),
        remote=_remote(req) if req.get("WorkplaceTypeCode") else None,
    )
