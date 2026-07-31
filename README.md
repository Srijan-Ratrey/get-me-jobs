# JobHunter

Discovers job postings from ATS boards and company careers pages, resolves a verified hiring
contact for each opening, scores fit against your profile, and drafts outreach.

This is a **research assistant that surfaces openings and one right person to email**, not a
lead-generation scraper. Read [docs/compliance.md](docs/compliance.md) before changing anything
that touches the network — the rate limits, `robots.txt` checks and draft-only outreach are what
keep the project on the defensible side of the line, and they are not style preferences.

## Install

```bash
uv sync --extra dev          # add --extra spa for the Playwright fallback
cp .env.example .env         # optional; every setting has a working default
```

Requires Python 3.11+. `uv` provisions its own interpreter, so your system Python does not matter.

## Commands

```bash
uv run jobhunter init            # create the DB + starter companies.yaml / profile.yaml
uv run jobhunter scan            # fetch jobs from every target in companies.yaml
uv run jobhunter contacts        # resolve a hiring contact for companies with open jobs
uv run jobhunter score           # rescore open jobs against profile.yaml
uv run jobhunter list --min-score 60
uv run jobhunter export out.xlsx
uv run jobhunter purge --email x@y.com   # GDPR erasure: delete + suppress rediscovery
```

Anything that writes accepts `--dry-run`.

## Configuration

`jobhunter init` writes `companies.yaml` and `profile.yaml` from the `*.example.yaml` files. Your
copies are gitignored — they describe what you are looking for and which employers you are
targeting, so they stay on your machine rather than in the repository.

- **`companies.yaml`** — the employers to watch. Either give an `ats` + `ats_token` directly, or
  just a `careers_url` and let the crawler fingerprint which ATS they use.
- **`profile.yaml`** — what you are looking for. Drives fit scoring. Keep `must_have_keywords`
  short: the scorer treats them as all-required, so a long list floors every score.
- **`.env`** — politeness, SMTP and cache settings, all prefixed `JOBHUNTER_`.

## Tests

```bash
uv run pytest
```

The suite runs offline and deterministically. A `conftest.py` fixture makes the network
structurally unreachable, so no test can make a real HTTP request, DNS lookup or SMTP connection.

## Nothing sends email

The outreach drafter writes rows to the `outreach` table with `status='draft'`. A human reads each
one and sends it from their own mail client. There is no send path.
