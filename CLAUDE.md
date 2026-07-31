# JobHunter

A pipeline that discovers job postings from ATS boards and company careers pages, resolves a
verified hiring contact for each opening, scores fit against a profile, and drafts outreach.

**You are building this from scratch.** Work through `TASKS.md` in order. Read the linked spec
before starting each phase — the specs contain endpoint shapes and algorithms you should not
have to guess at.

## Docs map

| File | What's in it |
|---|---|
| `TASKS.md` | The build backlog. Phased, with acceptance criteria. **Start here.** |
| `PLAN.md` | Architecture, component map, data model, failure modes |
| `docs/sources.md` | Exact ATS endpoints, response shapes, field mappings |
| `docs/contact-discovery.md` | Tiered discovery algorithm, pattern table, verification state machine |
| `docs/compliance.md` | Hard legal/ethical rules. Non-negotiable. |

## Stack

Python 3.11+. Do not substitute these without asking.

- `httpx[http2]` async — all network I/O
- `selectolax` for HTML, `beautifulsoup4` only as fallback for broken markup
- `playwright` — optional extra, only for JS-rendered careers pages
- `sqlalchemy` 2.0 typed ORM + SQLite
- `pydantic` v2 + `pydantic-settings` for config
- `dnspython` + stdlib `smtplib` for MX/RCPT verification
- `typer` + `rich` for CLI
- `pytest` + `pytest-asyncio` + `respx` for tests

## Layout

```
jobhunter/
  config.py           Settings (env) + Target/Profile (YAML)
  models.py           SQLAlchemy models + RawJob transport object
  db.py               Session scope, upserts, stale-job closing, suppression
  http.py             PoliteClient: rate limit, robots, cache, retry
  pipeline.py         Run orchestration for scan / score / contacts
  sources/
    base.py           JobSource protocol + HTML/date helpers
    greenhouse.py lever.py ashby.py workable.py
    careers_page.py   Generic crawler + ATS fingerprint detection
    registry.py       Target -> adapter dispatch, fingerprint handoff
  contacts/
    finder.py         Orchestrates the three tiers
    scraper.py        Tier 1: harvest published addresses
    patterns.py       Tier 2: name -> candidate emails
    verify.py         Tier 3: MX + catch-all + RCPT probe
  matching/scorer.py  Profile -> 0-100 fit score + reasons
  outreach/drafter.py Draft-only email generation  (Phase 3, not built yet)
  export.py           CSV / XLSX
  cli.py              typer app
companies.yaml        Targets to watch
profile.yaml          What you're looking for
tests/fixtures/       Saved API responses
```

Two deviations from the sketch above, both deliberate:

- **`pipeline.py` exists** so that run orchestration is importable and testable without
  going through typer. `cli.py` stays presentation-only, and remains the one module allowed
  to `print`.
- **There is no `smartrecruiters.py`.** `api.smartrecruiters.com/robots.txt` disallows the
  postings API for every user-agent except LinkedInBot, so with `respect_robots=True` there is
  no compliant way to use it. See `docs/sources.md`.

## Hard rules

These are not style preferences. Violating them breaks the project's legal footing or gets the
user's IP blocklisted. See `docs/compliance.md`.

1. **All network I/O goes through `PoliteClient`.** Never call `httpx` directly from an adapter.
   The rate limiting, robots.txt checks, and caching live there and must not be bypassable.
2. **No LinkedIn, Indeed, Glassdoor, or ZipRecruiter scraping.** Their ToS prohibit it and they
   have serious anti-bot. If a task seems to need them, stop and say so.
3. **Nothing sends email automatically.** The drafter writes to the `outreach` table with
   `status='draft'`. There is no send path in Phase 1–3.
4. **Every contact row records `source_url` and `discovery_method`.** Provenance for personal
   data is a GDPR obligation.
5. **Prefer role addresses** (`careers@`, `jobs@`, `talent@`) over named individuals. When both
   exist, the role address wins.
6. **Catch-all domains are `risky`, never `valid`.** Reporting false confidence is worse than
   reporting nothing.
7. **`verify_emails` defaults to `False`.** SMTP probing is opt-in enrichment; the pipeline must
   work fully without it.

## Conventions

- `from __future__ import annotations` at the top of every module; modern union syntax (`str | None`).
- Async by default for anything I/O bound. Keep SQLAlchemy synchronous inside `session_scope()` —
  mixing async ORM in adds complexity for no gain at this scale.
- Adapters return `list[RawJob]` and never touch the DB. Normalization and persistence belong to
  `db.upsert_job`. This keeps adapters trivially testable against fixtures.
- Raise domain exceptions (`RobotsDisallowed`, `SourceUnavailable`), never bare `Exception`.
- One adapter failing must not abort a run. Collect errors into `runs.errors` and continue.
- Log with `logging.getLogger(__name__)`. No `print` outside `cli.py`.
- Type-annotate public functions. Docstrings explain *why*, not *what*.

## Commands

```bash
uv sync --extra dev              # install
uv run jobhunter init            # create DB + starter companies.yaml/profile.yaml
uv run jobhunter scan            # fetch jobs from all targets
uv run jobhunter contacts        # resolve contacts for companies with open jobs
uv run jobhunter score           # rescore jobs against profile.yaml
uv run jobhunter list --min-score 60
uv run jobhunter export out.xlsx
uv run jobhunter purge --email x@y.com   # GDPR erasure
uv run pytest                    # tests must pass offline
```

## Testing

Tests run offline and deterministically. Mock HTTP with `respx`, feeding saved fixtures from
`tests/fixtures/`. No test may make a real network call, do a DNS lookup, or open an SMTP
connection. Cover at minimum: each adapter's parser, `canonical_title` normalization,
`compute_hash` dedup collapsing duplicates across sources, pattern generation, the verification
state machine, and the scorer's boundary cases.

## When you're unsure

The ATS response shapes in `docs/sources.md` were written from documentation that may have
drifted. Before writing a parser, fetch one live response and diff it against the documented
shape. If they disagree, trust the live response and correct the doc.
