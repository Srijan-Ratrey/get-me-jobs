"""PoliteClient: rate limiting, robots.txt, retry, caching.

These are the guarantees that keep the project's IP unblocked and its legal
footing intact, so they get tested directly rather than trusted.
"""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest
import respx

from jobhunter import http as http_module
from jobhunter.http import PoliteClient, RobotsDisallowed, SourceUnavailable

ORIGIN = "https://api.example.com"


@pytest.fixture
def captured_sleeps(monkeypatch):
    """Replace asyncio.sleep inside http.py so backoff is asserted, not waited on."""
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(http_module.asyncio, "sleep", fake_sleep)
    return slept


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #


@respx.mock
async def test_rate_limit_delays_second_same_host_request(tmp_path, allow_robots):
    allow_robots(ORIGIN)
    respx.get(f"{ORIGIN}/a").respond(200, text="a")
    respx.get(f"{ORIGIN}/b").respond(200, text="b")

    interval = 0.25
    async with PoliteClient(
        requests_per_second=1 / interval, cache_dir=tmp_path, cache_ttl=0
    ) as client:
        start = time.monotonic()
        await client.get(f"{ORIGIN}/a")
        await client.get(f"{ORIGIN}/b")
        elapsed = time.monotonic() - start

    assert elapsed >= interval, f"second same-host request was not delayed ({elapsed:.3f}s)"


@respx.mock
async def test_one_slow_host_does_not_stall_another(tmp_path, allow_robots):
    """The per-host wait must happen outside the global concurrency semaphore.

    If it is inside, a host sleeping off its rate limit occupies a slot that an
    idle host could use, and unrelated targets serialise behind it.
    """
    allow_robots("https://slow.example.com", "https://fast.example.com")
    for host in ("slow", "fast"):
        for path in ("1", "2"):
            respx.get(f"https://{host}.example.com/{path}").respond(200, text="ok")

    interval = 0.3
    async with PoliteClient(
        requests_per_second=1 / interval, cache_dir=tmp_path, cache_ttl=0
    ) as client:
        start = time.monotonic()
        await asyncio.gather(
            client.get("https://slow.example.com/1"),
            client.get("https://slow.example.com/2"),
            client.get("https://fast.example.com/1"),
            client.get("https://fast.example.com/2"),
        )
        elapsed = time.monotonic() - start

    # Each host serialises its own two requests (~1 interval). Run in parallel
    # that is ~1 interval total; serialised across hosts it would be ~3.
    assert elapsed < interval * 2.5, f"hosts did not proceed in parallel ({elapsed:.3f}s)"


# --------------------------------------------------------------------------- #
# robots.txt
# --------------------------------------------------------------------------- #


@respx.mock
async def test_robots_disallow_blocks_fetch(tmp_path):
    respx.get(f"{ORIGIN}/robots.txt").respond(200, text="User-agent: *\nDisallow: /")
    route = respx.get(f"{ORIGIN}/secret").respond(200, text="should not be fetched")

    async with PoliteClient(cache_dir=tmp_path, cache_ttl=0, requests_per_second=1000) as client:
        with pytest.raises(RobotsDisallowed):
            await client.get(f"{ORIGIN}/secret")

    assert route.call_count == 0, "a disallowed URL must never be requested"


@respx.mock
async def test_missing_robots_means_unrestricted(tmp_path):
    respx.get(f"{ORIGIN}/robots.txt").respond(404)
    respx.get(f"{ORIGIN}/data").respond(200, text="ok")
    async with PoliteClient(cache_dir=tmp_path, cache_ttl=0, requests_per_second=1000) as client:
        assert await client.get(f"{ORIGIN}/data") == "ok"


@respx.mock
async def test_robots_is_fetched_once_per_origin(tmp_path):
    """Concurrent first-hits on one host must not each fetch robots.txt."""
    robots = respx.get(f"{ORIGIN}/robots.txt").respond(404)
    respx.get(f"{ORIGIN}/a").respond(200, text="a")
    respx.get(f"{ORIGIN}/b").respond(200, text="b")

    async with PoliteClient(cache_dir=tmp_path, cache_ttl=0, requests_per_second=1000) as client:
        await asyncio.gather(client.get(f"{ORIGIN}/a"), client.get(f"{ORIGIN}/b"))

    assert robots.call_count == 1


@respx.mock
async def test_robots_allow_before_disallow_is_honoured(tmp_path):
    respx.get(f"{ORIGIN}/robots.txt").respond(
        200, text="User-agent: *\nAllow: /public\nDisallow: /"
    )
    respx.get(f"{ORIGIN}/public/jobs").respond(200, text="jobs")
    async with PoliteClient(cache_dir=tmp_path, cache_ttl=0, requests_per_second=1000) as client:
        assert await client.get(f"{ORIGIN}/public/jobs") == "jobs"
        with pytest.raises(RobotsDisallowed):
            await client.get(f"{ORIGIN}/private/jobs")


