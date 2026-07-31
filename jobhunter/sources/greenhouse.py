"""Greenhouse public job board API. See docs/sources.md."""
from __future__ import annotations

import logging

import httpx

from ..config import Target
from ..http import PoliteClient, SourceUnavailable
from ..models import RawJob
from .base import html_to_text, parse_iso

log = logging.getLogger(__name__)

# content=true inlines the description and saves one request per job.
BOARD_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"


class GreenhouseSource:
    name = "greenhouse"

    def matches(self, target: Target) -> bool:
        return (target.ats or "").lower() == self.name

    async def fetch(self, client: PoliteClient, target: Target) -> list[RawJob]:
        token = target.ats_token
        if not token:
            raise SourceUnavailable(f"{target.name}: greenhouse needs an ats_token")

        try:
            data = await client.get_json(BOARD_URL.format(token=token))
        except httpx.HTTPError as exc:
            raise SourceUnavailable(f"{target.name}: greenhouse fetch failed: {exc}") from exc

        return [self._to_raw(job) for job in data.get("jobs") or []]

    def _to_raw(self, job: dict) -> RawJob:
        return RawJob(
            source=self.name,
            external_id=str(job["id"]),
            title=job["title"],
            location=(job.get("location") or {}).get("name"),
            # `content` is entity-escaped HTML: unescape before stripping tags,
            # or the parser sees literal &lt;p&gt; and strips nothing.
            description=html_to_text(job.get("content"), unescape_first=True),
            url=job["absolute_url"],
            # first_published is the actual post date; updated_at is last-touch.
            posted_at=parse_iso(job.get("first_published") or job.get("updated_at")),
            company_name=job.get("company_name"),
        )
