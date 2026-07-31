# JobHunter — Architecture & Roadmap

A pipeline that discovers job postings from ATS boards and company careers pages, resolves a
verified contact for each opening, scores fit against your profile, and drafts outreach.

---

## 1. Guardrails (read this first)

These constraints shape the design; ignoring them turns the project into a liability.

| Concern | Rule the system enforces |
|---|---|
| **Site ToS** | Only official/public ATS JSON endpoints and pages that `robots.txt` allows. No LinkedIn/Indeed scraping. |
| **Rate limits** | Per-host token bucket (default 1 req/sec), exponential backoff on 429/5xx, on-disk response cache. Identifiable User-Agent with a contact URL. |
| **GDPR / personal data** | Prefer **role** addresses (`careers@`, `jobs@`, `talent@`). Named-person emails are stored with `source_url` provenance, a `confidence` score, and a `suppressed` flag. A `purge` command deletes any contact on request. |
| **CAN-SPAM / PECR** | Outreach is *drafted*, never auto-sent. Every draft is 1:1, job-specific, identifies you, and includes an opt-out line. No bulk blasts. |
| **SMTP verification** | RCPT-TO probe only, never sends data, aborts with `RSET`/`QUIT`. Rate-limited hard — aggressive probing gets your IP blocklisted. Catch-all domains are detected and downgraded to `risky`, not marked valid. |

Bottom line: this is a **research assistant that surfaces openings and one right person to email**,
not a lead-gen scraper. That distinction is what keeps it legal and what makes the emails actually work.

---

## 2. Stack

**Python 3.11+** — the scraping and email-parsing ecosystem is unmatched here.

| Layer | Choice | Why |
|---|---|---|
| HTTP | `httpx` (async) + `tenacity` | HTTP/2, connection pooling, clean async, retry decorators |
| HTML parsing | `selectolax` → fallback `beautifulsoup4` | selectolax is ~10x faster; bs4 for gnarly markup |
| JS-rendered pages | `playwright` (opt-in per company) | Many careers pages are React SPAs; only invoked when static fetch yields nothing |
| Storage | SQLite + `SQLAlchemy 2.0` | Zero-ops, single file, trivially upgradeable to Postgres by swapping the URL |
| DNS/SMTP | `dnspython` + stdlib `smtplib` | MX lookup and RCPT probing |
| CLI | `typer` + `rich` | Typed commands, readable tables/progress |
| Config | `pydantic-settings` + YAML | Secrets from `.env`, target lists in version-controlled YAML |
| Export | `pandas` + `openpyxl` | CSV/XLSX for your tracker |
| Tests | `pytest` + `respx` | Mock HTTP so the suite runs offline and deterministically |

---

## 3. Component map

```
                       ┌────────────────────────────┐
  companies.yaml  ───► │  Source adapters           │
  (ATS tokens,         │  greenhouse lever ashby    │
   careers URLs)       │  workable smartrecruiters  │
                       │  careers_page (generic)    │
                       └──────────────┬─────────────┘
                                      │ RawJob
                       ┌──────────────▼─────────────┐
                       │  Normalizer + deduper      │
                       │  canonical title, seniority│
                       │  remote flag, content hash │
                       └──────────────┬─────────────┘
                                      │ Job
              ┌───────────────────────┼───────────────────────┐
              ▼                       ▼                       ▼
   ┌────────────────────┐  ┌────────────────────┐  ┌────────────────────┐
   │ Matcher            │  │ Contact finder     │  │ Store (SQLite)     │
   │ profile.yaml →     │  │ 1 page scrape      │  │ companies/jobs/    │
   │ 0–100 fit score    │  │ 2 pattern generate │  │ contacts/runs      │
   │ + reasons          │  │ 3 MX + SMTP verify  │  └─────────┬──────────┘
   └─────────┬──────────┘  └─────────┬──────────┘            │
             └────────────┬──────────┘                       │
                          ▼                                  ▼
                 ┌────────────────┐                 ┌────────────────┐
                 │ Outreach       │                 │ Export         │
                 │ drafter (LLM)  │                 │ CSV / XLSX     │
                 └────────────────┘                 └────────────────┘
```

Each source adapter implements one interface, so adding a new ATS is ~40 lines:

```python
class JobSource(Protocol):
    name: str
    async def fetch(self, target: Target) -> list[RawJob]: ...
```

---

## 4. Data model

