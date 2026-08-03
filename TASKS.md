# Build Backlog

> **Status:** Phase 1 is complete and verified — 237 tests passing with the network denied at OS
> level, `scan → score → contacts → export` working end to end against live boards. The one
> exception is `smartrecruiters.py`, which cannot be built compliantly (see 1.6).
>
> Phases 2–4 are not started, except the unknown-ATS report, which fell out of building
> `jobhunter resolve`.

Work top to bottom. Each task lists the spec to read first and acceptance criteria that must pass
before moving on. Don't batch phases — Phase 1 should run end-to-end before Phase 2 starts.

Read `CLAUDE.md` first. Read `docs/compliance.md` before writing anything that touches the network.

---

## Phase 1 — Core pipeline

Goal: `jobhunter scan && jobhunter contacts && jobhunter export out.xlsx` produces a spreadsheet
of scored openings with a contact for each.

### 1.1 Project setup
- [x] `pyproject.toml` with the dependencies pinned in `CLAUDE.md`; `dev` and `spa` extras; `jobhunter` console script
- [x] `.env.example`, `.gitignore` (ignore `*.db`, `.cache/`, `.env`)
- [x] `README.md` — install, the five commands, and a link to `docs/compliance.md`

**Done when:** `uv sync --extra dev` succeeds and `uv run jobhunter --help` prints.

### 1.2 Config — `config.py`
- [x] `Settings(BaseSettings)`, env prefix `JOBHUNTER_`, reading `.env`
- [x] `Target` and `Profile` pydantic models
- [x] `load_targets()` / `load_profile()` from YAML
- [x] Starter `companies.yaml` (3–4 real companies across different ATSs) and `profile.yaml`

Defaults that must not be loosened: `requests_per_second=1.0`, `respect_robots=True`,
`verify_emails=False`, `max_concurrency=5`.

**Done when:** settings load from env, YAML round-trips into the models, and a missing YAML key
raises a clear pydantic error rather than a `KeyError`.

### 1.3 Models — `models.py`
> Read: `PLAN.md` §4

- [x] SQLAlchemy 2.0 typed models: `Company`, `Job`, `Contact`, `Run`, `Suppression`
- [x] `RawJob` pydantic transport object
- [x] `RawJob.canonical_title()` — strip parentheticals, bracketed tags, `m/f/d` variants, and
      trailing `- Location` segments
- [x] `RawJob.detect_seniority()` → `intern|junior|mid|senior|staff|lead|None`
- [x] `RawJob.detect_remote()`
- [x] `RawJob.compute_hash(company_name)` — sha256 of company + canonical title + location,
      **excluding** source and external_id so cross-source duplicates collapse

**Done when:** the unit tests in 1.9 pass. Specifically: `"Senior Backend Engineer (Remote) - Berlin"`
and `"Senior Backend Engineer m/f/d"` at the same company/location produce the same hash.

### 1.4 Database — `db.py`
- [x] `init_db()`, `session_scope()` contextmanager
- [x] `upsert_company()` — only overwrite fields with non-null values, so a sparse YAML entry
      never wipes previously discovered data
- [x] `upsert_job()` → `(job, is_new)`; on existing rows bump `last_seen`, clear `closed_at`,
      backfill a missing description
- [x] `close_stale_jobs()` — mark postings absent from this run as closed. Never delete rows.

**Done when:** running the same scan twice creates zero new jobs and updates `last_seen`; removing
a job from a fixture and rescanning sets `closed_at`.

### 1.5 HTTP client — `http.py`
> Read: `docs/compliance.md` §"Site terms of service"

- [x] `PoliteClient` async context manager wrapping `httpx.AsyncClient`
- [x] Per-host token bucket with its own lock — one slow host must not stall others, and no host
      gets hit faster than the limit
- [x] `robots.txt` fetch, parse, and per-origin cache; raise `RobotsDisallowed` on a block.
      A missing or erroring `robots.txt` means unrestricted.
- [x] On-disk response cache keyed by URL hash, TTL from settings
- [x] Retry with exponential backoff on 429/5xx, honouring `Retry-After`; **no retry on other 4xx**
- [x] `get()` → `str`, `get_json()` → parsed
- [x] `SourceUnavailable` exception

**Done when:** `respx` tests prove rate limiting delays a second same-host request, a 429 with
`Retry-After: 1` is retried, a 404 raises immediately without retrying, and a `Disallow: /` in
robots raises `RobotsDisallowed`.

### 1.6 Source adapters — `sources/`
> Read: `docs/sources.md` — endpoints and field mappings are all there

- [x] `base.py` — `JobSource` protocol, shared HTML-to-text helper
- [x] `greenhouse.py` — remember `html.unescape()` before stripping tags
- [x] `lever.py` — title is `text`; concatenate `descriptionPlain` + `lists[]` + `additionalPlain`
- [x] `ashby.py` — filter out `isListed == false`
- [x] `workable.py` — needs `?details=true`; fall back to the v3 endpoint on 404
- [~] `smartrecruiters.py` — **not built.** `api.smartrecruiters.com/robots.txt` is
      `Disallow: /` for every user-agent except LinkedInBot, so there is no compliant way to
      read it. Claiming to be LinkedInBot is prohibited by `docs/compliance.md`. See
      `docs/sources.md`.
