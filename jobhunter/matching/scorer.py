"""Profile -> 0-100 fit score, with every component recorded separately.

The components are stored in ``jobs.fit_reasons`` and always sum to the total.
That is not decoration: "why is everything a 70" is the failure mode this module
exists to avoid, and a score you cannot decompose is a score you cannot tune.
"""

from __future__ import annotations

import heapq
import logging
import math
import re
from collections.abc import Iterable
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

# Below this many characters a description is a stub -- a company blurb, or a
# title with a sentence under it -- and the absence of a skill in it is not
# evidence the job does not need that skill.
#
# Measured over every open posting with a full title match: descriptions under
# 1k chars mention a must-have 6-11% of the time, those over 1.5k mention one
# 71-83% of the time, and almost nothing sits in between. The population is two
# populations. Scoring the stubs 0/25 asserted the opposite of the base rate and
# cost 25 points to 508 postings, among them the most junior-appropriate titles
# in the database -- "Junior ML Engineer (Computer Vision)" and "Machine
# Learning Engineer Intern", both in Bangalore, both stranded at 62-64.
UNINFORMATIVE_DESCRIPTION_CHARS = 1500

# What an unjudgeable must-have earns. The measured base rate among descriptions
# long enough to judge is 78%, so this is deliberately conservative: an unknown
# should not outrank a posting that actually stated the requirement. Matches the
# 0.6 already used for an unmarked seniority, which is the same kind of unknown.
UNKNOWN_MUST_HAVE_CREDIT = 0.6

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
# The lookbehind keeps "6.5+ years" from reading as the 5 after the point. The
# decimal is rounded up: "2.5+ years" exceeds a 2-year cap, and a list number
# run into a range ("1.4–7 years", item 1 asking 4–7) cannot read as under 2.
_YEARS = re.compile(
    r"(?<![\d.])(\d{1,2}(?:\.\d)?)\s*(?:\+|\s*(?:[-–—]|to)\s*\d{1,2}(?:\.\d)?)?\s*(?:\+\s*)?"
    r"(?:years?|yrs?|yoe)\b",
    re.IGNORECASE,
)
# A figure for the whole career, as opposed to one skill within it. "4–6 years of
# professional software engineering experience, including at least 2 years
# specifically focused on applied AI" is a 4-year role; reading its 2 as the
# floor passed Brillio, Colruyt ("8+ years of software engineering experience. 2
# years of hands-on experience in developing agentic applications") and Zensar
# ("Overall IT Exp: 6.5+years ... Data Science Exp: 2+ Yrs") under a 2-year cap.
_OVERALL_YEARS = re.compile(
    r"\b(?:overall|total|professional|industry|work|relevant|it)\s+(?:\w+\s+){0,2}?(?:experience|exp)\b"
    r"|\bsoftware\s+(?:engineering|development)\s+experience\b",
    re.IGNORECASE,
)
# Years offered instead of a degree: "a STEM discipline or 5+ years of professional
# experience". The profile's applicant has the degree, so the figure is not theirs.
_INSTEAD_OF_DEGREE = re.compile(
    r"\b(?:degree|bachelor'?s?|b\.?\s?tech|b\.?e\.?|b\.?sc|discipline|stem|equivalent)\b[^.;]{0,100}\bor\s*$",
    re.IGNORECASE,
)
# A field label on the line above its value.
_FIELD_LABEL = re.compile(r"([^\n]*:)[ \t]*\n\s*$")
# A figure carved out of a larger one: "including at least 2 years", "of which 1".
_SUB_YEARS = re.compile(
    r"\b(?:including|incl\.?|of\s+which|out\s+of\s+which|specifically|within\s+(?:that|this|which))\b",
    re.IGNORECASE,
)
# Degrees that gate a lower experience band. Spelled out rather than matching
# bare "MS"/"MA", which collide with ordinary words and requisition codes.
_POSTGRAD = re.compile(
    r"\b(master'?s?|m\.?\s?tech|m\.?\s?sc|ph\.?\s?d|doctoral|doctorate)\b",
    re.IGNORECASE,
)

