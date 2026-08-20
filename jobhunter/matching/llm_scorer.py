"""Ask a model the one question keyword matching cannot answer.

The keyword scorer in ``scorer.py`` is deterministic, free and offline, and it
stays the default for all three reasons. What it cannot do is tell "this is a
machine-learning role" from "this posting contains the word Python". On the
current database that costs both ways: `AI Sales Engineer` and `Strategic
Finance Analyst` reach the shortlist on a stray title-word overlap plus full
must-have credit, while epiFi's `DS / ML Engineer` scores 40/40 on title and
then loses 25 points because its description never writes "python".

That is a judgement, not a lookup, so this module asks a model for it — and
stores the answer *beside* the keyword score rather than replacing it, so the
two stay comparable and a bad prompt cannot destroy existing data.

Opt-in, exactly like ``verify_emails``. Nothing here runs unless ``--llm`` is
passed, the package extra is installed and a key is configured; the pipeline
must work fully without any of the three.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from pydantic import BaseModel, Field

from ..config import Profile

log = logging.getLogger(__name__)

# Opus by default. This is a judgement call about a person's career prospects,
# and the whole reason the module exists is that a cheaper mechanism — keyword
# matching — gets it wrong in both directions. Downgrading to save a few rupees
# risks reintroducing exactly the false positives this is meant to remove.
# Override with `--model` when the volume justifies it.
DEFAULT_MODEL = "claude-opus-5"

# Enough to reach the requirements section of a long posting without paying for
# boilerplate about company values and benefits.
DESCRIPTION_CHARS = 6000

# A verdict is a handful of fields; the reasoning is one sentence.
MAX_TOKENS = 400

SYSTEM = """\
You screen job postings for a specific candidate. Judge only what the posting \
says — never invent requirements it does not state.

The candidate:
- {experience}
- Target roles: {titles}
- Skills: {skills}
- Can work in: {locations}

For the posting given, decide:

is_target_role — is this genuinely a machine-learning, data-science, AI or data
analytics role? A sales, marketing, finance, support, operations or
customer-facing role is NOT a target role even when it mentions AI, Python or
data in passing. Judge the actual work, not the vocabulary.

is_junior_appropriate — could someone with the experience above realistically be
hired for this? Say false when the posting requires materially more experience,
names a senior/staff/lead/principal level, or expects the candidate to lead
others. An unstated experience level is usually fine for a junior.

fit — 0 to 100. Reserve 80+ for postings that are both a target role and junior
appropriate AND overlap the candidate's actual skills. A posting failing either
gate should score below 30 regardless of how well the skills match.

reason — one sentence, concrete, citing what in the posting decided it.\
"""

USER = """\
Company: {company}
Title: {title}
Location: {location}

Description:
{description}\
"""


class Verdict(BaseModel):
    """A model's judgement on one posting."""

    is_target_role: bool = Field(description="Genuinely an ML/data/AI role, not merely AI-adjacent")
    is_junior_appropriate: bool = Field(description="Reachable at the candidate's experience level")
    fit: int = Field(ge=0, le=100, description="Overall fit, 0-100")
    reason: str = Field(description="One concrete sentence citing the posting")


@dataclass
class Scored:
    job_id: int
    verdict: Verdict | None
    error: str | None = None


def _profile_summary(profile: Profile) -> dict[str, str]:
    years = profile.max_years_experience
    experience = (
        f"At most {years} year{'s' if years != 1 else ''} of professional experience"
        if years is not None
        else "Early career"
    )
    return {
        "experience": experience,
        "titles": ", ".join(profile.titles) or "any",
        "skills": ", ".join(profile.nice_to_have_keywords[:25]) or "unspecified",
        "locations": ", ".join(profile.locations) or "anywhere",
    }


def build_messages(job: dict, profile: Profile) -> tuple[str, str]:
    """The system and user prompt for one posting. Separated out to be testable."""
    description = (job.get("description") or "").strip()
    if len(description) > DESCRIPTION_CHARS:
        description = description[:DESCRIPTION_CHARS] + "\n[truncated]"
    return (
        SYSTEM.format(**_profile_summary(profile)),
        USER.format(
            company=job.get("company") or "unknown",
            title=job.get("title") or "",
            location=job.get("location") or "unstated",
            description=description or "(no description provided)",
        ),
    )


async def score_one(client, job: dict, profile: Profile, *, model: str = DEFAULT_MODEL) -> Scored:
    """Score a single posting. Never raises — a failure is data, not an abort.

    One bad posting among thousands must not end the run, the same rule the
    adapters follow. The caller records the error and moves on.
    """
    system, user = build_messages(job, profile)
    try:
        response = await client.messages.parse(
            model=model,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format=Verdict,
            # A screening judgement, not a research task: thinking stays on
            # (disabling it on Opus 5 risks tool-call and tag leakage) but at
            # the cheapest depth.
            output_config={"effort": "low"},
        )
    except Exception as exc:  # noqa: BLE001 - provider errors are many and all non-fatal here
        log.warning("llm scoring failed for job %s: %s", job.get("id"), exc)
        return Scored(job_id=job["id"], verdict=None, error=type(exc).__name__)

    parsed = response.parsed_output
    if parsed is None:
        return Scored(job_id=job["id"], verdict=None, error="unparseable")
    return Scored(job_id=job["id"], verdict=parsed)


async def score_many(
    jobs: list[dict],
    profile: Profile,
    *,
    model: str = DEFAULT_MODEL,
    concurrency: int = 6,
    client=None,
    on_progress=None,
) -> list[Scored]:
    """Score many postings concurrently, bounded so a burst cannot trip limits.

    The SDK already retries 429s and 5xx with backoff, so the semaphore here is
    about not generating the burst in the first place.
    """
    if not jobs:
        return []
    owns_client = client is None
    if owns_client:
        client = build_client()

    semaphore = asyncio.Semaphore(concurrency)

    async def one(job: dict) -> Scored:
        async with semaphore:
            result = await score_one(client, job, profile, model=model)
        if on_progress:
            on_progress(result)
        return result

    try:
        return await asyncio.gather(*(one(job) for job in jobs))
    finally:
        if owns_client and hasattr(client, "close"):
            await client.close()


def build_client():
    """The async Anthropic client, imported lazily.

    Lazy so that importing this module — which ``cli.py`` does unconditionally —
    never requires the optional extra. Only actually using ``--llm`` does.
    """
    try:
        from anthropic import AsyncAnthropic
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise RuntimeError(
            "LLM scoring needs the `llm` extra: uv sync --extra llm"
        ) from exc
    return AsyncAnthropic()
