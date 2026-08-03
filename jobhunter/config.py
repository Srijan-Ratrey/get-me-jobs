"""Configuration: settings from env, targets and profile from YAML."""
from __future__ import annotations

import csv
import io
import re
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime knobs. Override any of these via .env or environment variables."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="JOBHUNTER_", extra="ignore")

    db_url: str = "sqlite+pysqlite:///jobhunter.db"
    cache_dir: Path = Path(".cache")

    # Politeness. Lower these at your own risk; they are what keep you unblocked.
    requests_per_second: float = 1.0
    max_concurrency: int = 5
    request_timeout: float = 20.0
    max_retries: int = 3
    respect_robots: bool = True
    cache_ttl_seconds: int = 6 * 3600
    # Base of the exponential backoff between retries. Configurable so tests can
    # exercise the retry path without actually sleeping 2 + 4 + 8 seconds.
    retry_backoff_base: float = 2.0

    # Identify yourself. A real contact URL here dramatically reduces the odds of a block.
    user_agent: str = (
        "JobHunterBot/0.1 (personal job search; +https://example.com/about-this-bot)"
    )

    # SMTP verification. Off by default: it is optional enrichment, and careless
    # probing gets your IP blocklisted.
    verify_emails: bool = False
    smtp_helo_host: str = "example.com"
    smtp_mail_from: str = "verify@example.com"
    smtp_timeout: float = 10.0
    smtp_delay_seconds: float = 3.0

    # Contact discovery
    enable_pattern_guessing: bool = True
    max_guesses_per_domain: int = 6


class Target(BaseModel):
    """One company to watch."""

    name: str
    domain: str | None = None
    ats: str | None = None  # greenhouse | lever | ashby | workable | smartrecruiters
    ats_token: str | None = None
    careers_url: str | None = None
    contact_pages: list[str] = Field(default_factory=list)


class Profile(BaseModel):
    """What you are looking for. Drives fit scoring."""

    titles: list[str] = Field(default_factory=list)
    must_have_keywords: list[str] = Field(default_factory=list)
    nice_to_have_keywords: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    remote_only: bool = False
    seniority: list[str] = Field(default_factory=list)
    min_score: int = 40

    # A posting demanding more experience than this is a senior role regardless
    # of what its title says, and is scored as a hard zero. None disables the
    # check. See matching/scorer.py for why the *smallest* stated requirement in
    # a description is the one that counts.
    max_years_experience: int | None = None


def load_targets(path: str | Path) -> list[Target]:
    data = yaml.safe_load(Path(path).read_text()) or {}
    # `companies:` with nothing under it parses to None rather than [], and a
    # bare key is an easy state to leave the file in by hand.
    return [Target(**t) for t in (data.get("companies") or [])]


def load_profile(path: str | Path) -> Profile:
    data = yaml.safe_load(Path(path).read_text()) or {}
    return Profile(**(data.get("profile") or data))


# --------------------------------------------------------------------------- #
# Importing a company list from a spreadsheet
# --------------------------------------------------------------------------- #

# Header aliases, matched after normalising to lowercase alphanumerics. Real
# spreadsheets say "Career page", "Careers URL", "careers_url" and mean the same.
_NAME_HEADERS = ("company", "companyname", "name", "employer", "organisation", "organization")
_URL_HEADERS = ("careerpage", "careerspage", "careerurl", "careersurl", "careers", "career",
                "jobspage", "jobsurl", "url", "link", "website")
_DOMAIN_HEADERS = ("domain", "companydomain", "site", "homepage")


def _normalise_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def _pick_column(headers: dict[str, str], candidates: Sequence[str]) -> str | None:
    """First header whose normalised form matches a candidate, in candidate order."""
    for candidate in candidates:
        if candidate in headers:
            return headers[candidate]
    return None


# Hosts that belong to an ATS vendor rather than to an employer. Deliberately
# duplicated at this layer rather than imported from sources.careers_page, which
# depends on this module. Kept short: it only needs the hosts a careers-page URL
# might plausibly point at.
ATS_HOSTS = (
    "greenhouse.io", "lever.co", "ashbyhq.com", "workable.com", "smartrecruiters.com",
    "recruitee.com", "personio.de", "personio.com", "teamtailor.com", "bamboohr.com",
    "myworkdayjobs.com", "workday.com", "keka.com", "darwinbox.in", "darwinbox.com",
    "zohorecruit.com", "freshteam.com", "icims.com", "successfactors.com", "jobvite.com",
    "recruiterbox.com", "turbohire.co", "instahyre.com", "hirist.com",
    # Aggregators and marketplaces: also not the employer, and scraping them for
    # a company's hiring address finds the aggregator's own contact details.
    "wellfound.com", "angel.co", "ycombinator.com", "trakstar.com", "lever.co",
)


def domain_from_url(url: str | None) -> str | None:
    """Company domain from a careers-page URL, or None if it is an ATS host.

    A surprising number of companies list an ATS URL as their careers page, so
    checking the input is not enough — the result has to be checked too. Getting
    this wrong points contact discovery at the ATS vendor: it would scrape
    ``apply.workable.com/careers`` looking for the employer's hiring address,
    and no domain at all is better than the wrong one.
    """
    if not url:
        return None
    host = urlparse(url.strip()).netloc.lower()
    host = host.split("@")[-1].split(":")[0]  # strip credentials and port
    host = host.removeprefix("www.")
    if not host:
        return None
    if any(host == ats or host.endswith(f".{ats}") for ats in ATS_HOSTS):
        return None
    return host


