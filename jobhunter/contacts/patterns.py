"""Tier 2: turn a known person's name into candidate addresses.

The highest-leverage step in the module is *not* the pattern table — it is
inferring the house pattern from an address you already trust. That turns twelve
guesses at 0.14 into one guess at 0.85, and makes the second contact you look up
at a company nearly free. See docs/contact-discovery.md.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass

from ..config import settings

log = logging.getLogger(__name__)

# Ordered by real-world prevalence. The prior is a probability, not a confidence.
PATTERNS: list[tuple[str, float]] = [
    ("first.last", 0.35),
    ("first", 0.15),
    ("flast", 0.14),
    ("firstlast", 0.10),
    ("first_last", 0.07),
    ("firstl", 0.05),
    ("f.last", 0.05),
    ("last.first", 0.03),
    ("last", 0.03),
    ("first-last", 0.02),
    ("lastf", 0.01),
]

# An address inferred from a known-good one at the same domain.
INFERRED_CONFIDENCE = 0.85
# Unverified guesses stay below the 0.55 surfacing threshold on purpose: they are
# not actionable until Tier 3 promotes them.
GUESS_SCALE = 0.4
GUESS_CAP = 0.5

HONORIFICS = frozenset({"mr", "mrs", "ms", "miss", "dr", "prof", "professor", "sir", "madam"})
SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv", "v", "phd", "md", "mba", "cfa"})


@dataclass
class NameParts:
    first: str
    last: str

    def __bool__(self) -> bool:
        return bool(self.first and self.last)


def strip_accents(value: str) -> str:
    """ä -> a, ø -> o, ç -> c. Mail systems overwhelmingly use ASCII local parts."""
    # Decompose, drop combining marks, then handle the letters that have no
    # decomposition (ø, ß, æ) explicitly.
    special = {"ø": "o", "ß": "ss", "æ": "ae", "œ": "oe", "đ": "d", "ð": "d", "þ": "th", "ł": "l"}
    value = "".join(special.get(ch, ch) for ch in value.lower())
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_name(full_name: str) -> NameParts | None:
    """Reduce a display name to ASCII first/last suitable for a local part.

    Drops honorifics and suffixes, keeps only the first given name when several
    are present, and collapses multi-part surnames ("van der Berg" -> vanderberg)
    the way corporate mail systems do.
    """
    if not full_name or not full_name.strip():
        return None
    cleaned = strip_accents(full_name)
    cleaned = re.sub(r"[^a-z\s'\-,.]", " ", cleaned)
    # "Schmidt, Anna" -> "Anna Schmidt"
    if "," in cleaned:
        surname, _, given = cleaned.partition(",")
        cleaned = f"{given} {surname}"
    tokens = [t.strip(" .'-") for t in cleaned.split()]
    tokens = [t for t in tokens if t and t not in HONORIFICS and t not in SUFFIXES]
    if len(tokens) < 2:
        return None
    first = re.sub(r"[^a-z]", "", tokens[0])
    # Everything after the first given name is the surname; apostrophes and
    # spaces disappear (O'Brien -> obrien).
    last = re.sub(r"[^a-z]", "", "".join(tokens[1:]))
    if not first or not last:
        return None
    return NameParts(first=first, last=last)


def render(pattern: str, name: NameParts) -> str | None:
    """Substitute a name into one pattern."""
    first, last = name.first, name.last
    table = {
        "first.last": f"{first}.{last}",
        "first": first,
        "flast": f"{first[0]}{last}",
        "firstlast": f"{first}{last}",
        "first_last": f"{first}_{last}",
        "firstl": f"{first}{last[0]}",
        "f.last": f"{first[0]}.{last}",
        "last.first": f"{last}.{first}",
        "last": last,
        "first-last": f"{first}-{last}",
        "lastf": f"{last}{first[0]}",
    }
    return table.get(pattern)


def infer_pattern(known_email: str, name: NameParts) -> str | None:
    """Reverse a known-good address for this person into a pattern name.

    Only meaningful when the address genuinely belongs to the given name — the
    caller is responsible for that pairing.
    """
    local = known_email.split("@", 1)[0].lower()
    for pattern, _ in PATTERNS:
        if render(pattern, name) == local:
            return pattern
    return None


def infer_pattern_from_directory(known: list[tuple[str, str]]) -> str | None:
    """Find the house pattern from any (full_name, email) pair at the domain."""
    for full_name, email in known:
        parts = normalize_name(full_name)
        if not parts:
            continue
        if pattern := infer_pattern(email, parts):
            log.info("inferred email pattern %r from %s", pattern, email.split("@")[-1])
            return pattern
    return None


def generate(
    full_name: str,
    domain: str,
    *,
    known_pattern: str | None = None,
    max_guesses: int | None = None,
) -> list[tuple[str, float, str]]:
    """Candidate addresses as (email, confidence, pattern).

    With ``known_pattern`` this returns exactly one high-confidence candidate.
    Without it, the prevalence-ordered table, capped and scaled well below the
    surfacing threshold.
    """
    name = normalize_name(full_name)
    if not name:
        log.debug("cannot generate patterns from %r", full_name)
        return []
    domain = domain.lower().strip().removeprefix("www.")

    if known_pattern:
        local = render(known_pattern, name)
        if local:
            return [(f"{local}@{domain}", INFERRED_CONFIDENCE, known_pattern)]

    limit = max_guesses if max_guesses is not None else settings.max_guesses_per_domain
    out: list[tuple[str, float, str]] = []
    seen: set[str] = set()
    for pattern, prior in PATTERNS[: max(0, limit)]:
        local = render(pattern, name)
        if not local or local in seen:
            continue
        seen.add(local)
        out.append((f"{local}@{domain}", min(GUESS_SCALE * prior, GUESS_CAP), pattern))
    return out
