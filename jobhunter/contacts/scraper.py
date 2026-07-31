"""Tier 1: harvest addresses a company has already published.

Free, safe, and the highest-signal tier — a published `careers@` needs no
verification and is the address you actually want. See docs/contact-discovery.md.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import unquote, urljoin, urlparse

import httpx
from selectolax.lexbor import LexborHTMLParser

from ..http import PoliteClient, RobotsDisallowed

log = logging.getLogger(__name__)

# Resolved against the company domain, in this order. EU sites are legally
# required to publish a contact, which is what makes imprint/impressum worth it.
CANDIDATE_PATHS = (
    "/careers", "/career", "/jobs", "/join-us",
    "/about", "/about-us", "/team", "/people",
    "/contact", "/contact-us", "/company",
    "/imprint", "/impressum",
)

EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Requires *both* an at-form and a dot-form. Matching " at " alone would turn
# every occurrence of "look at that" into a candidate address.
OBFUSCATED = re.compile(
    r"([A-Za-z0-9._%+\-]+)\s*(?:\[at\]|\(at\)|\{at\}|\s+at\s+|&#64;)\s*"
    r"([A-Za-z0-9.\-]+)\s*(?:\[dot\]|\(dot\)|\{dot\}|\s+dot\s+)\s*([A-Za-z]{2,})",
    re.IGNORECASE,
)

# Never a hiring contact.
REJECT_LOCAL = frozenset(
    """noreply no-reply donotreply do-not-reply postmaster abuse webmaster admin root
    support help helpdesk sales billing invoices accounts press media pr legal privacy
    security dpo gdpr compliance unsubscribe bounce mailer-daemon newsletter marketing
    notifications notification alerts alert automated system devnull test example
    accommodation accommodations accessibility grievance grievances ethics whistleblower
    investor investors ir partnerships partner vendor vendors procurement finance
    payments refunds disputes fraud care customercare service services
    future careers-future talentcommunity""".split()
)

# Addresses whose local part is a person's name are fine; these are not names,
# and a nearby "we are hiring" is not evidence that they are.
NON_PERSON_HINTS = re.compile(
    r"accommodat|accessib|grievance|ethic|whistle|invest|refund|dispute|fraud|"
    r"unsubscrib|newsletter|survey|feedback|complaint|escalat",
    re.IGNORECASE,
)
# Regex false positives picked up from inline assets.
ASSET_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js", ".ico", ".woff")

RECRUITING_TITLE = re.compile(
    r"recruit|talent|people|hr\b|human resources|hiring|staffing|sourc", re.IGNORECASE
)

# Local part -> (confidence, kind). Highest first; see the ranking table in
# docs/contact-discovery.md.
ROLE_RANKS: list[tuple[frozenset[str], float]] = [
    (frozenset({"careers", "career", "jobs", "job", "recruiting", "recruitment", "recruiter",
                "talent", "hiring"}), 0.95),
    (frozenset({"hr", "people", "peopleops", "people-ops", "personal", "personnel",
                "bewerbung", "hrteam"}), 0.90),
    (frozenset({"apply", "applications", "application", "join", "work", "workwithus"}), 0.85),
    (frozenset({"hello", "contact", "team", "hi", "enquiries", "enquiry", "inquiries"}), 0.55),
    (frozenset({"info", "information", "office", "mail"}), 0.50),
]
PERSON_WITH_RECRUITING_TITLE = 0.80
PERSON_UNKNOWN_TITLE = 0.60
# An opaque single word that is not in the role table and does not look like a
# name. Deliberately below the 0.55 surfacing threshold: it stays in the DB with
# its real confidence rather than being presented as a hiring contact.
UNRECOGNISED_LOCAL = 0.45

# JSON embedded in a page carries escaped punctuation, and `>Press` becomes
# the local part `u003epress` if the escape is not decoded first.
JSON_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")


@dataclass
class Candidate:
    """One discovered address, with the provenance every contact row needs."""

    email: str
    kind: str  # role | person
    confidence: float
    discovery_method: str
    source_url: str
    first_name: str | None = None
    last_name: str | None = None
    role_title: str | None = None

    @property
    def local(self) -> str:
        return self.email.split("@", 1)[0]

    @property
    def domain(self) -> str:
        return self.email.split("@", 1)[-1]


def decode_cfemail(encoded: str) -> str | None:
    """Decode Cloudflare's data-cfemail: a XOR against the first byte.

    Ten lines, and common enough on small-company sites to be worth them.
    """
    try:
        data = bytes.fromhex(encoded.strip())
    except ValueError:
        return None
    if len(data) < 2:
        return None
    key = data[0]
    decoded = "".join(chr(byte ^ key) for byte in data[1:])
    return decoded if EMAIL.fullmatch(decoded) else None


def _domain_matches(email_domain: str, company_domain: str | None) -> bool:
    """Same domain or a subdomain of it. Anything else is a vendor or agency."""
    if not company_domain:
        return True
    email_domain = email_domain.lower().strip(".")
    company_domain = company_domain.lower().strip(".").removeprefix("www.")
    return email_domain == company_domain or email_domain.endswith(f".{company_domain}")


def is_acceptable(email: str, company_domain: str | None) -> bool:
    """Filter out the addresses that are never a hiring contact."""
    email = email.lower()
    if not EMAIL.fullmatch(email):
        return False
    if email.endswith(ASSET_SUFFIXES):
        return False
    local, _, domain = email.partition("@")
    if local in REJECT_LOCAL:
        return False
    # A prefixed variant of a reject ("no-reply-2024@") is still a reject.
    if any(local.startswith(f"{bad}-") or local.startswith(f"{bad}.") for bad in REJECT_LOCAL):
        return False
    # accommodations@, grievance@, investors@ - real inboxes, wrong ones, and a
    # nearby "we're hiring!" is not evidence otherwise.
    if NON_PERSON_HINTS.search(local):
        return False
    return _domain_matches(domain, company_domain)


_ROLE_WORDS: frozenset[str] = frozenset().union(*(locals_ for locals_, _ in ROLE_RANKS)) | REJECT_LOCAL


def looks_like_person(local: str) -> bool:
    """Does this local part plausibly name a human?

    Requires two name-ish components ("anna.schmidt", "a_schmidt") and no
    component that is a known role word. Without the first check, any
    unrecognised word on a page that mentions hiring gets promoted to a 0.80
    named recruiter ("future@"); without the second, regional variants of role
    inboxes do ("info-jp@" splits into two perfectly good "names").
    """
    parts = [p for p in re.split(r"[._\-]", local.lower()) if p]
    if len(parts) < 2 or any(p in _ROLE_WORDS for p in parts):
        return False
    alpha = [p for p in parts if p.isalpha()]
    if len(alpha) < 2 or not any(len(p) >= 3 for p in alpha):
        return False
    # Either two substantial components ("anna.schmidt"), or a leading initial
    # plus a surname ("a_schmidt") — which is the `f.last` pattern, and common.
    return sum(1 for p in alpha if len(p) > 1) >= 2 or len(alpha[0]) == 1


def rank(email: str, context: str = "") -> tuple[float, str, str | None]:
    """Score an address. Returns (confidence, kind, role_title_hint)."""
    local = email.split("@", 1)[0].lower()
    for locals_, confidence in ROLE_RANKS:
        if local in locals_:
            return confidence, "role", None
    if looks_like_person(local):
        if match := RECRUITING_TITLE.search(context):
            return PERSON_WITH_RECRUITING_TITLE, "person", match.group(0)
        return PERSON_UNKNOWN_TITLE, "person", None
    # Regional and team variants of a role inbox ("careers-india@", "jobs.eu@")
    # keep that inbox's ranking: they are the same kind of address.
    head = re.split(r"[._\-]", local, maxsplit=1)[0]
    for locals_, confidence in ROLE_RANKS:
        if head in locals_:
            return confidence, "role", None
    # Neither a known role address nor a name: keep it, but do not surface it.
    return UNRECOGNISED_LOCAL, "role", None


def _surrounding_text(node, *, hops: int = 3) -> str:
    """Text of an enclosing block, for judging what an address is *for*.

    A `mailto:` anchor's own text is usually just "Email Anna", which says nothing
    about whether Anna recruits. The job title sits in a sibling element inside
    the same card, so walk up a few levels to find it — otherwise every named
    person on a team page ranks as title-unknown.
    """
    current = node
    best = node.text(strip=True) or ""
    for _ in range(hops):
        current = getattr(current, "parent", None)
        if current is None:
            break
        text = current.text(separator=" ", strip=True) or ""
        # Stop before swallowing the whole page: a huge blob makes any nearby
        # "we're hiring" look like this person's job title.
        if len(text) > 600:
            break
        best = text
    return best


def _split_person_name(local: str) -> tuple[str | None, str | None]:
    """Best-effort first/last from a local part like `anna.schmidt`."""
    parts = [p for p in re.split(r"[._\-]", local) if p and not p.isdigit()]
    if len(parts) >= 2 and all(len(p) > 1 for p in parts[:2]):
        return parts[0].capitalize(), parts[-1].capitalize()
    return None, None


def harvest(text: str, source_url: str, company_domain: str | None, *, is_html: bool) -> list[Candidate]:
    """Pull every plausible address out of one page or job description."""
    found: dict[str, Candidate] = {}

    def add(email: str, context: str, method: str) -> None:
        email = email.strip().lower().rstrip(".,;:)")
        if not is_acceptable(email, company_domain):
            return
        confidence, kind, title = rank(email, context)
        first, last = _split_person_name(email.split("@", 1)[0]) if kind == "person" else (None, None)
        existing = found.get(email)
        if existing is None or confidence > existing.confidence:
            found[email] = Candidate(
                email=email,
                kind=kind,
                confidence=confidence,
                discovery_method=method,
                source_url=source_url,
                first_name=first,
                last_name=last,
                role_title=title,
            )

    plain = JSON_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), text)
    if is_html:
        tree = LexborHTMLParser(text)
        # Script and style bodies are inlined JSON and CSS, not prose. Left in,
        # they contribute analytics keys and escaped punctuation that the address
        # regex happily mistakes for local parts.
        for node in tree.css("script, style, noscript, template"):
            node.decompose()

        # 1. mailto: hrefs are the highest-signal form.
        for node in tree.css('a[href^="mailto:"], a[href^="MAILTO:"]'):
            href = node.attributes.get("href") or ""
            address = unquote(href.split(":", 1)[-1]).split("?")[0]
            for candidate in EMAIL.findall(address):
                add(candidate, _surrounding_text(node), "scraped:mailto")

        # 2. Cloudflare-obfuscated addresses.
        for node in tree.css("[data-cfemail]"):
            if decoded := decode_cfemail(node.attributes.get("data-cfemail") or ""):
                add(decoded, node.parent.text(strip=True) if node.parent else "", "scraped:cfemail")

        plain = JSON_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), tree.text(separator="\n") or "")

    # 3. Plain text.
    for match in EMAIL.finditer(plain):
        start, end = match.span()
        add(match.group(0), plain[max(0, start - 200) : end + 200], "scraped:text")

    # 4. Obfuscated in prose.
    for match in OBFUSCATED.finditer(plain):
        local, domain, tld = match.groups()
        add(f"{local}@{domain}.{tld}", plain[max(0, match.start() - 200) : match.end() + 200],
            "scraped:obfuscated")

    return list(found.values())


async def scrape_company(
    client: PoliteClient,
    *,
    domain: str | None,
    extra_pages: list[str] | None = None,
    job_texts: list[tuple[str, str]] | None = None,
) -> list[Candidate]:
    """Tier 1 over a company's own pages plus the job descriptions in hand.

    ``extra_pages`` (target.contact_pages) is tried first and trusted most: it is
    the user's manual override.
    """
    candidates: list[Candidate] = []

    # Postings frequently end with "questions? email jobs@..." - free, and
    # exactly the right address.
    for text, source_url in job_texts or []:
        candidates.extend(harvest(text, source_url, domain, is_html=False))

    urls: list[str] = list(extra_pages or [])
    if domain:
        base = domain if domain.startswith("http") else f"https://{domain}"
        urls.extend(urljoin(base, path) for path in CANDIDATE_PATHS)

    for url in urls:
        try:
            html = await client.get(url)
        except RobotsDisallowed:
            log.info("robots.txt disallows %s, skipping", url)
            continue
        except httpx.HTTPError as exc:
            log.debug("contact page %s unavailable: %s", url, exc)
            continue
        page_domain = domain or urlparse(url).netloc
        candidates.extend(harvest(html, url, page_domain, is_html=True))

    # Highest confidence per address wins.
    best: dict[str, Candidate] = {}
    for candidate in candidates:
        if candidate.email not in best or candidate.confidence > best[candidate.email].confidence:
            best[candidate.email] = candidate
    return sorted(best.values(), key=lambda c: c.confidence, reverse=True)