```
companies   id, name, domain, ats, ats_token, careers_url, catch_all(bool|null), created_at
jobs        id, company_id, source, external_id, title, canonical_title, seniority,
            location, remote, description, url, posted_at, content_hash,
            first_seen, last_seen, closed_at, fit_score, fit_reasons(json)
contacts    id, company_id, email, kind(role|person), first_name, last_name, role_title,
            source_url, discovery_method, confidence(0-1),
            verify_status(valid|risky|invalid|unknown), verified_at, suppressed
outreach    id, job_id, contact_id, subject, body, status(draft|sent|replied), created_at
runs        id, started_at, finished_at, jobs_seen, jobs_new, contacts_found, errors(json)
```

Key decisions:

- **`content_hash`** over `(source, external_id)` for dedup — catches the same role reposted under a
  new ID, and the same role listed on both an ATS board and the company's own page.
- **`first_seen` / `last_seen` / `closed_at`** instead of deleting rows. A posting vanishing from the
  board is signal (filled or pulled), and you want the history.
- **`catch_all` on the company**, not the contact — it's a domain property, and caching it avoids
  re-probing on every run.

---

## 5. Contact discovery: the actual hard part

Three strategies, tried in order, each cheaper-and-safer before more-speculative:

**Tier 1 — Scrape published addresses (confidence 0.9)**
Fetch `/careers`, `/jobs`, `/about`, `/team`, `/contact`, `/company`, plus the job description body.
Regex for emails, decode `mailto:` and common obfuscations (`name [at] domain`). Filter out
`noreply@`, `support@`, `press@`, vendor domains. Prefer role addresses on the company's own domain.

**Tier 2 — Named recruiter + pattern generation (confidence 0.4–0.7)**
If a team/about page names a recruiter or hiring manager, generate candidates from the
12 common corporate patterns (`first.last@`, `flast@`, `first@`, `f.last@`, …). Crucially: if
*any* known-good email at that domain is already in the DB, infer the house pattern from it and
score that candidate 0.85 instead of guessing across all 12.

**Tier 3 — Verify (promotes or kills the guess)**
1. MX lookup — no MX records means the domain can't receive mail; mark `invalid`, stop.
2. Catch-all probe — RCPT a random address like `zz9x7q@domain`. Accepted? Domain is catch-all, so
   verification is meaningless; every guess becomes `risky`. Cache on the company.
3. RCPT probe the candidate. `250` → `valid`, `550` → `invalid`, anything else → `unknown`.

Only Tier-1 results and `valid` Tier-2 results are surfaced by default. This is the difference
between a tool you can act on and a spreadsheet of bounces.

---

## 6. Roadmap

**Phase 1 — Core pipeline (this session's scaffold)**
ATS adapters, careers-page crawler, normalizer/deduper, SQLite store, contact finder with
verification, profile scorer, CSV/XLSX export, CLI.

**Phase 2 — Quality**
Playwright fallback for SPA careers pages. Embedding-based matching (`sentence-transformers`)
so "Backend Engineer" matches "Server-side Developer". Company enrichment: size, funding, tech
stack. Pluggable paid-enrichment provider (Hunter/Apollo) behind the same interface.

**Phase 3 — Workflow**
LLM outreach drafter pulling specifics from the job description. Resume tailoring: gap analysis
between your profile and the posting's requirements. Application tracker with follow-up reminders.
Scheduled daily runs emailing you a digest of new high-fit roles.

**Phase 4 — Surface**
FastAPI backend + a small web UI: filter/sort, one-click "draft outreach", kanban application board.
Gmail API integration for send + reply detection.

---

## 7. Realistic failure modes

| Problem | Mitigation |
|---|---|
| Careers page is a React SPA — static fetch returns an empty shell | Detect low text-to-markup ratio, retry with Playwright |
| Company uses an ATS you haven't written an adapter for | Careers-page crawler as fallback; log unknown ATS fingerprints so you know what to build next |
| SMTP probes get your IP blocklisted | Hard rate limit, long backoff, and treat verification as optional enrichment the pipeline works without |
| Catch-all domains make verification useless | Detect and label `risky` rather than reporting false confidence |
| Same job counted 3× across sources | `content_hash` dedup on normalized title + company + location |
| Scoring says everything is a 70 | Score components stored separately in `fit_reasons` so you can see *why* and tune weights |

---

## 8. Cost

Phase 1 is free — public endpoints, local SQLite, no paid APIs. Phase 2's optional enrichment runs
roughly $0.01–0.05 per verified email. Phase 3's LLM drafting is a few cents per draft. The
expensive resource is your own outreach reputation, which is why nothing sends automatically.
