"""Build one application email from one (job, contact) pair, or refuse to.

There is no LLM here. The provider decision is parked, and it turns out the
specifics needed to write a non-generic message are already in the database:
the scorer knows which of the profile's skills *this* posting asks for, because
it matched them to compute the score. Reusing that match is both cheaper than a
model call and impossible to hallucinate.

`docs/compliance.md` says a draft that would read identically with the company
name swapped is a bug. That is enforced here rather than hoped for: a posting
we cannot say at least MIN_SPECIFIC_SKILLS concrete things about does not get
an email at all. Refusing to write is the correct output for a job we know
nothing about — the alternative is a mail merge, which is the thing this
project exists not to be.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from ..config import Applicant, Profile
from ..matching.scorer import _contains_word  # intra-package; see module docstring
from ..models import Company, Contact, Job

log = logging.getLogger(__name__)

# Below this many matched skills the message would be a form letter, so the
# drafter declines. Two is the floor at which the "your posting asks for X and
# Y" sentence names something real rather than gesturing.
MIN_SPECIFIC_SKILLS = 2

# The applicant's side of the same bar. A draft with no outcomes in it falls
# back to asserting competence, which is what every other application does.
MIN_ACHIEVEMENTS = 1

# How many skills to name in the body. Listing twelve reads like keyword
# stuffing and signals a script louder than saying nothing would.
MAX_SKILLS_NAMED = 3

# Profile keywords are stored lowercase for matching. Writing them back out that
# way produces "llm and lora", which reads like a script wrote it — because one
# did. Only acronyms and proper nouns need listing here; anything absent is left
# exactly as the profile spells it, which is already correct for ordinary words
# like "machine learning".
SKILL_CASING = {
    "python": "Python",
    "java": "Java",
    "scala": "Scala",
    "spark": "Spark",
    "llm": "LLM",
    "llms": "LLMs",
    "nlp": "NLP",
    "rag": "RAG",
    "lora": "LoRA",
    "sql": "SQL",
    "gcp": "GCP",
    "aws": "AWS",
    "cnn": "CNNs",
    "gan": "GANs",
    "mlops": "MLOps",
    "a/b testing": "A/B testing",
    "pytorch": "PyTorch",
    "tensorflow": "TensorFlow",
    "scikit-learn": "scikit-learn",
    "sklearn": "scikit-learn",
    "xgboost": "XGBoost",
    "opencv": "OpenCV",
    "yolo": "YOLO",
    "faiss": "FAISS",
    "bigquery": "BigQuery",
    "vertex ai": "Vertex AI",
    "mongodb": "MongoDB",
    "docker": "Docker",
    "keras": "Keras",
    "langchain": "LangChain",
    "numpy": "NumPy",
    "pandas": "pandas",
    "jupyter": "Jupyter",
    "django": "Django",
    "flask": "Flask",
    "machine learning": "machine learning",
    "deep learning": "deep learning",
    "computer vision": "computer vision",
    "vector database": "vector databases",
}

# Skills too generic to be worth naming. "The posting asks for evaluation" says
# nothing, and it crowds out the concrete technology sitting behind it in the
# list. Still counted toward MIN_SPECIFIC_SKILLS — they are real matches — but
# named last if at all.
VAGUE_SKILLS = frozenset(
    {
        "evaluation",
        "statistics",
        "experimentation",
        "classifier",
        "embeddings",
        "recommendation",
        "collaborative filtering",
        "data pipeline",
        "fine-tuning",
        "prompt engineering",
    }
)


@dataclass
class Draft:
    """A message ready to send."""

    subject: str
    body: str
    skills: list[str] = field(default_factory=list)


@dataclass
class Refusal:
    """Why no message was written. Surfaced, never swallowed."""

    reason: str


def matched_skills(job: Job, profile: Profile) -> list[str]:
    """Profile skills this posting actually asks for, in profile order.

    Matched against title and description together, whole-word, sharing
    `scorer._contains_word` so the drafter can never claim a skill the scorer
    did not credit. Deliberately recomputed rather than parsed back out of
    `fit_reasons`: those strings are prose written for humans and will drift.

    Must-have alternatives count too. Over all 1,307 open postings scoring 55+,
    nice-to-haves alone leave 598 (45%) with enough to say; including must-haves
    takes it to 725 (55%), recovering 127 postings. A posting that says "Python"
    and nothing else has still told us one true, specific thing.

    What the refused 45% have in common is length, not quality. Draftability
    tracks the description almost exactly: every one of the 34 postings scoring
    90+ is draftable and their median description is 4,852 characters, while the
    70-89 band is 55% draftable at a median of 950. That band is full of stubs
    because that is precisely what the scorer's unjudgeable-must-have rule
    promotes into it, so the two rules pull against each other by design: the
    scorer declines to punish a posting for saying nothing, and the drafter
    declines to write to one.

    Nice-to-haves come first because they discriminate: every ML posting wants
    Python, far fewer want LoRA.
    """
    haystack = f"{job.title or ''}\n{job.description or ''}"
    seen: list[str] = []

    def collect(keyword: str) -> None:
        keyword = keyword.strip()
        if keyword and keyword not in seen and _contains_word(haystack, keyword):
            seen.append(keyword)

    for keyword in profile.nice_to_have_keywords:
        collect(keyword)
    for group in profile.must_have_keywords:
        # A group is pipe-separated alternatives; name the one that matched.
        for alternative in group.split("|"):
            collect(alternative)
    return seen


def _greeting(contact: Contact) -> str:
    first = (contact.first_name or "").strip()
    # A role address has no person behind it, so "Hi careers" would be absurd.
    return first if first and contact.kind != "role" else "there"


def _humanise(skills: list[str]) -> str:
    """['a', 'b', 'c'] -> 'a, b and c'."""
    if len(skills) == 1:
        return skills[0]
    return f"{', '.join(skills[:-1])} and {skills[-1]}"


def _present(skill: str) -> str:
    """Write a stored keyword the way a person would type it.

    Unmapped keywords are left exactly as the profile spells them. Title-casing
    them instead produced "machine learning, Recommendation and Evaluation" --
    ordinary words capitalised mid-sentence, which reads more like a script than
    the lowercase acronyms the mapping was added to fix.
    """
    return SKILL_CASING.get(skill.lower(), skill)


def _worth_naming(skills: list[str]) -> list[str]:
    """Concrete technologies first, vague ones only to fill the quota."""
    concrete = [s for s in skills if s.lower() not in VAGUE_SKILLS]
    vague = [s for s in skills if s.lower() in VAGUE_SKILLS]
    return (concrete + vague)[:MAX_SKILLS_NAMED]


# Job locations arrive as whatever the ATS stored: "bengaluru, in", or a
# semicolon-joined list of four offices. Dropped straight into a sentence they
# read like a database dump, which is exactly the tell this drafter exists to
# avoid.
def _location_phrase(job) -> str:
    raw = (job.location or "").strip()
    if not raw:
        return ""
    first = re.split(r"[;/|]", raw)[0].strip()
    # "Bengaluru, Karnataka, India" -> "Bengaluru". The city is the part a
    # human would say; the rest is administrative.
    city = first.split(",")[0].strip(" -")
    if not city or len(city) > 40:
        return ""
    if city.islower() or city.isupper():
        city = city.title()
    return f" in {city}"


def _signature(applicant: Applicant, *, omit: str = "") -> str:
    """Name, then one line of contact details separated by middots.

    The email address is not repeated here: it is already the From header, and
    a signature block four lines deep reads like a letterhead. `omit` drops a
    link the body already named, so the portfolio URL appears once. If nothing
    is left to show, the address goes back in rather than leaving no reply path.
    """
    bits = [applicant.phone.strip(), *(link.strip() for link in applicant.links)]
    bits = [_bare_url(b) for b in bits if b]
    if omit:
        bits = [b for b in bits if b != omit]
    if not bits:
        bits = [applicant.email.strip()]
    contact = " · ".join(b for b in bits if b)
    return f"{applicant.name.strip()}\n{contact}" if contact else applicant.name.strip()


def _bare_url(value: str) -> str:
    """`https://github.com/x/` -> `github.com/x`. Schemes are noise to a reader."""
    return re.sub(r"^https?://(www\.)?", "", value).rstrip("/")


# Gmail and Outlook both cut the subject around here. Past it the headline --
# the evidence that earns the open -- is what disappears, so the role title
# gives up its qualifier first.
SUBJECT_MAX = 72


def _subject_title(title: str) -> str:
    """The role title, shortened only if the full one would not survive display.

    "Data Scientist - Online Ads / Bidding Marketplaces" becomes "Data
    Scientist". The qualifier is dropped rather than the headline because a
    recruiter already knows which roles they posted; what they cannot see from
    the inbox list is whether this one is worth opening.
    """
    lead = re.split(r"\s+[-–—]\s+|\s*[(\[]", title, maxsplit=1)[0].strip(" ,-–—")
    return lead or title


def _portfolio(applicant: Applicant) -> str:
    """The link most worth naming in the body, if there is one.

    GitHub first: for this kind of role it is the one link a reader might
    actually open. Everything else stays in the signature.
    """
    for link in applicant.links:
        if "github.com" in link.lower():
            return _bare_url(link.strip())
    return ""


def _background(applicant: Applicant) -> str:
    """Degree, base and availability as one sentence, skipping whatever is unset."""
    where = applicant.location.strip()
    when = applicant.availability.strip()
    parts = [p for p in (applicant.education.strip(),) if p]
    if where and when:
        parts.append(f"Based in {where}, {when}")
    elif where:
        parts.append(f"Based in {where}")
    elif when:
        parts.append(when[0].upper() + when[1:])
    return ". ".join(parts) + "." if parts else ""


def draft_for(job: Job, contact: Contact, company: Company, profile: Profile) -> Draft | Refusal:
    """Write the message, or explain why this job does not get one."""
    applicant = profile.applicant
    missing = applicant.is_complete()
    if missing:
        return Refusal(f"applicant profile incomplete: missing {', '.join(missing)}")

    skills = matched_skills(job, profile)
    if len(skills) < MIN_SPECIFIC_SKILLS:
        # Not a failure to be retried — there is genuinely nothing specific to
        # say about this posting, and a generic note is worse than silence.
        return Refusal(
            f"only {len(skills)} profile skill(s) found in the posting; "
            f"need {MIN_SPECIFIC_SKILLS} to write something specific"
        )

    if len(applicant.achievements) < MIN_ACHIEVEMENTS:
        # Same principle as the skills floor above, pointed at our own side of
        # the message: an application with no outcomes in it is an assertion,
        # and sending one is worse than sending nothing.
        return Refusal(
            "applicant profile lists no achievements; a message with no evidence "
            "in it is not worth sending"
        )

    named = [_present(s) for s in _worth_naming(skills)]
    title = (job.title or "").strip()
    employer = (company.name or "").strip()
    where = _location_phrase(job)

    headline = applicant.headline.strip()
    if headline:
        subject = f"{title} — {headline}"
        if len(subject) > SUBJECT_MAX:
            subject = f"{_subject_title(title)} — {headline}"
    else:
        subject = f"Application: {title} — {applicant.name.strip()}"

    # Evidence first, because that is what gets read. The opening names this
    # posting specifically so the message cannot be a merge field, the middle
    # is outcomes with numbers, and the ask is a question -- "are you the right
    # person, or who is?" is far easier to answer than "please consider me",
    # and earns a forward even when the answer is no.
    evidence = "\n\n".join(a.strip() for a in applicant.achievements if a.strip())
    background = _background(applicant)
    portfolio = _portfolio(applicant)
    resume_line = (
        f"Resume attached, and my work is at {portfolio}." if portfolio else "Resume attached."
    )

    paragraphs = [
        f"Hi {_greeting(contact)},",
        f"I came across the {title} opening at {employer}{where}, and I'd like to be "
        f"considered for it. Your posting asks for {_humanise(named)}, which is what "
        f"I have been doing.",
        "Quick version of what I've done:",
        evidence,
        background,
        resume_line,
        "Would you be the right person to speak to about this, or could you point me "
        "to whoever is?",
        f"The posting I am referring to: {job.url}",
        "If you would rather I did not follow up, reply and say so and I will not.",
        f"Thanks for your time,\n{_signature(applicant, omit=portfolio)}",
    ]
    body = "\n\n".join(p for p in paragraphs if p) + "\n"

    problem = preflight(subject, body)
    if problem:
        return Refusal(problem)
    return Draft(subject=subject, body=body, skills=named)


# An f-string that lost its value leaves a literal brace behind, and a template
# bug shipping unattended to real HR inboxes is not recoverable. Cheap to check.
_UNFILLED = re.compile(r"[{}]")


def preflight(subject: str, body: str) -> str | None:
    """Reasons this message must not be sent, or None. Checked before every send."""
    if not subject.strip():
        return "empty subject"
    if not body.strip():
        return "empty body"
    if _UNFILLED.search(subject) or _UNFILLED.search(body):
        return "message still contains an unfilled placeholder"
    if "\n\n" not in body:
        return "body has no paragraph break; template probably collapsed"
    if len(body) < 200:
        return f"body is only {len(body)} chars; template probably collapsed"
    return None


# How many of a company's open roles to name in a speculative note. Two or three
# shows you looked; listing eight reads like a scrape, which it would be.
MAX_ROLES_NAMED = 3


def _role_list(evidence: list[Job]) -> list[str]:
    """Distinct open role titles, shortest first so the sentence stays readable."""
    seen: list[str] = []
    for job in evidence:
        title = (job.title or "").strip()
        # Titles carry trailing requisition noise: "Data Engineer (R4633)".
        title = re.sub(r"\s*[\(\[][^)\]]*[\)\]]\s*$", "", title).strip(" -–—,")
        if title and title.lower() not in {s.lower() for s in seen}:
            seen.append(title)
    return sorted(seen, key=len)[:MAX_ROLES_NAMED]


def draft_speculative(
    contact: Contact, company: Company, evidence: list[Job], profile: Profile
) -> Draft | Refusal:
    """A note to a company that is hiring, but not for anything that matches.

    Deliberately not the application template with a word changed. That message
    says "I'd like to apply for the X role", and sending it when no such role
    exists would imply a posting that was never there -- which `compliance.md`
    rules out under "no implying a prior conversation". This one says plainly
    that it is speculative, in its subject line as well as its first sentence.

    What keeps it specific is the company's own open postings: the roles they
    are actually advertising, and the skills those postings ask for. Both are
    facts from the database, so the message cannot claim something untrue about
    a company, and it cannot be sent unchanged to a different one.
    """
    applicant = profile.applicant
    missing = applicant.is_complete()
    if missing:
        return Refusal(f"applicant profile incomplete: missing {', '.join(missing)}")
    if not evidence:
        # No open postings means no published hiring intent, which is the whole
        # lawful basis for writing. Refusing here is not caution, it is the rule.
        return Refusal(f"{company.name} has no open postings to write about")

    skills: list[str] = []
    for job in evidence:
        for skill in matched_skills(job, profile):
            if skill not in skills:
                skills.append(skill)
    if len(skills) < MIN_SPECIFIC_SKILLS:
        return Refusal(
            f"{company.name}'s open postings mention only {len(skills)} profile skill(s); "
            f"need {MIN_SPECIFIC_SKILLS} to say anything true and specific"
        )

    roles = _role_list(evidence)
    if not roles:
        return Refusal(f"{company.name} has no nameable open role titles")

    named = [_present(s) for s in _worth_naming(skills)]
    employer = (company.name or "").strip()
    wanted = (profile.titles[0].strip() if profile.titles else "").strip()
    subject = (
        f"Speculative application: {wanted} — {applicant.name.strip()}"
        if wanted
        else f"Speculative application — {applicant.name.strip()}"
    )

    body = f"""Hi {_greeting(contact)},

I could not find an opening at {employer} that matches what I do, so this is a speculative note rather than an application.

You are currently hiring for {_humanise(roles)}, and {"that posting asks" if len(roles) == 1 else "those postings ask"} for {_humanise(named)}. That is the work I have been doing, and the detail is in the CV attached.

If something closer to my background opens up, I would be glad to hear about it.

If you would rather I did not follow up, reply and say so and I will not.

Best,
{_signature(applicant)}
"""

    problem = preflight(subject, body)
    if problem:
        return Refusal(problem)
    return Draft(subject=subject, body=body, skills=named)
