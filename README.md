# JobHunter

Finds job postings on public ATS boards, resolves a hiring contact where one is
published, scores each opening against your profile, and exports a ranked sheet.

This is a **research assistant that surfaces openings and one right person to email**, not a
lead-generation scraper. Read [docs/compliance.md](docs/compliance.md) before changing anything
that touches the network — the rate limiting, `robots.txt` checks and draft-only outreach are what
keep the project on the defensible side of the line, and they are not style preferences.

## Install

```bash
uv sync --extra dev          # add --extra spa for the Playwright fallback (not built yet)
uv run jobhunter init        # creates the DB and your companies.yaml / profile.yaml
```

Requires Python 3.11+. `uv` provisions its own interpreter, so your system Python does not matter.
Copy `.env.example` to `.env` if you want to change any setting; every one has a working default.

## Commands

```bash
uv run jobhunter init                    # DB + companies.yaml / profile.yaml from the examples
uv run jobhunter resolve --from list.csv # discover each company's ATS from its careers page
uv run jobhunter harvest                 # probe published ATS token lists for India-hiring firms
uv run jobhunter scan                    # fetch jobs from every target in companies.yaml
uv run jobhunter score                   # rescore open jobs against profile.yaml
uv run jobhunter contacts                # resolve a hiring contact where one is published
uv run jobhunter list --min-score 55     # ranked openings, best first
uv run jobhunter export jobs.xlsx        # one row per job with its best contact
uv run jobhunter stats                   # what is in the database
uv run jobhunter purge --email x@y.com   # GDPR erasure: delete + suppress rediscovery
```

Anything that writes takes `--dry-run`. Add `-v` for debug logging.

**Only new postings.** `list` and `export` both take `--since` (when *we* first saw a posting) and
`--posted-within` (when the company published it). Both accept `7d`, `24h`, `2w`, a date, or
`last-scan`:

```bash
uv run jobhunter list --min-score 55 --since last-scan     # new since your last scan
uv run jobhunter list --min-score 55 --posted-within 30d   # freshly posted, not just newly seen
```

Add `--why` to `list` to see how each score was built — every score decomposes into the five
components that produced it.

## Adding companies

The pipeline only looks where you point it, so `companies.yaml` is the input that decides how
useful everything else is. Two ways to fill it:

**By hand**, if you know a company's ATS:

```yaml
companies:
  - name: Sarvam AI
    domain: sarvam.ai      # only used by contact discovery
    ats: ashby
    ats_token: sarvam
```

**From a spreadsheet**, if you have a list of companies and their careers pages. `resolve` fetches
each page, works out which ATS it is fronting, and writes the board token:

```bash
uv run jobhunter resolve --from companies.csv --dry-run   # look first
uv run jobhunter resolve --from companies.csv
```

The CSV needs a company-name column and a careers-URL column; `Company` / `Career page` /
`careers_url` and similar spellings are all recognised. Anything that does not resolve is written
to `unresolved-companies.md` **with the reason** — an unsupported ATS, a page with no ATS marker,
or an unreachable URL. When a careers URL fails outright, `resolve` asks the ATSs directly whether
they host that company, which recovers boards behind dead or bot-blocked careers pages.

**From published token lists**, if you would rather not curate a list at all. `harvest` reads flat
JSON arrays of ATS board tokens from `data/<ats>_companies.json`, probes each board once, and keeps
only the companies with openings you could actually take:

```bash
uv run jobhunter harvest --limit 300 --dry-run   # pilot: project the yield before committing
uv run jobhunter harvest                          # the full sweep
```

The sweep is long — thousands of boards at one request per second per host — so it is resumable:
progress goes to `.harvest-state.jsonl` and a re-run picks up where it stopped. `--min-india-jobs`
raises the bar if the yield is larger than you want to watch. Token lists are not shipped with this
repo; the ones used during development came from
[Feashliaa/job-board-aggregator](https://github.com/Feashliaa/job-board-aggregator) (`data/` is
CC BY-NC 4.0 — usable locally, not redistributable, hence gitignored).

## What to expect

Worth setting expectations honestly, because the shape of the output surprises people:

- **Supported ATSs are Greenhouse, Lever, Ashby and Workable.** Companies on Workday, iCIMS,
  SuccessFactors, Darwinbox, Keka or Freshteam are detected and reported, but cannot be read —
  there is no adapter. On one 108-company list, 2 resolved directly and 3 more were recovered by
  probing; the rest were on enterprise ATSs. **Modern ATSs correlate with company stage, not
  location** — startups are where this works.
- **SmartRecruiters is not usable at all.** Their `robots.txt` allows the postings API for
  LinkedInBot only. See [docs/sources.md](docs/sources.md).
- **Most companies publish no hiring email.** Contact discovery finds one for roughly a fifth of
  them; the rest report "no contact found — apply through the posting", which is the honest answer.
  A guessed address presented as a real one is worse than none, because bounces damage the sending
  reputation you are spending.
- **Most postings score zero, and that is the point.** A junior profile hard-zeroes anything whose
  title says Senior/Staff/Lead or whose description demands more years than your cap.

## Configuration

`jobhunter init` writes `companies.yaml` and `profile.yaml` from the `*.example.yaml` files. Your
copies are gitignored — they describe what you are looking for and which employers you are
targeting, so they stay on your machine rather than in the repository.

- **`companies.yaml`** — the employers to watch. Either an `ats` + `ats_token`, or just a
  `careers_url` for `resolve` to work out.
- **`profile.yaml`** — what you are looking for. Keep `must_have_keywords` short: they are treated
  as all-required, so a long list lowers the ceiling on every job. `exclude_keywords` match the
  **title only** and are a hard zero. `max_years_experience` reads the *smallest* figure a posting
  mentions, so an ad listing several bands is judged on the most junior one.
- **`.env`** — politeness, SMTP and cache settings, all prefixed `JOBHUNTER_`.

## How it works

```
companies.yaml ──> source adapters ──> normalise + dedup ──> SQLite
                   greenhouse lever      canonical title,      │
                   ashby workable        content hash          │
                   careers_page                                │
                                          ┌────────────────────┴───────┐
                                          v                            v
                                    scorer (profile)            contact finder
                                    0-100 + reasons             1 scrape
                                          │                     2 pattern guess
                                          │                     3 MX/RCPT verify
                                          └──────────> export (CSV / XLSX)
```

`PoliteClient` fronts every request: one per second per host, `robots.txt` honoured and cached,
responses cached on disk, exponential backoff that respects `Retry-After`. Nothing bypasses it.

The database exists to remember things a spreadsheet cannot: which postings are new since last
run, which have closed, and which addresses have been erased and must never be rediscovered.

## Tests

```bash
uv run pytest
```

237 tests, offline and deterministic. A `conftest.py` fixture blocks sockets, DNS and SMTP for
every test, so a test *cannot* silently start making real requests. CI runs the same suite on every
push. To confirm the guarantee independently on macOS:

```bash
sandbox-exec -p '(version 1)(allow default)(deny network*)' .venv/bin/pytest -q
```

Adapter tests assert field-by-field against real captured board responses in `tests/fixtures/`, so
a passing parser is one that works on production data.

## Nothing sends email

The outreach drafter (Phase 3, not built) writes rows to the `outreach` table with
`status='draft'`. A human reads each one and sends it from their own client. There is no send path,
and `docs/compliance.md` explains why that is the single design decision that most reduces risk.