# "must be 18 years of age" is not an experience requirement. Across 20k open
# postings this accounts for 1,124 matches at 18 and 312 at 21 -- by far the
# two biggest spikes in the distribution, and every one of them spurious.
_AGE_NOT_EXPERIENCE = re.compile(r"^\s*(of age|or older|or above|old\b)", re.IGNORECASE)

# Above this, a figure is company history rather than anything asked of the
# reader: "building on more than 30 years of investing experience". No real
# posting asks for more, and the measured tail past 20 is 478 matches of
# exactly that kind.
MAX_PLAUSIBLE_YEARS = 20
_REMOTE_HINT = ("remote", "work from home", "wfh", "anywhere", "distributed")


# Languages a posting might require. English is absent on purpose: every
# posting this tool reads is in English, so requiring it rules nobody out.
_LANGUAGES = frozenset(
    """german french spanish japanese mandarin chinese cantonese korean dutch italian
    portuguese arabic russian polish swedish norwegian danish finnish turkish hebrew thai
    vietnamese indonesian bahasa czech hungarian romanian greek ukrainian hindi kannada
    tamil telugu marathi bengali malayalam gujarati punjabi urdu odia""".split()
)
# Case-sensitive: a language name is capitalised, and "polish the docs" is a verb.
# A name followed by "-speaking" describes a market or audience ("Spanish-speaking
# markets"), not the reader, so it is not counted.
_LANGUAGE_NAME = re.compile(
    r"\b("
    + "|".join(f"{n.capitalize()}|{n.upper()}" for n in sorted(_LANGUAGES))
    + r")\b(?!-speaking)"
)
# Most of these names are also adjectives ("German automotive clients", "the
# Japanese market"), so a name only counts with a fluency word beside it. Strong
# cues may sit a few words away. Weak ones ("written", "verbal") also describe
# communication in general, so they must share the name's clause: in "written
# and verbal communication, experience with the Japanese market" they are a
# comma away and say nothing about Japanese.
_STRONG_FLUENCY = re.compile(
    r"fluen\w*|proficien\w*|\bnative\b|mother\s+tongue|\bspeak\w*|business[- ]level"
    r"|\b[BC][12]\b",
    re.IGNORECASE,
)
# "Polish" is the one name that is also an everyday verb, and sentence-initial
# capitals defeat the case check: "Polish written specs before review". As a
# language it is followed by a noun about language, or by nothing at all.
_POLISH_THE_VERB = re.compile(
    r"Polish\s+(?!(?:language|speaker|speakers|skills|fluency|proficiency|native|is|are"
    r"|required|and|or)\b)[a-z]"
)
_WEAK_FLUENCY = re.compile(r"\bspoken\b|\bwritten\b|\bverbal\b|\blanguage\b", re.IGNORECASE)
_FLUENCY_WINDOW = 40
# "Fluency in French is a plus" is a preference, not a requirement.
_OPTIONAL = re.compile(
    r"\bplus\b|\bbonus\b|preferred|nice[- ]to[- ]have|advantage|desirable|not\s+required"
    r"|optional|beneficial|\ban\s+asset\b|\bideally\b",
    re.IGNORECASE,
)
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n|;|•")

# Measured on 3,419 open postings mentioning "clearance": the security sense is
# always qualified ("TS/SCI security clearance", "Top Secret clearance") or a
# field header ("Clearance Level: Secret"), which is what keeps "customs
# clearance" and "creepage and clearance" out.
_CLEARANCE = re.compile(
    r"\b(?:security|secret|top[- ]secret|ts|ts/sci|sci|dod|government|public\s+trust|federal"
    r"|active|interim|nv[12]|baseline|dv|sc)\s+clearances?\b"
    r"|^\s*clearance(?:\s+level)?\s*:"
    r"|\b(?:hold|holds|obtain|maintain|possess)\s+(?:an?\s+)?(?:active\s+)?(?:u\.?s\.?\s+)?clearance\b",
    re.IGNORECASE | re.MULTILINE,
)


