"""Configuration: settings from env, targets and profile from YAML."""
from __future__ import annotations

from pathlib import Path

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
    return [Target(**t) for t in data.get("companies", [])]


def load_profile(path: str | Path) -> Profile:
    data = yaml.safe_load(Path(path).read_text()) or {}
    return Profile(**(data.get("profile") or data))


settings = Settings()
