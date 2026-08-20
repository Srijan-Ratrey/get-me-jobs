"""SQLAlchemy ORM models plus the RawJob transport object."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

from pydantic import BaseModel
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """Current UTC time, timezone-aware.

    Be aware of the asymmetry this creates: SQLite's DateTime column drops the
    offset on write, so a value is aware in memory and **naive** once read back.
    Both are UTC, so nothing is lost — but comparing a freshly-created `utcnow()`
    against a column loaded from the DB raises TypeError, and building a query
    filter from an aware datetime silently matches nothing. Filters belong in
    naive UTC; see ``pipeline.resolve_since``.
    """
    return datetime.now(timezone.utc)


def hash_email(email: str) -> str:
    """Stable hash for the suppression list.

    Normalisation lives here rather than at each call site so that a write and a
    later lookup cannot disagree about casing or stray whitespace — a mismatch
    would silently un-suppress an address someone asked to be forgotten.
    """
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


class Base(DeclarativeBase):
    pass


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    domain: Mapped[str | None] = mapped_column(String(255), index=True)
    ats: Mapped[str | None] = mapped_column(String(50))
    ats_token: Mapped[str | None] = mapped_column(String(255))
    careers_url: Mapped[str | None] = mapped_column(String(500))
    # Domain-level property, cached so we do not re-probe every run.
    catch_all: Mapped[bool | None] = mapped_column(Boolean)
    email_pattern: Mapped[str | None] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    jobs: Mapped[list[Job]] = relationship(back_populates="company")
    contacts: Mapped[list[Contact]] = relationship(back_populates="company")


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("content_hash", name="uq_jobs_content_hash"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    source: Mapped[str] = mapped_column(String(50))
    external_id: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(500))
    canonical_title: Mapped[str] = mapped_column(String(500), index=True)
    seniority: Mapped[str | None] = mapped_column(String(50))
    location: Mapped[str | None] = mapped_column(String(255))
    remote: Mapped[bool] = mapped_column(Boolean, default=False)
    description: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str] = mapped_column(String(1000))
    posted_at: Mapped[datetime | None] = mapped_column(DateTime)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)

    # Lifecycle: rows are never deleted. A posting disappearing is itself signal.
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime)

    fit_score: Mapped[int | None] = mapped_column(Integer, index=True)
    fit_reasons: Mapped[dict | None] = mapped_column(JSON)

    # Optional second opinion from `score --llm`, deliberately stored beside the
    # keyword score rather than overwriting it: the two are only useful if they
    # can be compared, and a bad prompt must not be able to destroy fit_score.
    llm_score: Mapped[int | None] = mapped_column(Integer, index=True)
    llm_verdict: Mapped[dict | None] = mapped_column(JSON)

    company: Mapped[Company] = relationship(back_populates="jobs")


class Contact(Base):
    __tablename__ = "contacts"
    __table_args__ = (UniqueConstraint("company_id", "email", name="uq_contact_company_email"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    email: Mapped[str] = mapped_column(String(320), index=True)
    kind: Mapped[str] = mapped_column(String(20), default="role")  # role | person
    first_name: Mapped[str | None] = mapped_column(String(100))
    last_name: Mapped[str | None] = mapped_column(String(100))
    role_title: Mapped[str | None] = mapped_column(String(255))

    # Provenance is a GDPR requirement, not a nice-to-have: you must be able to
    # say where any personal data came from.
    source_url: Mapped[str | None] = mapped_column(String(1000))
    discovery_method: Mapped[str] = mapped_column(String(50))  # scraped | pattern | inferred
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    verify_status: Mapped[str] = mapped_column(String(20), default="unknown")
    verified_at: Mapped[datetime | None] = mapped_column(DateTime)
    suppressed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    company: Mapped[Company] = relationship(back_populates="contacts")


class Outreach(Base):
    """A drafted message. There is no send path: status starts and stays 'draft'
    until a human sends it from their own mail client. See docs/compliance.md."""

    __tablename__ = "outreach"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"))
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"))
    subject: Mapped[str] = mapped_column(String(500))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft|sent|replied
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Suppression(Base):
    """Addresses that must never be (re)discovered, stored as a hash.

    Storing the hash rather than the address is what makes an erasure request
    genuinely honourable: suppressing a contact must not require retaining the
    personal data the request asked you to delete.
    """

    __tablename__ = "suppressions"

    id: Mapped[int] = mapped_column(primary_key=True)
    email_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    jobs_seen: Mapped[int] = mapped_column(Integer, default=0)
    jobs_new: Mapped[int] = mapped_column(Integer, default=0)
    contacts_found: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[list | None] = mapped_column(JSON)


# --------------------------------------------------------------------------- #
# Transport object: what source adapters return before normalization.
# --------------------------------------------------------------------------- #

_WS = re.compile(r"\s+")
_ZERO_WIDTH = re.compile(r"[​‌‍﻿]")
_GENDER_TAG = re.compile(r"\b(m/f/d|m/w/d|f/m/d|w/m/d|d/f/m|d/m/w|h/f|m/w|m/f)\b")
_BRACKETED = re.compile(r"[(\[]([^)\]]*)[)\]]")
# Segment separators inside a title. Deliberately excludes "/": it appears
# mid-word ("AM/ Manager") far more often than it separates a location.
_SEGMENTS = re.compile(r"\s+[-–—|]\s+|\s*[,;]\s+")
_LOCATION_WORDS = ("remote", "hybrid", "onsite", "on-site", "work from home", "wfh", "anywhere")

# Cities and regions that show up as a trailing title segment. Not exhaustive and
# never will be — the point is to catch the common cases so that a *role*
# qualifier ("- Platform Team") is not mistaken for a location and discarded.
# Weighted toward India because that is where the configured targets hire.
_GEO = frozenset(
    """bengaluru bangalore blr mumbai bombay pune delhi ncr gurgaon gurugram noida hyderabad
    chennai kolkata ahmedabad jaipur kochi cochin coimbatore indore chandigarh trivandrum
    thiruvananthapuram mysore mysuru nagpur bhubaneswar vizag visakhapatnam india
    berlin london manchester edinburgh dublin amsterdam rotterdam paris munich hamburg
    frankfurt cologne zurich geneva barcelona madrid lisbon porto warsaw krakow stockholm
    copenhagen oslo helsinki milan rome vienna prague budapest bucharest athens istanbul
    york nyc sf seattle austin boston chicago denver atlanta dallas houston philadelphia
    phoenix portland miami toronto vancouver montreal ottawa
    singapore tokyo osaka seoul beijing shanghai shenzhen taipei sydney melbourne brisbane
    perth auckland wellington dubai doha riyadh cairo lagos nairobi johannesburg
    usa us america canada uk england scotland ireland germany france spain italy portugal
    netherlands belgium poland sweden norway denmark finland switzerland austria greece
    australia japan china korea brazil mexico argentina israel uae singapore
    emea apac amer latam noram europe asia global worldwide onsite""".split()
) | {
    "new york", "san francisco", "bay area", "los angeles", "san diego", "new delhi",
    "hong kong", "tel aviv", "abu dhabi", "sao paulo", "mexico city", "cape town",
    "united states", "united kingdom", "south korea", "new zealand", "south africa",
    "north america", "south america", "latin america", "middle east", "greater noida",
}

_SENIORITY = [
    ("intern", ("intern", "internship", "trainee")),
    ("junior", ("junior", "jr.", "entry level", "entry-level", "graduate", "new grad")),
    ("staff", ("staff", "principal", "distinguished", "fellow")),
    ("lead", ("lead", "manager", "head of", "director", "vp ", "chief")),
    ("senior", ("senior", "sr.", "sr ")),
    ("mid", ("engineer ii", "engineer 2", "mid-level")),
]
_REMOTE = ("remote", "work from home", "wfh", "distributed", "anywhere")


class RawJob(BaseModel):
    """Source adapters emit these; the normalizer turns them into Job rows."""

    source: str
    external_id: str | None = None
    title: str
    location: str | None = None
    description: str | None = None
    url: str
    posted_at: datetime | None = None
    company_name: str | None = None

    # Several ATSs expose remote and seniority as real fields rather than
    # something to infer from the title (Ashby's `isRemote`, Lever's
    # `workplaceType`, SmartRecruiters' `experienceLevel`). docs/sources.md calls
    # those authoritative, so adapters need a channel to pass them through.
    # None means "this source gave no signal" — fall back to text inference.
    remote: bool | None = None
    seniority_hint: str | None = None

    def _is_location_like(self, segment: str) -> bool:
        """Is this title segment a place rather than part of the role?

        The distinction matters more than it looks. Dropping every trailing
        segment collapses "Engineering Manager - Platform Team" and
        "Engineering Manager - Pune" into one hash, so one of two real openings
        silently disappears. Measured against live boards, being indiscriminate
        here lost 12% of postings. When in doubt this returns False: a duplicate
        row is visible and harmless, a dropped job is invisible and costly.
        """
        seg = segment.strip().strip(".-–—|:").strip()
        if not seg or len(seg) > 40:
            return False
        if any(w in seg for w in _LOCATION_WORDS):
            return True
        if seg in _GEO:
            return True
        tokens = [t for t in re.split(r"[\s,/]+", seg) if t]
        if tokens and all(t in _GEO for t in tokens):
            return True
        # The job's own location field is the most reliable signal available:
        # "- Bangalore" on a job located in "Bangalore, Karnataka" is redundant.
        location = _ZERO_WIDTH.sub("", (self.location or "").lower())
        return bool(location) and len(seg) > 2 and seg in location

    def canonical_title(self) -> str:
        """Strip the noise companies add so the same role dedupes across sources.

        Removes gendered job-ad tags, location qualifiers and bracketed places,
        while *keeping* anything that distinguishes one role from another.
        """
        t = _ZERO_WIDTH.sub("", self.title.lower())
        t = _GENDER_TAG.sub(" ", t)
        # "(Remote)" and "[US]" go; "(Models)" and "(4+ YOE)" stay, because they
        # are what tells two otherwise identically-titled openings apart.
        t = _BRACKETED.sub(
            lambda m: " " if self._is_location_like(m.group(1)) else f" {m.group(1)} ", t
        )
        segments = _SEGMENTS.split(t)
        kept = [segments[0]] + [s for s in segments[1:] if not self._is_location_like(s)]
        t = " ".join(kept)
        t = re.sub(r"[^a-z0-9+#\s]", " ", t)
        return _WS.sub(" ", t).strip()

    def detect_seniority(self) -> str | None:
        """A source-provided level beats parsing the title; fall back to needles."""
        for hay in (self.seniority_hint, self.title):
            if not hay:
                continue
            hay = hay.lower()
            for level, needles in _SENIORITY:
                if any(n in hay for n in needles):
                    return level
        return None

    def detect_remote(self) -> bool:
        if self.remote is not None:
            return self.remote
        hay = f"{self.title} {self.location or ''}".lower()
        return any(n in hay for n in _REMOTE)

    def _title_location_hints(self) -> list[str]:
        """Places named in the title that the location field does not already cover.

        Greenhouse hands back both "Engineering Manager - Bangalore" and
        "Engineering Manager - Pune" with ``location.name == "Bangalore"``. Once
        canonical_title has stripped the city, nothing distinguishes them and one
        of two real openings vanishes. Keeping the surplus city out of
        canonical_title (so the role reads cleanly) but inside the hash resolves
        that without reintroducing the noise.
        """
        location = _ZERO_WIDTH.sub("", (self.location or "").lower())
        t = _GENDER_TAG.sub(" ", _ZERO_WIDTH.sub("", self.title.lower()))
        candidates = [m.group(1) for m in _BRACKETED.finditer(t)]
        candidates += _SEGMENTS.split(_BRACKETED.sub(" ", t))[1:]

        hints: set[str] = set()
        for segment in candidates:
            seg = segment.strip().strip(".-–—|:").strip()
            if not seg or not self._is_location_like(seg):
                continue
            if any(w in seg for w in _LOCATION_WORDS):
                continue  # remote/hybrid is carried by detect_remote(), not the hash
            if seg in location:
                continue  # redundant with the location field
            hints.add(seg)
        return sorted(hints)

    def compute_hash(self, company_name: str) -> str:
        """Dedup key. Deliberately excludes source and external_id so the same role
        posted on an ATS board and the company's own page collapses into one row."""
        parts = [
            company_name.strip().lower(),
            self.canonical_title(),
            _WS.sub(" ", (self.location or "").lower()).strip(),
            ",".join(self._title_location_hints()),
        ]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()
