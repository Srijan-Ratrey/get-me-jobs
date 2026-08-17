"""Profile -> 0-100 fit score, with every component recorded separately.

The components are stored in ``jobs.fit_reasons`` and always sum to the total.
That is not decoration: "why is everything a 70" is the failure mode this module
exists to avoid, and a score you cannot decompose is a score you cannot tune.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from ..config import Profile
from ..models import Job

log = logging.getLogger(__name__)

# Weights sum to exactly 100 so fit_reasons reconciles against fit_score.
W_TITLE = 40
W_MUST_HAVE = 25
W_NICE_TO_HAVE = 15
W_LOCATION = 10
W_SENIORITY = 10

# An unmarked title ("Data Scientist") earns this share of the seniority weight.
# Most junior-appropriate postings carry no level word at all, so scoring them
# zero would discard the majority of genuine matches.
UNMARKED_SENIORITY_CREDIT = 0.6

# Nice-to-haves saturate: matching 8 of 40 keywords is already a strong signal,
# and requiring all 40 would mean nobody ever scores well on this component.
NICE_TO_HAVE_SATURATION = 8

# Cities that ATS boards spell more than one way. Greenhouse's PhonePe board
# returns both "Bangalore" and "Bengaluru" in a single response, so matching the
# literal string the user typed silently drops half a city's postings.
LOCATION_ALIASES: list[frozenset[str]] = [
    frozenset({"bengaluru", "bangalore", "blr"}),
    frozenset({"mumbai", "bombay"}),
    frozenset({"delhi", "new delhi", "ncr"}),
    frozenset({"gurgaon", "gurugram"}),
    frozenset({"hyderabad", "secunderabad"}),
    frozenset({"kolkata", "calcutta"}),
    frozenset({"chennai", "madras"}),
    frozenset({"pune", "poona"}),
    frozenset({"kochi", "cochin"}),
    frozenset({"mysore", "mysuru"}),
    frozenset({"trivandrum", "thiruvananthapuram"}),
    frozenset({"vizag", "visakhapatnam"}),
    frozenset({"usa", "us", "united states", "america"}),
    frozenset({"uk", "united kingdom", "britain", "england"}),
    frozenset({"nyc", "new york"}),
    frozenset({"sf", "san francisco", "bay area"}),
]

# "5+ years", "3-5 years", "minimum 4 years", "at least 2 yrs", "4+ yoe".
# The range separator must include en and em dashes: job ads use "0–3 years" far
# more often than "0-3 years", and matching only the ASCII hyphen makes the regex
# skip the lower bound and capture the upper one instead — which turns an
# explicitly fresher-friendly "0–3 years" into a 3-year requirement.
_YEARS = re.compile(
    r"(\d{1,2})\s*(?:\+|\s*(?:[-–—]|to)\s*\d{1,2})?\s*(?:\+\s*)?(?:years?|yrs?|yoe)\b",
    re.IGNORECASE,
)
_REMOTE_HINT = ("remote", "work from home", "wfh", "anywhere", "distributed")


@dataclass
class Score:
    """A scored job: the total, the parts that made it, and why."""

    total: int
    components: dict[str, int] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    disqualified: str | None = None

    def as_fit_reasons(self) -> dict:
        return {
            "total": self.total,
            "components": self.components,
            "reasons": self.reasons,
            "disqualified": self.disqualified,
        }


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9+#.]+", text.lower()))


def _contains_word(haystack: str, needle: str) -> bool:
    """Whole-word containment, so "lead" does not match "leadership"."""
    return re.search(rf"(?<!\w){re.escape(needle.lower())}(?!\w)", haystack.lower()) is not None


def _satisfied_by(haystack: str, requirement: str) -> str | None:
    """The alternative that satisfies a must-have, or None.

    A must-have may list pipe-separated alternatives ("python|pytorch|pandas"),
    any one of which counts. Without this a single unwritten word forfeits the
    whole must-have weight: 71% of otherwise-qualifying postings scored 0/25
    because their description never spelled "python", including ML roles whose
    titles matched perfectly and whose text was all PyTorch and NumPy.
    """
    for alternative in requirement.split("|"):
        alternative = alternative.strip()
        if alternative and _contains_word(haystack, alternative):
            return alternative
    return None


def _expand_aliases(values: list[str]) -> set[str]:
    """Add every known alias of each configured location."""
    out: set[str] = set()
    for value in values:
        v = value.strip().lower()
        if not v:
            continue
        out.add(v)
        for group in LOCATION_ALIASES:
            if v in group:
                out |= group
    return out


# Remote postings that really are open to anyone, as opposed to remote-within-a-country.
_GLOBALLY_OPEN = ("anywhere", "worldwide", "global", "any location", "any country")

# Entries in `locations` that describe *how* you work rather than *where* you are.
# These must never become place aliases. A profile listing "Remote" is saying it
# will accept remote work; it is not saying that anywhere calling itself remote is
# reachable. Conflating the two let "USA | Remote" and "Remote - California"
# satisfy a Bengaluru preference, which put 397 unreachable postings — over 40% of
# everything that passed this gate — into the shortlist.
_MODALITY_WORDS = frozenset(
    {
        "remote",
        "anywhere",
        "worldwide",
        "global",
        "hybrid",
        "wfh",
        "work from home",
        "distributed",
        "onsite",
        "on-site",
        "on site",
    }
)


def _place_aliases(wanted: list[str]) -> set[str]:
    """Alias-expanded set of the wanted entries that actually name a place."""
    return _expand_aliases([w for w in wanted if w.strip().lower() not in _MODALITY_WORDS])


def _names_place(text: str, aliases: set[str]) -> bool:
    """Whole-word alias match against a location string.

    Word boundaries, not substrings: "india" otherwise matches "Indianapolis"
    and "Indiana", quietly filing US postings under an India-only shortlist.
    """
    return any(re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", text) for alias in aliases)

# Words a location string can contain that do not name a place. Anything left over
# after these and the remote words are removed is taken to be a real region — which
# is what distinguishes "Remote" from "Remote - EU".
_LOCATION_FILLER = frozenset(
    """in at or and the of a an office offices based flexible friendly travel optional
    required preferred first team full part time hq headquarters area region multiple
    locations location various any some all across within near from to""".split()
)


def matches_location(location: str | None, remote: bool, wanted: list[str]) -> bool:
    """Can someone in one of the wanted places actually take this job?

    Shares LOCATION_ALIASES with scoring so a filter for "bengaluru" also catches
    "Bangalore". Exposed because location is only 10 of 100 points — the right
    weight for ranking, far too weak for filtering: a perfect-fit role in San
    Francisco scores 86 and crowds out every reachable role beneath it.

    "Remote" is not treated as matching everywhere. "USA | Remote" and
    "Remote - EU" are remote *within a region*, and a candidate in Bengaluru
    cannot take either. A remote posting qualifies only when it names a wanted
    region, says it is open globally, or names no region at all.

    Note that "Remote" appearing in `wanted` is deliberately *not* a place. It
    only expresses willingness to work remotely, so it is stripped before the
    place check and the region logic below decides the outcome instead.
    """
    if not wanted:
        return True
    text = (location or "").lower()
    if _names_place(text, _place_aliases(wanted)):
        return True

    looks_remote = remote or any(hint in text for hint in _REMOTE_HINT)
    if not looks_remote:
        return False
    if any(hint in text for hint in _GLOBALLY_OPEN):
        return True
    # Remote but tied to somewhere else: strip the remote vocabulary and see what
    # place name is left. Anything remaining is a region, and since the aliases
    # already failed to match, it is not a region we want. Length is not a usable
    # test here — "EU" is two characters and disqualifying.
    residue = text
    for hint in ("remote-friendly", "remote friendly", *_REMOTE_HINT, *_GLOBALLY_OPEN,
                 "hybrid", "onsite", "on-site", "on site"):
        residue = residue.replace(hint, " ")
    leftover = [w for w in re.findall(r"[a-z]+", residue) if w not in _LOCATION_FILLER]
    return not leftover


def min_years_required(description: str | None) -> int | None:
    """The smallest experience requirement stated anywhere in a description.

    Deliberately the minimum, not the maximum. Postings routinely bundle several
    roles' requirements into one body, or mention a senior band further down, so
    taking the largest number would reject perfectly open junior roles. If even
    the smallest stated figure is out of range, the posting really is not junior.
    """
    if not description:
        return None
    found = [int(m.group(1)) for m in _YEARS.finditer(description)]
    # 0 is not a constraint ("0-2 years" reads as open to freshers).
    found = [y for y in found if 0 < y <= 40]
    return min(found) if found else None


def score_job(job: Job, profile: Profile) -> Score:
    """Score one job against the profile. Always returns a decomposable Score."""
    title = (job.title or "").lower()
    description = job.description or ""
    haystack = f"{title}\n{description}".lower()

    # ---- hard disqualifiers, checked before anything else ---------------- #

    # Title only. Against the description this would zero good junior postings
    # that merely mention working with senior engineers.
    for keyword in profile.exclude_keywords:
        if _contains_word(title, keyword):
            return Score(
                total=0,
                components={k: 0 for k in ("title", "must_have", "nice_to_have", "location", "seniority")},
                reasons=[f"excluded: title contains {keyword!r}"],
                disqualified=f"exclude_keyword:{keyword}",
            )

    if profile.max_years_experience is not None:
        required = min_years_required(description)
        if required is not None and required > profile.max_years_experience:
            return Score(
                total=0,
                components={k: 0 for k in ("title", "must_have", "nice_to_have", "location", "seniority")},
                reasons=[
                    f"excluded: needs {required}+ years, cap is {profile.max_years_experience}"
                ],
                disqualified=f"years_required:{required}",
            )

    components: dict[str, int] = {}
    reasons: list[str] = []

    # ---- title (40) ------------------------------------------------------ #
    # Matched against the full title rather than canonical_title, which strips
    # the qualifiers ("- Machine Learning") that carry the signal.
    if not profile.titles:
        components["title"] = W_TITLE
        reasons.append("title: no preference set, full credit")
    else:
        hits = [t for t in profile.titles if t.strip() and t.lower() in title]
        if hits:
            components["title"] = W_TITLE
            reasons.append(f"title: matches {hits[0]!r}")
        else:
            # Partial credit on shared significant words, so "Data Science
            # Engineer" is not treated the same as "Account Executive".
            profile_words = _words(" ".join(profile.titles)) - {"engineer", "and", "of", "the"}
            overlap = profile_words & _words(title)
            if overlap:
                components["title"] = round(W_TITLE * 0.4)
                reasons.append(f"title: partial overlap {sorted(overlap)}")
            else:
                components["title"] = 0
                reasons.append("title: no match")

    # ---- must-haves (25), all-or-proportional ---------------------------- #
    musts = [k for k in profile.must_have_keywords if k.strip()]
    if not musts:
        components["must_have"] = W_MUST_HAVE
        reasons.append("must_have: none set, full credit")
    else:
        # Report the alternative that actually matched, not the group, so --why
        # never claims "python" was found when the text only said "pytorch".
        present = [hit for k in musts if (hit := _satisfied_by(haystack, k))]
        missing = [k for k in musts if _satisfied_by(haystack, k) is None]
        components["must_have"] = round(W_MUST_HAVE * len(present) / len(musts))
        reasons.append(
            f"must_have: {len(present)}/{len(musts)} present {present}"
            + (f", missing {missing}" if missing else "")
        )

    # ---- nice-to-haves (15), saturating ---------------------------------- #
    nices = [k for k in profile.nice_to_have_keywords if k.strip()]
    if not nices:
        components["nice_to_have"] = W_NICE_TO_HAVE
        reasons.append("nice_to_have: none set, full credit")
    else:
        # Whole-word, not substring: "gan" otherwise matches "organization" and
        # "lora" matches "exploration", handing out credit for skills the posting
        # never mentions and floating irrelevant jobs above relevant ones.
        present = [k for k in nices if _contains_word(haystack, k)]
        ratio = min(1.0, len(present) / min(NICE_TO_HAVE_SATURATION, len(nices)))
        components["nice_to_have"] = round(W_NICE_TO_HAVE * ratio)
        reasons.append(f"nice_to_have: {len(present)}/{len(nices)} present {present[:6]}")

    # ---- location / remote (10) ------------------------------------------ #
    # Scoring defers to matches_location so ranking and filtering can never
    # disagree. They used to run separate substring checks, and the scoring copy
    # awarded full credit for any posting mentioning "remote" — which is how
    # US-only roles came to outrank reachable Bengaluru ones.
    location = (job.location or "").lower()
    places = _place_aliases(profile.locations)
    if profile.remote_only:
        if job.remote:
            components["location"] = W_LOCATION
            reasons.append("location: remote, as required")
        else:
            components["location"] = 0
            reasons.append("location: remote_only set but posting is not remote")
    elif not profile.locations:
        components["location"] = W_LOCATION
        reasons.append("location: no preference set, full credit")
    elif matches_location(job.location, bool(job.remote), profile.locations):
        components["location"] = W_LOCATION
        matched = sorted(p for p in places if _names_place(location, {p}))
        reasons.append(
            f"location: matches {matched[0]!r}" if matched else "location: remote and not region-locked"
        )
    else:
        components["location"] = 0
        reasons.append(f"location: {job.location!r} not reachable from {sorted(places)[:4]}...")

    # ---- seniority (10) -------------------------------------------------- #
    wanted_levels = {s.strip().lower() for s in profile.seniority if s.strip()}
    level = (job.seniority or "").lower()
    if not wanted_levels:
        components["seniority"] = W_SENIORITY
        reasons.append("seniority: no preference set, full credit")
    elif level and level in wanted_levels:
        components["seniority"] = W_SENIORITY
        reasons.append(f"seniority: {level!r} wanted")
    elif not level:
        components["seniority"] = round(W_SENIORITY * UNMARKED_SENIORITY_CREDIT)
        reasons.append("seniority: title carries no level, partial credit")
    else:
        components["seniority"] = 0
        reasons.append(f"seniority: {level!r} not in {sorted(wanted_levels)}")

    total = sum(components.values())
    return Score(total=total, components=components, reasons=reasons)
