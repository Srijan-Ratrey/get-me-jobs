"""Workable public widget API. See docs/sources.md.

Written defensively: the live shape drops several fields the documentation shows
(no `id`, no `requirements`, no `benefits`), so every optional key is treated as
optional rather than indexed directly.
"""
from __future__ import annotations

import logging

import httpx

from ..config import Target
from ..http import PoliteClient, SourceUnavailable
from ..models import RawJob
from .base import html_to_text, join_locations, join_parts, parse_iso

log = logging.getLogger(__name__)

# Without ?details=true the description fields are absent entirely.
WIDGET_URL = "https://apply.workable.com/api/v1/widget/accounts/{token}?details=true"
V3_URL = "https://apply.workable.com/api/v3/accounts/{token}/jobs"


class WorkableSource:
    name = "workable"

    def matches(self, target: Target) -> bool:
        return (target.ats or "").lower() == self.name

    async def fetch(self, client: PoliteClient, target: Target) -> list[RawJob]:
        token = target.ats_token
        if not token:
            raise SourceUnavailable(f"{target.name}: workable needs an ats_token")

        try:
            data = await client.get_json(WIDGET_URL.format(token=token))
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise SourceUnavailable(
                    f"{target.name}: workable fetch failed: {exc}"
                ) from exc
            # Some accounts only answer on v3.
            log.debug("%s: workable v1 404, trying v3", target.name)
            try:
                data = await client.get_json(V3_URL.format(token=token))
            except httpx.HTTPError as exc3:
                raise SourceUnavailable(
                    f"{target.name}: workable v1 and v3 both failed: {exc3}"
                ) from exc3
        except httpx.HTTPError as exc:
            raise SourceUnavailable(f"{target.name}: workable fetch failed: {exc}") from exc

        # An empty jobs array is a valid account with nothing open, not an error.
        return [self._to_raw(job) for job in data.get("jobs") or []]

    def _to_raw(self, job: dict) -> RawJob:
        url = job.get("url") or job.get("shortlink") or job.get("application_url")
        if not url:
            raise SourceUnavailable(f"workable posting {job.get('shortcode')!r} has no URL")
        return RawJob(
            source=self.name,
            # `shortcode` is the only identifier; there is no `id` field.
            external_id=job.get("shortcode") or job.get("code"),
            title=job["title"],
            location=join_locations(job.get("city"), job.get("state"), job.get("country")),
            # requirements/benefits are frequently absent even with details=true.
            description=join_parts(
                html_to_text(job.get("description")),
                html_to_text(job.get("requirements")),
                html_to_text(job.get("benefits")),
            ),
            url=url,
            posted_at=parse_iso(job.get("published_on") or job.get("created_at")),
            remote=job.get("telecommuting"),
            # "Mid-Senior level" and friends: a free level signal.
            seniority_hint=job.get("experience"),
        )
