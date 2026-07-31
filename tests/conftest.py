"""Shared fixtures, and the guarantee that the suite cannot touch the network.

Mocking each call site is easy to forget; making the network structurally
unreachable is not. If a test ever needs real I/O, that is a design problem with
the test, not a reason to relax this.
"""
from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


class NetworkBlocked(RuntimeError):
    """A test tried to reach the network, DNS, or an SMTP server."""


def _blocked(*args, **kwargs):
    raise NetworkBlocked(
        "A test attempted real network/DNS/SMTP I/O. Mock HTTP with respx, and "
        "patch the module-level dns/smtplib indirections in jobhunter.contacts.verify."
    )


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Block outbound sockets, DNS resolution and SMTP for every test."""
    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)

    import smtplib

    monkeypatch.setattr(smtplib.SMTP, "__init__", _blocked)

    try:
        import dns.resolver
    except ImportError:  # pragma: no cover - dnspython is a hard dependency
        pass
    else:
        monkeypatch.setattr(dns.resolver, "resolve", _blocked, raising=False)


@pytest.fixture
def fixture_text():
    """Raw text of a saved live response."""

    def load(name: str) -> str:
        return (FIXTURES / name).read_text()

    return load


@pytest.fixture
def fixture_json():
    def load(name: str):
        return json.loads((FIXTURES / name).read_text())

    return load


@pytest.fixture
def polite_client(tmp_path):
    """A PoliteClient wired for tests: no cache, no waiting, no backoff.

    Rate limiting and backoff are exercised deliberately in test_http.py; every
    other test would only be made slow by them.
    """
    from jobhunter.http import PoliteClient

    def make(**overrides):
        options = {
            "cache_dir": tmp_path / "cache",
            "cache_ttl": 0,
            "requests_per_second": 10_000.0,
            "max_retries": 0,
            "retry_backoff_base": 1.0,
        }
        options.update(overrides)
        return PoliteClient(**options)

    return make


@pytest.fixture
def allow_robots(respx_mock):
    """Register a permissive robots.txt for a host.

    A 404 is the realistic case for an API host and means unrestricted, so this
    exercises the real code path rather than bypassing the robots check.
    """

    def register(*origins: str):
        for origin in origins:
            respx_mock.get(f"{origin}/robots.txt").respond(404)

    return register
