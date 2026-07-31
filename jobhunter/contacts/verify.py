"""Tier 3: MX lookup, catch-all detection, RCPT probe.

Opt-in (``settings.verify_emails``, default False). The pipeline must be fully
useful without it, because aggressive probing gets your IP blocklisted and
Tier-1 role addresses need no verification anyway.

**This module never issues DATA.** The probe is structurally incapable of
delivering a message, which is the property that keeps it defensible.
"""
from __future__ import annotations

import logging
import random
import smtplib
import string
import time
from dataclasses import dataclass, field

import dns.resolver

from ..config import settings

log = logging.getLogger(__name__)

VALID = "valid"
INVALID = "invalid"
RISKY = "risky"
UNKNOWN = "unknown"

# Providers that deliberately do not leak recipient validity. An `unknown` from
# these means "no information", never a negative signal.
OPAQUE_MX_SUFFIXES = (
    "google.com", "googlemail.com", "outlook.com", "protection.outlook.com",
    "hotmail.com", "office365.com", "protonmail.ch", "proton.me", "pphosted.com",
    "mimecast.com", "barracudanetworks.com",
)

ACCEPT_CODES = frozenset({250, 251})
REJECT_CODES = frozenset({550, 551, 553})


@dataclass
class VerifyResult:
    status: str
    detail: str
    catch_all: bool | None = None
    mx_hosts: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Network indirections. Kept as module-level functions so tests can patch them
# without needing a real resolver or mail server.
# --------------------------------------------------------------------------- #


def resolve_mx(domain: str) -> list[str]:
    """MX hosts for a domain, best-priority first. Falls back to an A record."""
    try:
        answers = dns.resolver.resolve(domain, "MX")
    except Exception as exc:  # noqa: BLE001 - dnspython raises a wide family
        log.debug("MX lookup failed for %s: %s", domain, exc)
    else:
        records = sorted(
            ((int(r.preference), str(r.exchange).rstrip(".")) for r in answers),
            key=lambda pair: pair[0],
        )
        if records:
            return [host for _, host in records]
    # No MX but an A record still accepts mail per RFC 5321 §5.1.
    try:
        dns.resolver.resolve(domain, "A")
    except Exception:  # noqa: BLE001
        return []
    return [domain]


def probe_recipients(mx_host: str, recipients: list[str]) -> dict[str, int]:
    """EHLO -> MAIL FROM -> RCPT TO (xN) -> RSET -> QUIT. Never DATA.

    One connection per host, recipients probed serially on it, which is both
    politer and less likely to be read as a dictionary attack.
    """
    codes: dict[str, int] = {}
    server = smtplib.SMTP(timeout=settings.smtp_timeout)
    try:
        server.connect(mx_host, 25)
        server.ehlo_or_helo_if_needed()
        server.docmd("MAIL", f"FROM:<{settings.smtp_mail_from}>")
        for recipient in recipients:
            code, _ = server.docmd("RCPT", f"TO:<{recipient}>")
            codes[recipient] = code
        server.rset()
    finally:
        try:
            server.quit()
        except (smtplib.SMTPException, OSError):
            server.close()
    return codes


def _random_local(length: int = 12) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(length))


def is_opaque_mx(mx_hosts: list[str]) -> bool:
    """Google Workspace and Microsoft 365 will not tell you. Do not press them."""
    return any(host.lower().rstrip(".").endswith(OPAQUE_MX_SUFFIXES) for host in mx_hosts)


# --------------------------------------------------------------------------- #
# The state machine
# --------------------------------------------------------------------------- #


def verify_domain(domain: str, *, known_catch_all: bool | None = None) -> VerifyResult:
    """MX lookup, then the catch-all probe. Cache the result on the company."""
    mx_hosts = resolve_mx(domain)
    if not mx_hosts:
        return VerifyResult(INVALID, f"{domain} has no MX or A record", mx_hosts=[])

    if is_opaque_mx(mx_hosts):
        return VerifyResult(
            UNKNOWN, f"{mx_hosts[0]} does not disclose recipient validity", mx_hosts=mx_hosts
        )

    if known_catch_all is True:
        return VerifyResult(RISKY, f"{domain} is a known catch-all", True, mx_hosts)
    if known_catch_all is False:
        return VerifyResult(UNKNOWN, "catch-all already ruled out", False, mx_hosts)

    probe = f"{_random_local()}@{domain}"
    try:
        codes = probe_recipients(mx_hosts[0], [probe])
    except Exception as exc:  # noqa: BLE001 - smtplib raises a wide family
        return VerifyResult(UNKNOWN, f"catch-all probe failed: {exc}", None, mx_hosts)

    code = codes.get(probe, 0)
    if code in ACCEPT_CODES:
        # The domain accepts everything, so verification cannot mean anything.
        return VerifyResult(RISKY, f"{domain} accepts all recipients (catch-all)", True, mx_hosts)
    return VerifyResult(UNKNOWN, "not a catch-all; candidates can be probed", False, mx_hosts)


def verify_email(email: str, *, mx_hosts: list[str], delay: float | None = None) -> VerifyResult:
    """RCPT-probe one address. Assumes the domain is not a catch-all."""
    if delay is None:
        delay = settings.smtp_delay_seconds
    if delay:
        # Serial and spaced, per MX host. Never concurrent.
        time.sleep(delay)
    try:
        codes = probe_recipients(mx_hosts[0], [email])
    except Exception as exc:  # noqa: BLE001
        return VerifyResult(UNKNOWN, f"probe failed: {exc}", mx_hosts=mx_hosts)

    code = codes.get(email, 0)
    if code in ACCEPT_CODES:
        return VerifyResult(VALID, f"RCPT accepted ({code})", mx_hosts=mx_hosts)
    if code in REJECT_CODES:
        return VerifyResult(INVALID, f"RCPT rejected ({code})", mx_hosts=mx_hosts)
    # 450/451/452 is greylisting, and anything else is not a negative signal.
    return VerifyResult(UNKNOWN, f"inconclusive ({code})", mx_hosts=mx_hosts)


def verify_candidates(
    domain: str,
    emails: list[str],
    *,
    known_catch_all: bool | None = None,
    max_probes: int | None = None,
) -> tuple[dict[str, VerifyResult], bool | None]:
    """Verify up to ``max_probes`` candidates. Returns (results, catch_all).

    Stops at the first ``valid``: once you have a real address, further probing is
    pure downside.
    """
    domain_result = verify_domain(domain, known_catch_all=known_catch_all)
    results: dict[str, VerifyResult] = {}

    if domain_result.status == INVALID:
        return {email: domain_result for email in emails}, domain_result.catch_all
    if domain_result.status == RISKY:
        # Catch-all: every candidate is risky, and never valid.
        return {
            email: VerifyResult(RISKY, domain_result.detail, True, domain_result.mx_hosts)
            for email in emails
        }, True
    if domain_result.status == UNKNOWN and is_opaque_mx(domain_result.mx_hosts):
        return {email: domain_result for email in emails}, domain_result.catch_all

    limit = max_probes if max_probes is not None else settings.max_guesses_per_domain
    for email in emails[:limit]:
        result = verify_email(email, mx_hosts=domain_result.mx_hosts)
        results[email] = result
        if result.status == VALID:
            break
    return results, domain_result.catch_all