def required_language(text: str | None, spoken: list[str]) -> str | None:
    """A language the posting requires that is not in ``spoken``, or None.

    Judged one sentence at a time. A sentence counts when a language name sits
    within a few words of a fluency word and nothing in it softens the ask. A
    sentence naming several languages ("English and either German, French or
    Spanish") passes when any of them is spoken, since it offers a choice.
    """
    if not text or not _LANGUAGE_NAME.search(text):
        return None
    known = {s.strip().lower() for s in spoken}
    for sentence in _SENTENCE.split(text):
        if _OPTIONAL.search(sentence):
            continue
        named = set()
        for match in _LANGUAGE_NAME.finditer(sentence):
            if _POLISH_THE_VERB.match(sentence, match.start()):
                continue
            near = sentence[max(0, match.start() - _FLUENCY_WINDOW) : match.end() + _FLUENCY_WINDOW]
            start = sentence.rfind(",", 0, match.start()) + 1
            end = sentence.find(",", match.end())
            clause = sentence[start : end if end != -1 else len(sentence)]
            if _STRONG_FLUENCY.search(near) or _WEAK_FLUENCY.search(clause):
                named.add(match.group(1).lower())
        if named and not named & known:
            return sorted(named)[0]
    return None


_REQUIRED = re.compile(r"\bmust\b|\brequired\b|\bmandatory\b", re.IGNORECASE)
# Saying a clearance is unnecessary: "not required", "no security clearance
# required", a header answered "Clearance: None".
_NO_CLEARANCE = re.compile(
    r"not\s+(?:be\s+)?(?:required|needed|necessary)|\bno\s+(?:\w+\s+){0,2}clearance"
    r"|clearance(?:\s+level)?\s*:\s*(?:none|n/?a|not\s+required)\b",
    re.IGNORECASE,
)


def requires_clearance(text: str | None) -> bool:
    """Does the posting require a security clearance, rather than merely welcome one?

    15 of the 424 postings this rule first caught said "clearance preferred" or
    "is a strong plus". A softened sentence still counts when it also says
    must or required: "MUST possess Active TS/SCI clearance (Poly preferred)".
    """
    if not text or not _CLEARANCE.search(text):
        return False
    return any(
        _CLEARANCE.search(sentence)
        and not _NO_CLEARANCE.search(sentence)
        and (not _OPTIONAL.search(sentence) or _REQUIRED.search(sentence))
        for sentence in _SENTENCE.split(text)
    )


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
    # A space in the needle matches any whitespace: html_to_text puts inline tags
    # on their own lines, so "<b>machine</b> learning" arrives as "machine\nlearning".
    pattern = r"\s+".join(re.escape(word) for word in needle.lower().split())
    return re.search(rf"(?<!\w){pattern}(?!\w)", haystack.lower()) is not None


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


def place_aliases(wanted: list[str]) -> set[str]:
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


# Somewhere a remote role can be tied to. A bare "US" must be upper case, so
# "contact us only" never reads as a country.
_ELSEWHERE = (
    r"(?:the\s+)?(?:continental\s+)?(?:(?-i:US|U\.S\.?|USA)\b|united\s+states|canada|(?-i:UK)\b"
    r"|united\s+kingdom|europe|(?-i:EU|EEA)\b|germany|france|spain|netherlands|poland|ireland"
    r"|portugal|australia|brazil|mexico|latam|latin\s+america|philippines|singapore|japan"
    r"|israel|argentina|colombia|south\s+africa|nigeria|kenya|pakistan|vietnam|indonesia"
    r"|emea|north\s+america|americas)"
)
# Phrasings measured on the shortlist's region-less remote roles: "U.S. citizenship
# required", "Remote (U.S", "based anywhere in the US", "authorized to work in the
# U.S.", "Eligible to work in Germany", "The role is based in Spain".
_RESTRICTED_ELSEWHERE = re.compile(
    r"(?:must|should|need\s+to|required\s+to)\s+(?:be\s+)?(?:physically\s+)?"
    r"(?:located|based|resid\w*|living|live)\s+(?:in|within)\s+"
    + _ELSEWHERE
    + r"|(?:authori[sz]ed|eligible|legally\s+(?:authori[sz]ed|able))\s+to\s+work\s+(?:in|within|for)\s+"
    + _ELSEWHERE
    + r"|(?-i:US|U\.S\.|USA)[\s-]+(?:only|based\s+only|citizens?(?:hip)?|based\s+(?:positions?|roles?|candidates))"
    + r"|remote\s*(?:\(|-|–|,)?\s*(?:within|in)?\s*"
    + _ELSEWHERE
    + r"|anywhere\s+in\s+"
    + _ELSEWHERE
    + r"|(?:role|position|job)\s+is\s+based\s+in\s+"
    + _ELSEWHERE
    + r"|open\s+(?:only\s+)?to\s+(?:candidates|applicants|residents)\s+(?:in|of|based\s+in)\s+"
    + _ELSEWHERE,
    re.IGNORECASE,
)


