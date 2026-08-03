"""Generic careers-page crawler, and the ATS fingerprinter.

The fallback when a company is not on a supported ATS — and, more valuably, the
way we discover which ATS they *are* on. Handing off to a real adapter is always
better than scraping, so fingerprinting runs first and extraction is the
last resort.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx
from selectolax.lexbor import LexborHTMLParser

from ..config import Target
from ..http import PoliteClient, RobotsDisallowed, SourceUnavailable
from ..models import RawJob
from .base import normalize_text, parse_iso

log = logging.getLogger(__name__)

# Below this ratio of visible text to raw markup, the page is a client-rendered
# shell rather than a list of jobs.
SPA_TEXT_RATIO = 0.05
SPA_MIN_TEXT = 200
# Never crawl a whole site looking for postings.
MAX_DETAIL_LINKS = 50

JOB_LINK = re.compile(r"/(jobs?|careers?|positions?|openings?|vacanc\w*)/", re.IGNORECASE)
JOB_LINK_LOOSE = re.compile(r"job|career|position|opening|vacanc", re.IGNORECASE)


@dataclass(frozen=True)
class Fingerprint:
    """An ATS detected in a careers page's markup."""

    ats: str
    token: str | None
    supported: bool
    marker: str


# host substring -> (ats name, is a supported adapter). Order matters: the more
# specific greenhouse hosts must be tested before the bare "grnhse" marker.
_MARKERS: list[tuple[str, str, bool]] = [
    ("job-boards.greenhouse.io", "greenhouse", True),
    ("boards.greenhouse.io", "greenhouse", True),
    ("grnhse", "greenhouse", True),
    # Regional and bare hosts, tested last so the specific ones report first.
    ("greenhouse.io", "greenhouse", True),
    ("jobs.lever.co", "lever", True),
    ("jobs.ashbyhq.com", "ashby", True),
    ("apply.workable.com", "workable", True),
    ("workable.com", "workable", True),
    ("jobs.smartrecruiters.com", "smartrecruiters", False),
    ("careers.smartrecruiters.com", "smartrecruiters", False),
    ("recruitee.com", "recruitee", False),
    ("jobs.personio.de", "personio", False),
    ("personio.com", "personio", False),
    ("teamtailor.com", "teamtailor", False),
    ("bamboohr.com", "bamboohr", False),
    ("myworkdayjobs.com", "workday", False),
    # Common in India, no adapter yet - these are the Phase 2 backlog.
    ("keka.com", "keka", False),
    ("darwinbox.in", "darwinbox", False),
    ("darwinbox.com", "darwinbox", False),
    ("zohorecruit.com", "zoho_recruit", False),
    ("freshteam.com", "freshteam", False),
]

# Ordered per ATS: the specific board hosts are tried before the looser
# fallbacks, so a real board link always wins over a vendor mention elsewhere
# on the page.
_TOKEN_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "greenhouse": (
        re.compile(r"(?:job-)?boards\.greenhouse\.io/(?:embed/job_board\?for=)?([\w-]+)"),
        # Regional hosts such as eu.greenhouse.io are still Greenhouse boards.
        re.compile(r"(?:[\w-]+\.)?greenhouse\.io/(?:embed/job_board\?for=)?([\w-]+)"),
    ),
    "lever": (re.compile(r"jobs\.lever\.co/([\w-]+)"),),
    "ashby": (re.compile(r"jobs\.ashbyhq\.com/([\w-]+)"),),
    "workable": (
        re.compile(r"apply\.workable\.com/([\w-]+)"),
        re.compile(r"([\w-]+)\.workable\.com"),
    ),
}

# Never a board token: either an ATS vendor's own subdomain or a page on their
# marketing site. Without this, `www.workable.com` in a footer yields the token
# "www", which gets written to companies.yaml and 404s on every future scan.
_NOT_TOKENS = frozenset(
    """www apply jobs job careers career boards board job-boards help support docs blog
    status about pricing customers product products demo resources login signup partners
    company legal privacy terms security en eu us uk in api app cdn assets static""".split()
)


