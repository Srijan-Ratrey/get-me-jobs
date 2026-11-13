"""Zoho Recruit career sites. See docs/sources.md.

Every tenant serves its board at `/jobs/Careers`, and the page embeds the full
records, descriptions included, as JSON in a hidden input. One request reads
the whole board. A target names the tenant's host:

    - name: AuxoAI
      ats: zoho_recruit
      ats_token: https://auxoai.zohorecruit.com

robots.txt on every tenant allows `/jobs/` and disallows the rest.
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import urlparse

import httpx
from selectolax.lexbor import LexborHTMLParser

from ..config import Target
from ..http import PoliteClient, SourceUnavailable
from ..models import RawJob
from .base import html_to_text, join_locations, parse_iso

log = logging.getLogger(__name__)

# Zoho's data centres: .com, .in, .eu, .com.au, .jp, .ca, .sa, .com.cn.
TENANT_HOST = re.compile(r"^[\w-]+\.zohorecruit\.[a-z.]+$")


class ZohoRecruitSource:
    name = "zoho_recruit"

    def matches(self, target: Target) -> bool:
        return (target.ats or "").lower() == self.name

    async def fetch(self, client: PoliteClient, target: Target) -> list[RawJob]:
        origin = _origin(target.ats_token) or _origin(target.careers_url)
        if origin is None:
            raise SourceUnavailable(
                f"{target.name}: zoho_recruit needs a <tenant>.zohorecruit.<tld> URL"
            )
        try:
            html = await client.get(f"{origin}/jobs/Careers")
        except httpx.HTTPError as exc:
            raise SourceUnavailable(f"{target.name}: zoho_recruit fetch failed: {exc}") from exc
        records = parse_records(html)
        if records is None:
            raise SourceUnavailable(
                f"{target.name}: no job records embedded in {origin}/jobs/Careers"
            )
        # An unpublished opening is still in the page, but not open to applicants.
        return [_to_raw(origin, r) for r in records if r.get("Publish", True) and r.get("id")]


def _origin(value: str | None) -> str | None:
    if not value:
        return None
    host = urlparse(value if "://" in value else f"https://{value}").netloc
    return f"https://{host}" if TENANT_HOST.match(host) else None


def parse_records(html: str) -> list[dict] | None:
    """The job list among the page's hidden inputs: the array whose rows have Posting_Title."""
    for node in LexborHTMLParser(html).css('input[type="hidden"]'):
        value = (node.attributes.get("value") or "").strip()
        if not value.startswith("[{"):
            continue
        try:
            rows = json.loads(value)
        except json.JSONDecodeError:
            continue
        if rows and isinstance(rows[0], dict) and "Posting_Title" in rows[0]:
            return rows
    return None


def _to_raw(origin: str, record: dict) -> RawJob:
    return RawJob(
        source="zoho_recruit",
        external_id=str(record["id"]),
        title=record.get("Posting_Title") or record.get("Job_Opening_Name") or "",
        location=join_locations(record.get("City"), record.get("State"), record.get("Country")),
        description=html_to_text(record.get("Job_Description")),
        url=f"{origin}/jobs/Careers/{record['id']}",
        posted_at=parse_iso(record.get("Date_Opened")),
        remote=record.get("Remote_Job"),
    )