def _restricted_elsewhere(description: str | None, places: set[str]) -> bool:
    """Does the posting tie a region-less remote role to somewhere not wanted?

    "Remote within the US or India" names a wanted place right after the
    restriction, so a mention of one in the next few words cancels it.
    """
    for match in _RESTRICTED_ELSEWHERE.finditer(description or ""):
        tail = description[match.end() : match.end() + 40].lower()
        if not _names_place(tail, places):
            return True
    return False


def matches_location(
    location: str | None, remote: bool, wanted: list[str], description: str | None = None
) -> bool:
    """Can someone in one of the wanted places actually take this job?

    Shares LOCATION_ALIASES with scoring so a filter for "bengaluru" also catches
    "Bangalore". Exposed because location is only 10 of 100 points — the right
    weight for ranking, far too weak for filtering: a perfect-fit role in San
    Francisco scores 86 and crowds out every reachable role beneath it.

    "Remote" is not treated as matching everywhere. "USA | Remote" and
    "Remote - EU" are remote *within a region*, and a candidate in Bengaluru
    cannot take either. A remote posting qualifies only when it names a wanted
    region, says it is open globally, or names no region at all -- and, when a
    `description` is given, the posting does not restrict it elsewhere in prose
    ("U.S. citizenship required", "based anywhere in the US").

    Note that "Remote" appearing in `wanted` is deliberately *not* a place. It
    only expresses willingness to work remotely, so it is stripped before the
    place check and the region logic below decides the outcome instead.
    """
    if not wanted:
        return True
    text = (location or "").lower()
    places = place_aliases(wanted)
    if _names_place(text, places):
        return True

    looks_remote = remote or any(hint in text for hint in _REMOTE_HINT)
    if not looks_remote:
        return False
    # No early return on "anywhere" or "global": "Anywhere in Latin America" is
    # tied to a region, and the residue test below is what notices.
    # Remote but tied to somewhere else: strip the remote vocabulary and see what
    # place name is left. Anything remaining is a region, and since the aliases
    # already failed to match, it is not a region we want. Length is not a usable
    # test here — "EU" is two characters and disqualifying.
    residue = text
    for hint in (
        "remote-friendly",
        "remote friendly",
        *_REMOTE_HINT,
        *_GLOBALLY_OPEN,
        "hybrid",
        "onsite",
        "on-site",
        "on site",
    ):
        residue = residue.replace(hint, " ")
    leftover = [w for w in re.findall(r"[a-z]+", residue) if w not in _LOCATION_FILLER]
    return not leftover and not _restricted_elsewhere(description, places)


def _clause_before(description: str, start: int, window: int = 140) -> str:
    """The text leading up to a match, back to the start of its own clause.

    Only backwards: a figure is qualified by what precedes it. Looking forward
    from "4-6 years of experience with a bachelor's degree ... or a master's
    degree or PhD with 2-4 years" would find "master" for *both* figures and
    discard the requirement entirely.
    """
    chunk = description[max(0, start - window) : start]
    for separator in ("•", ";", ". ", "\n"):
        index = chunk.rfind(separator)
        if index != -1:
            chunk = chunk[index + len(separator) :]
    return chunk