def _extract_token(ats: str, html: str) -> str | None:
    """Pull the board token out of an ATS link, ignoring vendor boilerplate.

    Every occurrence is considered, not just the first: pages routinely mention
    the ATS vendor ("Powered by Workable") before linking to the actual board.
    """
    for pattern in _TOKEN_PATTERNS.get(ats, ()):
        for match in pattern.finditer(html):
            candidate = next((group for group in match.groups() if group), None)
            if candidate and candidate.lower() not in _NOT_TOKENS:
                return candidate
    return None


def fingerprint(html: str) -> Fingerprint | None:
    """Detect which ATS a careers page is fronting, and its board token."""
    lowered = html.lower()
    for marker, ats, supported in _MARKERS:
        if marker not in lowered:
            continue
        token = _extract_token(ats, html)
        return Fingerprint(ats=ats, token=token, supported=supported, marker=marker)
    return None


def looks_like_spa(html: str) -> bool:
    """True if the page is a client-rendered shell with nothing to parse."""
    if not html:
        return True
    text = LexborHTMLParser(html).text(separator=" ") or ""
    visible = re.sub(r"\s+", " ", text).strip()
    if len(visible) < SPA_MIN_TEXT:
        return True
    return (len(visible) / len(html)) < SPA_TEXT_RATIO


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #


def _iter_jsonld(html: str):
    """Yield every JSON-LD object on the page, flattening arrays and @graph."""
    tree = LexborHTMLParser(html)
    for node in tree.css('script[type="application/ld+json"]'):
        raw = node.text(strip=True)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log.debug("skipping malformed JSON-LD block")
            continue
        stack = [data]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                if "@graph" in item:
                    stack.extend(item["@graph"] if isinstance(item["@graph"], list) else [item["@graph"]])
                yield item


def _jsonld_location(posting: dict) -> str | None:
    """Flatten schema.org jobLocation into a readable string."""
    locations = posting.get("jobLocation")
    if isinstance(locations, dict):
        locations = [locations]
    if not isinstance(locations, list):
        return None
    parts: list[str] = []
    for entry in locations:
        if not isinstance(entry, dict):
            continue
        address = entry.get("address")
        if isinstance(address, str):
            parts.append(address)
        elif isinstance(address, dict):
            for key in ("addressLocality", "addressRegion", "addressCountry"):
                value = address.get(key)
                if isinstance(value, dict):
                    value = value.get("name")
                if value and str(value) not in parts:
                    parts.append(str(value))
    return ", ".join(parts) or None


def extract_jsonld(html: str, base_url: str) -> list[RawJob]:
    """schema.org JobPosting is the cleanest signal a careers page can give."""
    jobs: list[RawJob] = []
    for item in _iter_jsonld(html):
        types = item.get("@type")
        types = [types] if isinstance(types, str) else (types or [])
        if not any(str(t).lower() == "jobposting" for t in types):
            continue
        title = item.get("title") or item.get("name")
        if not title:
            continue
        url = item.get("url") or base_url
        identifier = item.get("identifier")
        if isinstance(identifier, dict):
            identifier = identifier.get("value")
        organisation = item.get("hiringOrganization")
        if isinstance(organisation, dict):
            organisation = organisation.get("name")
        jobs.append(
            RawJob(
                source="careers_page",
                # Stable across runs: a URL path, never a positional index.
                external_id=str(identifier) if identifier else urlparse(str(url)).path or str(title),
                title=str(title).strip(),
                location=_jsonld_location(item),
                description=normalize_text(
                    re.sub(r"<[^>]+>", " ", str(item.get("description") or "")) or None
                ),
                url=urljoin(base_url, str(url)),
                posted_at=parse_iso(item.get("datePosted")),
                company_name=str(organisation) if organisation else None,
            )
        )
    return jobs


