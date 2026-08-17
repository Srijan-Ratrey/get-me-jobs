"""A deliberately well-behaved async HTTP client.

Per-host rate limiting, robots.txt compliance, on-disk caching, and retry with
backoff. Everything that touches the network goes through here.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from .config import settings

log = logging.getLogger(__name__)

# Statuses worth trying again. Everything else in 4xx is a permanent answer.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

# However long a server asks us to wait, cap it: a Retry-After of a day is not
# something a foreground scan should honour literally.
MAX_RETRY_AFTER_SECONDS = 60.0

# Hosts where a 401/403 on robots.txt is an API gateway's default answer to an
# unrouted path, not a crawl directive.
#
# `api.ashbyhq.com` returns 401 "Unauthorized" for /robots.txt because it 401s
# everything it does not route, while /posting-api/job-board/ is a documented
# public syndication endpoint that docs/compliance.md reviewed and cleared. Left
# unhandled, the disallow-all rule below silently stops this project reading any
# Ashby board — ten of the tracked companies, including the one holding the two
# best-scoring roles.
#
# Deliberately a module constant and not a setting: exempting a host from robots
# handling should require a code change somebody reviews, never an env var. Add
# to it only alongside a written justification in docs/compliance.md, and only
# for a host whose public API terms have actually been read.
ROBOTS_EXEMPT_HOSTS = frozenset({"api.ashbyhq.com"})


class RobotsDisallowed(Exception):
    """Raised when robots.txt forbids the URL. Not an error to retry."""


class SourceUnavailable(Exception):
    """A source could not be fetched or parsed.

    The caller logs this into ``runs.errors`` and moves on to the next target:
    one adapter failing must never abort a whole run.
    """


@dataclass
class _HostBucket:
    """One token bucket per host so a slow site cannot be hammered.

    The lock is held across the sleep, which is what serialises same-host
    requests. Callers must acquire this *before* the global concurrency
    semaphore, or a host waiting out its rate limit would occupy a slot that a
    different, idle host could be using.
    """

    min_interval: float
    last_request: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def acquire(self) -> None:
        async with self.lock:
            wait = self.min_interval - (time.monotonic() - self.last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            self.last_request = time.monotonic()


class PoliteClient:
    """Async context manager wrapping httpx with politeness guarantees."""

    def __init__(
        self,
        *,
        requests_per_second: float | None = None,
        respect_robots: bool | None = None,
        cache_dir: Path | None = None,
        cache_ttl: int | None = None,
        max_retries: int | None = None,
        retry_backoff_base: float | None = None,
    ):
        rps = requests_per_second or settings.requests_per_second
        self._min_interval = 1.0 / rps if rps > 0 else 0.0
        self._respect_robots = (
            settings.respect_robots if respect_robots is None else respect_robots
        )
        self._cache_dir = cache_dir or settings.cache_dir
        self._cache_ttl = settings.cache_ttl_seconds if cache_ttl is None else cache_ttl
        self._max_retries = settings.max_retries if max_retries is None else max_retries
        self._backoff_base = (
            settings.retry_backoff_base if retry_backoff_base is None else retry_backoff_base
        )
        self._user_agent = settings.user_agent
        self._buckets: dict[str, _HostBucket] = {}
        self._robots: dict[str, RobotFileParser | None] = {}
        self._robots_locks: dict[str, asyncio.Lock] = {}
        self._sem = asyncio.Semaphore(settings.max_concurrency)
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> PoliteClient:
        self._client = httpx.AsyncClient(
            timeout=settings.request_timeout,
            follow_redirects=True,
            headers={
                "User-Agent": self._user_agent,
                "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client:
            await self._client.aclose()

    # ----------------------------- caching ------------------------------- #

    def _cache_path(self, url: str) -> Path:
        return self._cache_dir / f"{hashlib.sha256(url.encode()).hexdigest()}.json"

    def _cache_read(self, url: str) -> str | None:
        if self._cache_ttl <= 0:
            return None
        path = self._cache_path(url)
        if not path.exists() or time.time() - path.stat().st_mtime > self._cache_ttl:
            return None
        try:
            return json.loads(path.read_text())["body"]
        except (json.JSONDecodeError, KeyError, OSError):
            return None

    def _cache_write(self, url: str, body: str) -> None:
        if self._cache_ttl <= 0:
            return
        try:
            self._cache_path(url).write_text(json.dumps({"url": url, "body": body}))
        except OSError as exc:
            log.debug("cache write failed for %s: %s", url, exc)

    # ---------------------------- rate limit ----------------------------- #

    async def _send(self, url: str, **kwargs) -> httpx.Response:
        """Issue one request, rate-limited per host and globally capped.

        Order matters: the per-host wait happens outside the semaphore so that a
        host sleeping off its rate limit does not consume one of the concurrency
        slots the other hosts are sharing.
        """
        assert self._client is not None
        host = urlparse(url).netloc
        bucket = self._buckets.setdefault(host, _HostBucket(self._min_interval))
        await bucket.acquire()
        async with self._sem:
            return await self._client.get(url, **kwargs)

    # ----------------------------- robots -------------------------------- #

    async def _robots_for(self, origin: str) -> RobotFileParser | None:
        """Fetch and parse an origin's robots.txt. None means no restrictions.

        Note that stdlib ``RobotFileParser`` resolves conflicts by **first match**,
        while the de-facto standard most crawlers follow is longest match. So a
        file reading ``Disallow: /`` then ``Allow: /public`` is treated here as
        blanket-disallowed, where Google would read ``/public`` as permitted.
        That is stricter than the site owner intended, and deliberately left
        alone: erring toward not fetching is the right direction for this project,
        and a hand-rolled matcher is a poor thing to be wrong about.
        """
        try:
            # Through _send: robots.txt is a network request like any other and
            # is subject to the same per-host rate limit.
            resp = await self._send(f"{origin}/robots.txt", timeout=10.0)
        except httpx.HTTPError:
            return None
        if resp.status_code in (401, 403):
            # An access-controlled robots.txt means the whole origin is off
            # limits, not that it is unrestricted. This is what stdlib
            # RobotFileParser.read() does and what Google's spec says, and
            # returning None here instead would have this client quietly grant
            # itself access to precisely the hosts that refused to state terms.
            if urlparse(origin).netloc in ROBOTS_EXEMPT_HOSTS:
                log.debug("%s: gateway-auth'd robots.txt, vetted public API", origin)
                return None
            parser = RobotFileParser()
            parser.disallow_all = True
            return parser
        if resp.status_code != 200:
            # A missing (404) or erroring robots.txt means unrestricted.
            return None
        parser = RobotFileParser()
        parser.parse(resp.text.splitlines())
        return parser

    async def _allowed(self, url: str) -> bool:
        if not self._respect_robots:
            return True
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        # Per-origin lock so concurrent first-hits on one host fetch robots once.
        lock = self._robots_locks.setdefault(origin, asyncio.Lock())
        async with lock:
            if origin not in self._robots:
                self._robots[origin] = await self._robots_for(origin)
        parser = self._robots[origin]
        if parser is None:
            return True
        return parser.can_fetch(self._user_agent, url)

    # ------------------------------ retry -------------------------------- #

    @staticmethod
    def _retry_after_seconds(resp: httpx.Response) -> float | None:
        """Parse Retry-After in either form: delta-seconds or an HTTP-date."""
        raw = (resp.headers.get("Retry-After") or "").strip()
        if not raw:
            return None
        if raw.isdigit():
            return min(float(raw), MAX_RETRY_AFTER_SECONDS)
        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        delta = (when - datetime.now(timezone.utc)).total_seconds()
        return min(max(delta, 0.0), MAX_RETRY_AFTER_SECONDS)

    # ------------------------------ fetch -------------------------------- #

    async def get(self, url: str, *, use_cache: bool = True, **kwargs) -> str:
        """GET a URL as text. Raises RobotsDisallowed or httpx.HTTPError."""
        if use_cache and (cached := self._cache_read(url)) is not None:
            log.debug("cache hit %s", url)
            return cached

        if not await self._allowed(url):
            raise RobotsDisallowed(url)

        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                resp = await self._send(url, **kwargs)
            except httpx.HTTPError as exc:
                last_exc = exc
            else:
                if resp.status_code == 200:
                    self._cache_write(url, resp.text)
                    return resp.text
                if resp.status_code in RETRYABLE_STATUS:
                    last_exc = httpx.HTTPStatusError(
                        f"{resp.status_code} for {url}", request=resp.request, response=resp
                    )
                    # Honour Retry-After when the server bothers to send one, in
                    # preference to our own backoff schedule.
                    if (delay := self._retry_after_seconds(resp)) is not None:
                        if attempt < self._max_retries:
                            await asyncio.sleep(delay)
                        continue
                else:
                    # 4xx other than 429: retrying will not help.
                    resp.raise_for_status()
                    return resp.text

            if attempt < self._max_retries:
                await asyncio.sleep(self._backoff_base**attempt)

        assert last_exc is not None
        raise last_exc

    async def get_json(self, url: str, **kwargs):
        """GET a URL and parse it as JSON. Raises SourceUnavailable on garbage."""
        body = await self.get(url, **kwargs)
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise SourceUnavailable(f"{url} did not return JSON: {exc}") from exc