def min_years_required(description: str | None) -> int | None:
    """The smallest experience requirement a reader could actually qualify under.

    Deliberately the minimum, not the maximum. Postings routinely bundle several
    roles' requirements into one body, or mention a senior band further down, so
    taking the largest number would reject perfectly open junior roles. If even
    the smallest stated figure is out of range, the posting really is not junior.

    But the minimum is only a real floor if its conditions are reachable. G2's
    Data Scientist asks for "4-6 years with a bachelor's degree ... or a
    master's degree or PhD with 2-4 years", and reading that as a 2-year role
    scored it 90 for an applicant holding a bachelor's, who is actually looking
    at the 4-6 band. A lower band gated behind a postgraduate degree is a
    different candidate's route, not a cheaper version of this one.

    So degree-gated figures are set aside *when an ungated one exists*. Where
    every figure is gated there is no alternative branch to choose between --
    "Master's preferred. 3+ years." is one requirement with a note attached --
    and they are all used, which keeps the disqualifier from silently going
    blind on those postings.

    This assumes the reader does not hold a postgraduate degree. That is the
    conservative direction for a checker whose failure mode is false positives,
    and the fix if it is ever wrong is a profile flag, not a rewrite.
    """
    if not description:
        return None

    ungated: list[tuple[int, bool, bool]] = []
    gated: list[tuple[int, bool, bool]] = []
    for match in _YEARS.finditer(description):
        years = math.ceil(float(match.group(1)))
        # 0 is not a constraint ("0-2 years" reads as open to freshers).
        if not 0 < years <= MAX_PLAUSIBLE_YEARS:
            continue
        if _AGE_NOT_EXPERIENCE.match(description[match.end() :]):
            continue
        before = _clause_before(description, match.start())
        if not before.strip() and (
            label := _FIELD_LABEL.search(description[: match.start()][-120:])
        ):
            # "Overall IT Exp:" on one line and "6.5+years" on the next.
            before = label.group(1)
        if _INSTEAD_OF_DEGREE.search(before):
            continue
        after = re.split(r"[.;•\n]", description[match.end() : match.end() + 80], maxsplit=1)[0]
        # Only the figure's own phrase: "experience, including at least 2 years"
        # must not borrow the main clause's "professional ... experience".
        sub = bool(_SUB_YEARS.search(before[-40:]))
        own = before.rsplit(",", 1)[-1][-60:]
        overall = not sub and bool(_OVERALL_YEARS.search(own + " " + after))
        bucket = gated if _POSTGRAD.search(before) else ungated
        bucket.append((years, overall, sub))

    pool = ungated or gated
    if not pool:
        return None
    # A stated career total is the requirement; a skill carved out of it is not.
    if overall_figures := [y for y, overall, _ in pool if overall]:
        return min(overall_figures)
    return min([y for y, _, sub in pool if not sub] or [y for y, _, _ in pool])


# The level a description states outright, when the title does not. Barclays
# titles a role "Gen AI Engineer" and puts "Assistant Vice President
# Expectations" in the body; banks and LinkedIn-style postings carry fields such
# as "Corporate Title: AVP" or "Seniority level: Mid-Senior level". Checked
# against the profile's exclude_keywords, so no new vocabulary: "Analyst" or
# "Fresher / Entry level" pass, "Senior Associate" does not.
_STATED_LEVEL = re.compile(
    r"\b((?:assistant\s+|senior\s+)?vice\s+president|managing\s+director|director)\s+expectations\b"
    r"|\b(?:corporate\s+title|job\s+level|career\s+level|position\s+level|job\s+grade|seniority\s+level)"
    r"\s*[:\-–]\s*([a-z][a-z /&-]{1,30})",
    re.IGNORECASE,
)


def stated_level(description: str | None) -> str | None:
    """The role's level as the description declares it, lower-cased, or None."""
    match = _STATED_LEVEL.search(description or "")
    return (match.group(1) or match.group(2)).strip().lower() if match else None


def _zero(reason: str, disqualified: str) -> Score:
    return Score(
        total=0,
        components={k: 0 for k in ("title", "must_have", "nice_to_have", "location", "seniority")},
        reasons=[f"excluded: {reason}"],
        disqualified=disqualified,
    )


def title_excluded(title: str | None, keywords: list[str]) -> str | None:
    """The exclude keyword the title contains, whole-word, or None.

    Public because the description-fetching adapters ask it too: a posting
    this zeroes on its title scores 0 whatever its description says, so
    fetching that description is a wasted request.
    """
    return next((k for k in keywords if _contains_word(title or "", k)), None)