def harvest_links(html: str, base_url: str) -> list[str]:
    """Collect plausible job-detail URLs, same-origin only, deduplicated."""
    origin = urlparse(base_url).netloc
    seen: dict[str, None] = {}
    for node in LexborHTMLParser(html).css("a[href]"):
        href = (node.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)
        if parsed.netloc != origin or not JOB_LINK.search(parsed.path):
            continue
        # A bare /careers/ index is not a posting.
        if parsed.path.rstrip("/").count("/") < 2:
            continue
        seen.setdefault(absolute.split("#")[0], None)
        if len(seen) >= MAX_DETAIL_LINKS:
            break
    return list(seen)


def extract_repeated(html: str, base_url: str) -> list[RawJob]:
    """Find the container holding the most job-ish sibling links."""
    tree = LexborHTMLParser(html)
    best: list[RawJob] = []
    for container in tree.css("ul, ol, table, div, section"):
        anchors = [
            a
            for a in container.css("a[href]")
            if JOB_LINK_LOOSE.search(
                f"{a.attributes.get('href') or ''} {a.text(strip=True) or ''}"
            )
        ]
        if len(anchors) < 3 or len(anchors) <= len(best):
            continue
        candidate: list[RawJob] = []
        for anchor in anchors:
            href = (anchor.attributes.get("href") or "").strip()
            title = (anchor.text(strip=True) or "").strip()
            if not href or not title or len(title) < 3:
                continue
            absolute = urljoin(base_url, href)
            candidate.append(
                RawJob(
                    source="careers_page",
                    external_id=urlparse(absolute).path,
                    title=title,
                    location=None,
                    url=absolute,
                )
            )
        if len(candidate) > len(best):
            best = candidate
    return best


class CareersPageSource:
    name = "careers_page"

    def matches(self, target: Target) -> bool:
        # Never claims a target by name: the registry falls back to it explicitly.
        return False

    async def fetch(self, client: PoliteClient, target: Target) -> list[RawJob]:
        if not target.careers_url:
            raise SourceUnavailable(f"{target.name}: no careers_url to crawl")
        try:
            html = await client.get(target.careers_url)
        except RobotsDisallowed:
            raise
        except httpx.HTTPError as exc:
            raise SourceUnavailable(f"{target.name}: careers page fetch failed: {exc}") from exc
        return await self.parse(client, target, html, target.careers_url)

    async def parse(
        self, client: PoliteClient, target: Target, html: str, base_url: str
    ) -> list[RawJob]:
        """Extract postings from already-fetched markup, in priority order."""
        if looks_like_spa(html):
            # Playwright is a Phase 2 concern; say so rather than returning [].
            raise SourceUnavailable(
                f"{target.name}: careers page is a client-rendered shell "
                "(install the 'spa' extra once Playwright support lands)"
            )

        if jobs := extract_jsonld(html, base_url):
            log.info("%s: %d jobs from JSON-LD", target.name, len(jobs))
            return jobs

        if jobs := extract_repeated(html, base_url):
            log.info("%s: %d jobs from repeated structure", target.name, len(jobs))
            return jobs

        jobs = await self._crawl_details(client, target, harvest_links(html, base_url))
        if jobs:
            log.info("%s: %d jobs from link harvest", target.name, len(jobs))
            return jobs

        raise SourceUnavailable(f"{target.name}: no postings found on {base_url}")

    async def _crawl_details(
        self, client: PoliteClient, target: Target, links: list[str]
    ) -> list[RawJob]:
        """Last resort: fetch each candidate page and parse it individually."""
        jobs: list[RawJob] = []
        for link in links:
            try:
                page = await client.get(link)
            except (httpx.HTTPError, RobotsDisallowed) as exc:
                log.debug("%s: skipping %s: %s", target.name, link, exc)
                continue
            if found := extract_jsonld(page, link):
                jobs.extend(found)
                continue
            tree = LexborHTMLParser(page)
            heading = tree.css_first("h1")
            title = (heading.text(strip=True) if heading else "") or ""
            if title:
                jobs.append(
                    RawJob(
                        source=self.name,
                        external_id=urlparse(link).path,
                        title=title,
                        url=link,
                        description=normalize_text(tree.body.text(separator="\n")) if tree.body else None,
                    )
                )
        return jobs