def load_company_csv(path: str | Path) -> tuple[list[Target], list[str]]:
    """Read a company list from CSV. Returns (targets, skipped_reasons).

    Skips are returned rather than logged away: a spreadsheet row that silently
    vanishes is a company you think you are watching and are not.
    """
    text = Path(path).read_text(encoding="utf-8-sig")  # tolerate a BOM
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError(f"{path} has no header row")

    headers = {_normalise_header(h): h for h in reader.fieldnames if h}
    name_col = _pick_column(headers, _NAME_HEADERS)
    url_col = _pick_column(headers, _URL_HEADERS)
    domain_col = _pick_column(headers, _DOMAIN_HEADERS)
    if not name_col:
        raise ValueError(f"{path}: no company-name column found in {reader.fieldnames}")
    if not url_col:
        raise ValueError(f"{path}: no careers-URL column found in {reader.fieldnames}")

    targets: list[Target] = []
    skipped: list[str] = []
    seen: set[str] = set()
    for line, row in enumerate(reader, start=2):
        name = (row.get(name_col) or "").strip()
        url = (row.get(url_col) or "").strip()
        if not name and not url:
            continue  # a blank spacer row is not worth reporting
        if not name:
            skipped.append(f"line {line}: no company name")
            continue
        if not url.lower().startswith(("http://", "https://")):
            skipped.append(f"{name}: careers URL is not http(s) ({url!r})")
            continue
        key = name.lower()
        if key in seen:
            skipped.append(f"{name}: duplicate row in the CSV")
            continue
        seen.add(key)
        targets.append(
            Target(
                name=name,
                careers_url=url,
                domain=(row.get(domain_col) or "").strip().lower() or domain_from_url(url)
                if domain_col
                else domain_from_url(url),
            )
        )
    return targets, skipped


_BLOCK_KEY = re.compile(r"^companies:[ \t]*$", re.MULTILINE)
_ANY_KEY = re.compile(r"^companies:[ \t]*(.*)$", re.MULTILINE)


def append_targets(path: str | Path, targets: list[Target], *, note: str = "") -> int:
    """Append targets to a companies YAML, skipping names already present.

    Appends as text rather than re-dumping the parsed YAML, because a safe_dump
    round-trip discards every comment in the file — and those comments carry the
    provenance of the hand-verified entries. Appending sequence items after a
    trailing comment block is fine: comments do not end a YAML sequence.

    Two things this has to be careful about. A ``companies: []`` flow-style value
    cannot take appended block items at all, so the key is rewritten to block form
    first. And because this edits a file the user curates by hand, the result is
    re-parsed before being kept — a corrupted companies.yaml would be a genuinely
    costly thing to do silently.
    """
    target_path = Path(path)
    original = target_path.read_text(encoding="utf-8") if target_path.exists() else ""
    existing = {t.name.strip().lower() for t in load_targets(target_path)} if original else set()
    fresh = [t for t in targets if t.name.strip().lower() not in existing]
    if not fresh:
        return 0

    body = original
    if not _BLOCK_KEY.search(body):
        match = _ANY_KEY.search(body)
        if match and match.group(1).strip() in ("", "[]", "~", "null"):
            # `companies: []` -> `companies:` so block items can follow.
            body = body[: match.start()] + "companies:" + body[match.end() :]
        elif match:
            raise ValueError(
                f"{path}: `companies` is an inline list; rewrite it in block form "
                "(one `- name:` per line) before appending"
            )
        else:
            body = (body.rstrip("\n") + "\n\ncompanies:\n") if body.strip() else "companies:\n"

    lines = ["", f"  # {note}" if note else "  # appended by `jobhunter resolve`"]
    for target in fresh:
        lines.append(f"  - name: {_yaml_scalar(target.name)}")
        for field in ("domain", "ats", "ats_token", "careers_url"):
            if value := getattr(target, field, None):
                lines.append(f"    {field}: {_yaml_scalar(value)}")

    updated = body.rstrip("\n") + "\n" + "\n".join(lines) + "\n"
    target_path.write_text(updated, encoding="utf-8")
    try:
        written = load_targets(target_path)
    except Exception as exc:  # noqa: BLE001 - restore before re-raising
        target_path.write_text(original, encoding="utf-8")
        raise ValueError(f"{path}: append produced invalid YAML, file restored ({exc})") from exc
    if len(written) != len(existing) + len(fresh):
        target_path.write_text(original, encoding="utf-8")
        raise ValueError(
            f"{path}: expected {len(existing) + len(fresh)} companies after append, "
            f"found {len(written)}; file restored"
        )
    return len(fresh)


def _yaml_scalar(value: str) -> str:
    """Quote a scalar only when it needs it, so the file stays readable."""
    text = str(value)
    if text != text.strip() or not text:
        return repr(text)
    if re.search(r"[:#\[\]{},&*?|>%@`\"']|^-", text):
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


settings = Settings()
