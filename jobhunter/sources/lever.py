"""Lever public postings API. See docs/sources.md."""
from __future__ import annotations

import logging

import httpx

from ..config import Target
from ..http import PoliteClient, SourceUnavailable
from ..models import RawJob
from .base import html_to_text, join_parts, parse_epoch_ms

log = logging.getLogger(__name__)

# Returns a bare JSON array. Do not add &group=team - it changes the shape.
POSTINGS_URL = "https://api.lever.co/v0/postings/{token}?mode=json"


class LeverSource:
    name = "lever"

    def matches(self, target: Target) -> bool:
        return (target.ats or "").lower() == self.name

    async def fetch(self, client: PoliteClient, target: Target) -> list[RawJob]:
        token = target.ats_token
        if not token:
            raise SourceUnavailable(f"{target.name}: lever needs an ats_token")

        try:
            data = await client.get_json(POSTINGS_URL.format(token=token))
        except httpx.HTTPError as exc:
            raise SourceUnavailable(f"{target.name}: lever fetch failed: {exc}") from exc
        if not isinstance(data, list):
            raise SourceUnavailable(f"{target.name}: lever returned {type(data).__name__}, not a list")

        return [self._to_raw(job) for job in data]

    def _to_raw(self, job: dict) -> RawJob:
        categories = job.get("categories") or {}
        workplace = (job.get("workplaceType") or "").lower()
        return RawJob(
            source=self.name,
            external_id=job["id"],
            # Lever's title field is `text`, not `title`.
            title=job["text"],
            location=categories.get("location"),
            # descriptionPlain alone omits the requirements, which live in
            # lists[] and are exactly what the scorer reads.
            description=join_parts(
                job.get("descriptionPlain"),
                *self._list_sections(job.get("lists") or []),
                job.get("additionalPlain"),
            ),
            url=job["hostedUrl"],
            posted_at=parse_epoch_ms(job.get("createdAt")),
            # Authoritative, and more reliable than string-matching the location.
            remote=True if workplace == "remote" else (False if workplace else None),
        )

    @staticmethod
    def _list_sections(lists: list[dict]) -> list[str | None]:
        parts: list[str | None] = []
        for section in lists:
            heading = (section.get("text") or "").strip()
            body = html_to_text(section.get("content"))
            if heading and body:
                parts.append(f"{heading}\n{body}")
            else:
                parts.append(body or heading or None)
        return parts