- [x] `careers_page.py` — fingerprint → SPA detection → JSON-LD → repeated-structure → link harvest
- [x] `registry.py` — dispatch a `Target` to its adapter, falling back to `careers_page`

Before writing each parser: fetch one live response, save it to `tests/fixtures/{ats}.json`, and
diff against the documented shape. Correct `docs/sources.md` if it has drifted.

**Done when:** each adapter has a fixture test asserting field-by-field, and one adapter raising
`SourceUnavailable` doesn't abort the run.

### 1.7 Contact discovery — `contacts/`
> Read: `docs/contact-discovery.md` in full

- [x] `scraper.py` — Tier 1. Candidate page list, `mailto:` + text + obfuscated + `data-cfemail`
      extraction, the reject list, the ranking table
- [x] `patterns.py` — Tier 2. **Pattern inference from a known-good address first**, then the
      prevalence-ordered table. Accent stripping, apostrophe/space handling, suffix removal.
- [x] `verify.py` — Tier 3. MX lookup → catch-all probe → RCPT probe. Serial, delayed, capped.
      Never issue `DATA`. Treat Google/Microsoft MX as `unknown`-by-design.
- [x] `finder.py` — run tiers in order, stop at confidence ≥ 0.85, return sorted list
- [x] Suppression check — never return an address on the suppression list

**Done when:** a fixture HTML page with `careers@` plus a named person returns the role address
first at 0.95; a catch-all domain marks candidates `risky` not `valid`; and pattern inference from
one known address produces exactly one candidate at 0.85 rather than six at 0.4.

### 1.8 Matching, export, CLI
- [x] `matching/scorer.py` — 0–100 from title match, must-have keywords, nice-to-haves, location/
      remote fit, seniority fit; **store each component separately in `fit_reasons`** so a
      surprising score is explainable. `exclude_keywords` present → hard zero.
- [x] `export.py` — CSV and XLSX, one row per job with its best contact, sorted by score
- [x] `cli.py` — `init`, `scan`, `contacts`, `score`, `list`, `export`, `purge`. Rich progress
      bars and a summary table. `--dry-run` on anything that writes.

**Done when:** the scorer's reasons add up to the total; `list --min-score 60` filters correctly;
`purge --email` deletes the row and suppresses future rediscovery; XLSX opens cleanly.

### 1.9 Tests
- [x] `respx`-mocked fixtures for every adapter
- [x] `canonical_title` / `compute_hash` cases, including the cross-source collapse
- [x] Pattern generation, including accented and multi-part surnames
- [x] Verification state machine — every branch, with `smtplib` mocked
- [x] Scorer boundaries: perfect match, exclude-keyword zero, empty profile
- [x] Rate limiter timing, robots blocking, retry behaviour

**Done when:** `uv run pytest` passes with **no network, DNS, or SMTP access**. Run it with
networking disabled to confirm.

---

## Phase 2 — Quality

- [ ] Playwright fallback for SPA careers pages, behind the `spa` extra. Reuse the same politeness
      budget — a headless browser is not an excuse to ignore the rate limit.
- [ ] Embedding-based title matching (`sentence-transformers`, local model) so "Backend Engineer"
      matches "Server-side Developer". Keep keyword scoring as a fallback and blend the two.
- [ ] Company enrichment: size, funding stage, tech stack, from public sources
- [ ] Pluggable enrichment provider interface (`ContactProvider`) so Hunter.io/Apollo can drop in
      behind the same call the free scraper uses. Free path stays the default.
- [ ] `docs/sources.md` additions: Recruitee, Personio (XML feed), Teamtailor, BambooHR
- [x] Unknown-ATS fingerprint log → a report of which adapter to build next
      (`jobhunter resolve` writes `unresolved-companies.md`, with a tally naming which
      missing adapter would unlock the most companies)

## Phase 3 — Workflow

- [ ] LLM outreach drafter. Pulls specifics from the job description; a draft that reads
      identically with the company name swapped is a bug. Writes `status='draft'` only.
- [ ] Resume gap analysis: profile vs. posting requirements, with concrete suggestions
- [ ] Application tracker: status transitions, follow-up reminders (one follow-up, 7 days)
- [ ] `prune --older-than 365d` for retention compliance
- [ ] Scheduled daily run emailing a digest of new high-fit roles

## Phase 4 — Surface

- [ ] FastAPI backend over the existing store
- [ ] Web UI: filter/sort, one-click draft, kanban application board
- [ ] Gmail API for send + reply detection — the first place a send path exists. Gate it behind
      explicit per-message confirmation.

---

## Definition of done, for any task

1. Type-annotated, `from __future__ import annotations`, modern union syntax
2. Tests pass offline
3. No network call outside `PoliteClient`
4. No new `Contact` path that skips `source_url`
5. Nothing sends email
6. A one-line note in `README.md` if the task added a user-facing command
