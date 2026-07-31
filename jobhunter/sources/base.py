"""The adapter contract, plus the HTML/date helpers every adapter needs.

Adapters return ``list[RawJob]`` and never touch the DB: normalization and
persistence belong to ``db.upsert_job``, which keeps adapters trivially testable
against saved fixtures.
"""
from __future__ import annotations

import html as html_module
import logging
import re
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from selectolax.lexbor import LexborHTMLParser

from ..config import Target
from ..http import PoliteClient
from ..models import RawJob

log = logging.getLogger(__name__)

_BLANK_RUN = re.compile(r"\n{3,}")
_SPACES = re.compile(r"[ \t ]+")


@runtime_checkable
class JobSource(Protocol):
    """One ATS or crawl strategy. Adding a new source should be ~40 lines."""

    name: str

    def matches(self, target: Target) -> bool:
        """True if this adapter handles the target."""
        ...

    async def fetch(self, client: PoliteClient, target: Target) -> list[RawJob]:
        """Fetch postings. Raises SourceUnavailable on a skippable failure."""
        ...


def html_to_text(fragment: str | None, *, unescape_first: bool = False) -> str | None:
    """Flatten an HTML fragment to plain text.

    Descriptions feed both the scorer and the Tier-1 contact scraper, and both
    want text rather than markup. ``unescape_first`` is for sources that send
    entity-escaped HTML (Greenhouse), where the tags must be decoded before a
    parser can see them at all.
    """
    if not fragment:
        return None
    raw = html_module.unescape(fragment) if unescape_first else fragment
    try:
        text = LexborHTMLParser(raw).text(separator="\n")
    except Exception:  # noqa: BLE001 - selectolax chokes on some broken markup
        from bs4 import BeautifulSoup  # imported lazily: only needed as a fallback

        text = BeautifulSoup(raw, "html.parser").get_text("\n")
    return normalize_text(text)


def normalize_text(text: str | None) -> str | None:
    """Collapse whitespace while keeping paragraph breaks readable."""
    if not text:
        return None
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [_SPACES.sub(" ", line).strip() for line in text.split("\n")]
    joined = _BLANK_RUN.sub("\n\n", "\n".join(lines)).strip()
    return joined or None


def join_parts(*parts: str | None) -> str | None:
    """Concatenate description sections, dropping the empties."""
    return normalize_text("\n\n".join(p for p in parts if p and p.strip()))


def parse_iso(value: str | None) -> datetime | None:
    """Parse ISO 8601 to an aware UTC datetime. Bad input is not worth raising over."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        log.debug("unparseable timestamp %r", value)
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_epoch_ms(value: int | float | None) -> datetime | None:
    """Lever sends epoch milliseconds, not seconds."""
    if not value:
        return None
    try:
        return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        log.debug("unparseable epoch %r", value)
        return None


def join_locations(*values: str | None) -> str | None:
    """Join distinct location strings, preserving order."""
    seen: list[str] = []
    for v in values:
        v = (v or "").strip()
        if v and v not in seen:
            seen.append(v)
    return " | ".join(seen) or None
