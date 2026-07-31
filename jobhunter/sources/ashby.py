"""Ashby public job board API. See docs/sources.md."""
from __future__ import annotations

import logging

import httpx

from ..config import Target
from ..http import PoliteClient, SourceUnavailable
from ..models import RawJob
from .base import join_locations, normalize_text, parse_iso

log = logging.getLogger(__name__)

BOARD_URL = "https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"


class AshbySource:
    name = "ashby"

    def matches(self, target: Target) -> bool:
        return (target.ats or "").lower() == self.name

    async def fetch(self, client: PoliteClient, target: Target) -> list[RawJob]:
        token = target.ats_token
        if not token:
            raise SourceUnavailable(f"{target.name}: ashby needs an ats_token")

        try:
            data = await client.get_json(BOARD_URL.format(token=token))
        except httpx.HTTPError as exc:
            raise SourceUnavailable(f"{target.name}: ashby fetch failed: {exc}") from exc

        # isListed == false is an unlisted/internal posting. The live endpoint
        # appears to filter these already, but relying on an undocumented
        # server-side filter to avoid surfacing internal roles is a bad trade.
        return [
            self._to_raw(job) for job in data.get("jobs") or [] if job.get("isListed") is not False
        ]

    def _to_raw(self, job: dict) -> RawJob:
        secondary = [
            (loc or {}).get("location") for loc in job.get("secondaryLocations") or []
        ]
        return RawJob(
            source=self.name,
            external_id=job["id"],
            title=job["title"],
            location=join_locations(job.get("location"), *secondary),
            description=normalize_text(job.get("descriptionPlain")),
            url=job["jobUrl"],
            posted_at=parse_iso(job.get("publishedAt")),
            # isRemote is authoritative; prefer it over keyword detection.
            remote=job.get("isRemote"),
        )