def _disqualifier(job: Job, profile: Profile) -> Score | None:
    """The first hard rule the job breaks, as a zero Score, or None."""
    title = (job.title or "").lower()
    description = job.description or ""

    # Title only. Against the description this would zero good junior postings
    # that merely mention working with senior engineers.
    if keyword := title_excluded(title, profile.exclude_keywords):
        return _zero(f"title contains {keyword!r}", f"exclude_keyword:{keyword}")

    if profile.max_years_experience is not None:
        required = min_years_required(description)
        if required is not None and required > profile.max_years_experience:
            return _zero(
                f"needs {required}+ years, cap is {profile.max_years_experience}",
                f"years_required:{required}",
            )

    posting = f"{job.title or ''}\n{description}"
    if profile.languages and (language := required_language(posting, profile.languages)):
        return _zero(
            f"requires {language.title()}, which profile.languages does not list",
            f"language:{language}",
        )

    if not profile.eligible_for_clearance and requires_clearance(description):
        return _zero("requires a security clearance", "clearance")

    if (level := stated_level(description)) and (
        keyword := title_excluded(level, profile.exclude_keywords)
    ):
        return _zero(f"description states the level {level!r}", f"stated_level:{keyword}")
    return None


def score_job(job: Job, profile: Profile, *, gates: bool = True) -> Score:
    """Score one job against the profile. Always returns a decomposable Score.

    ``gates=False`` skips the hard disqualifiers, scoring the posting as if
    none of them existed. Only the filter audit wants that: it is how a rule's
    cost is measured.
    """
    if gates and (zero := _disqualifier(job, profile)):
        return zero

    title = (job.title or "").lower()
    description = job.description or ""
    haystack = f"{title}\n{description}".lower()

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
        if not present and len(description) < UNINFORMATIVE_DESCRIPTION_CHARS:
            # Nothing matched, but there was nothing to match against. Say so in
            # the reason rather than reporting a confident 0, so --why never
            # claims a skill is absent when the posting simply never said.
            components["must_have"] = round(W_MUST_HAVE * UNKNOWN_MUST_HAVE_CREDIT)
            reasons.append(
                f"must_have: unknown, description is {len(description)} chars "
                f"and states no requirements; partial credit"
            )
        else:
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
    places = place_aliases(profile.locations)
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
    elif matches_location(job.location, bool(job.remote), profile.locations, job.description):
        components["location"] = W_LOCATION
        matched = sorted(p for p in places if _names_place(location, {p}))
        reasons.append(
            f"location: matches {matched[0]!r}"
            if matched
            else "location: remote and not region-locked"
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


@dataclass
class RuleAudit:
    """What one hard rule zeroed, and what those postings would have scored without it."""

    rule: str
    zeroed: int = 0
    # Postings this rule alone kept out of the shortlist: without the hard
    # rules they reach the profile's min_score.
    would_pass: int = 0
    # (score without the rules, title, company) for the best of what it zeroed.
    examples: list[tuple[int, str, str]] = field(default_factory=list)


def audit_rules(jobs: Iterable[Job], profile: Profile, *, examples: int = 3) -> list[RuleAudit]:
    """Replay every hard rule over the jobs and price each one.

    A hard zero leaves no trace separating "correctly excluded" from "excluded
    by a parsing bug": the "must be 18 years of age" bug zeroed 1,124 postings
    and was found only by looking at what a rule had thrown away. Re-scoring
    each zeroed posting with the rules off says which rules cost anything. One
    that zeroes 10,000 sales roles which would have scored 12 is free; one
    that zeroes 40 roles which would have scored 75 is worth reading.

    A job is charged to the first rule it breaks, the one that decided it.
    """
    audits: dict[str, RuleAudit] = {}
    candidates: dict[str, list[tuple[int, str, str]]] = {}
    for job in jobs:
        zero = _disqualifier(job, profile)
        if zero is None:
            continue
        shadow = score_job(job, profile, gates=False).total
        audit = audits.setdefault(zero.disqualified, RuleAudit(zero.disqualified))
        audit.zeroed += 1
        audit.would_pass += shadow >= profile.min_score
        company = job.company.name if job.company else ""
        candidates.setdefault(zero.disqualified, []).append((shadow, job.title, company))
    for rule, audit in audits.items():
        audit.examples = heapq.nlargest(examples, candidates[rule])
    return sorted(audits.values(), key=lambda a: (-a.would_pass, -a.zeroed))