@respx.mock
async def test_disallow_before_allow_is_read_strictly(tmp_path):
    """Pins a real quirk: stdlib robotparser is first-match, not longest-match.

    Google and most crawlers would read `Allow: /public` as carving an exception
    out of `Disallow: /`. Python reads the Disallow first and stops. That makes us
    stricter than the site owner intended, which is the safe direction to be
    wrong in — but it is surprising enough to be worth a test, because it means
    some genuinely-permitted paths will be skipped.
    """
    respx.get(f"{ORIGIN}/robots.txt").respond(
        200, text="User-agent: *\nDisallow: /\nAllow: /public"
    )
    route = respx.get(f"{ORIGIN}/public/jobs").respond(200, text="jobs")
    async with PoliteClient(cache_dir=tmp_path, cache_ttl=0, requests_per_second=1000) as client:
        with pytest.raises(RobotsDisallowed):
            await client.get(f"{ORIGIN}/public/jobs")
    assert route.call_count == 0


# --------------------------------------------------------------------------- #
# Retry
# --------------------------------------------------------------------------- #


@respx.mock
async def test_429_with_retry_after_is_retried(tmp_path, allow_robots, captured_sleeps):
    allow_robots(ORIGIN)
    route = respx.get(f"{ORIGIN}/limited")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "1"}),
        httpx.Response(200, text="finally"),
    ]

    async with PoliteClient(
        cache_dir=tmp_path, cache_ttl=0, requests_per_second=1000, max_retries=3
    ) as client:
        assert await client.get(f"{ORIGIN}/limited") == "finally"

    assert route.call_count == 2
    # Retry-After was honoured in preference to our own backoff schedule.
    assert 1.0 in captured_sleeps


@respx.mock
async def test_retry_after_http_date_is_understood(tmp_path, allow_robots, captured_sleeps):
    """Retry-After has a date form as well as delta-seconds."""
    allow_robots(ORIGIN)
    route = respx.get(f"{ORIGIN}/limited")
    route.side_effect = [
        httpx.Response(503, headers={"Retry-After": "Wed, 21 Oct 2099 07:28:00 GMT"}),
        httpx.Response(200, text="ok"),
    ]
    async with PoliteClient(
        cache_dir=tmp_path, cache_ttl=0, requests_per_second=1000, max_retries=2
    ) as client:
        assert await client.get(f"{ORIGIN}/limited") == "ok"

    assert route.call_count == 2
    # Capped rather than honoured literally: a far-future date must not hang a scan.
    assert captured_sleeps and max(captured_sleeps) <= http_module.MAX_RETRY_AFTER_SECONDS


@respx.mock
async def test_404_raises_immediately_without_retrying(tmp_path, allow_robots, captured_sleeps):
    allow_robots(ORIGIN)
    route = respx.get(f"{ORIGIN}/missing").respond(404)

    async with PoliteClient(
        cache_dir=tmp_path, cache_ttl=0, requests_per_second=1000, max_retries=3
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await client.get(f"{ORIGIN}/missing")

    assert route.call_count == 1, "a 404 is a permanent answer; retrying wastes the budget"
    # Sub-millisecond entries are the rate limiter, which shares asyncio.sleep.
    # What must be absent is a backoff-scale pause.
    assert all(s < 0.5 for s in captured_sleeps), f"unexpected backoff: {captured_sleeps}"


@respx.mock
async def test_retries_are_bounded(tmp_path, allow_robots, captured_sleeps):
    allow_robots(ORIGIN)
    route = respx.get(f"{ORIGIN}/broken").respond(500)

    async with PoliteClient(
        cache_dir=tmp_path, cache_ttl=0, requests_per_second=1000, max_retries=2
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await client.get(f"{ORIGIN}/broken")

    assert route.call_count == 3  # the initial attempt plus max_retries


# --------------------------------------------------------------------------- #
# Caching and JSON
# --------------------------------------------------------------------------- #


@respx.mock
async def test_cache_avoids_a_second_request(tmp_path, allow_robots):
    allow_robots(ORIGIN)
    route = respx.get(f"{ORIGIN}/cached").respond(200, text="body")

    async with PoliteClient(cache_dir=tmp_path, cache_ttl=600, requests_per_second=1000) as client:
        assert await client.get(f"{ORIGIN}/cached") == "body"
        assert await client.get(f"{ORIGIN}/cached") == "body"

    assert route.call_count == 1


@respx.mock
async def test_get_json_wraps_garbage_in_source_unavailable(tmp_path, allow_robots):
    allow_robots(ORIGIN)
    respx.get(f"{ORIGIN}/not-json").respond(200, text="<html>nope</html>")
    async with PoliteClient(cache_dir=tmp_path, cache_ttl=0, requests_per_second=1000) as client:
        with pytest.raises(SourceUnavailable, match="did not return JSON"):
            await client.get_json(f"{ORIGIN}/not-json")
