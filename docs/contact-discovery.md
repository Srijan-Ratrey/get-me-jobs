# Contact Discovery

The hard part of the project. Finding a *plausible* email is easy; finding one that reaches a
human who can act on your application is not.

**Goal: one high-confidence contact per company, not fifty guesses.** A single verified role
address beats a spreadsheet of pattern guesses, because guesses bounce and bounces damage your
sending reputation.

---

## Three tiers

Run in order. Stop as soon as you have a contact with `confidence >= 0.85`. Each tier is more
speculative and more expensive than the last, so there's never a reason to skip ahead.

```
Tier 1  scrape published addresses      confidence 0.75 – 0.95   free, safe
   │    (stop here if a role address on the company domain was found)
   ▼
Tier 2  named person -> pattern guess    confidence 0.40 – 0.85   free, speculative
   │
   ▼
Tier 3  MX + catch-all + RCPT verify     promotes or kills Tier 2
```

Only Tier-1 results and `verify_status == "valid"` Tier-2 results are surfaced by default.
Everything else stays in the DB with its real confidence, visible via `--include-risky`.

---

## Tier 1 — Scrape published addresses

Pages to fetch, in this order, resolved against the company domain:

```
/careers  /career  /jobs  /join-us  /about  /about-us  /team  /people
/contact  /contact-us  /company  /imprint  /impressum   (EU sites: legally must list contact)
```

Plus: the **job description body** you already have. Postings frequently end with "questions?
email jobs@…", and that address is both free and exactly the right one.

Also honour `target.contact_pages` from `companies.yaml` — the user's manual override, which
should be tried first and trusted most.

### Extraction

Match emails in three forms:

1. `mailto:` hrefs — highest signal, decode percent-encoding and strip `?subject=`.
2. Plain text via a standard address regex.
3. Obfuscated: `name [at] domain [dot] com`, `name (at) domain`, `name AT domain DOT com`.
   Normalize the separators, then re-validate against the address regex.

Also decode Cloudflare's `data-cfemail` hex attribute — it's a trivial XOR against the first
byte, and it's common enough on small-company sites to be worth the ten lines.

### Filtering

**Reject** — never a hiring contact:

```
noreply@ no-reply@ donotreply@ postmaster@ abuse@ webmaster@ admin@ root@
support@ help@ sales@ billing@ press@ media@ legal@ privacy@ security@
info@ (accept only if nothing better exists, confidence 0.5)
```

Also reject: any address whose domain isn't the company domain or a known subdomain (you're
picking up vendor/agency addresses); anything ending in an image or asset extension (regex
false positives); addresses appearing in more than ~5 unrelated companies' results (a shared
CMS template leaked a developer's address).

**Rank** — highest first:

| Local part | Confidence | Kind |
|---|---|---|
| `careers@` `jobs@` `recruiting@` `recruitment@` `talent@` `hiring@` | 0.95 | role |
| `hr@` `people@` `peopleops@` `personal@` `bewerbung@` | 0.90 | role |
| `apply@` `applications@` `join@` `work@` | 0.85 | role |
| named person on a `/team` page with a recruiting-ish title | 0.80 | person |
| named person, title unknown | 0.60 | person |
| `hello@` `contact@` `team@` | 0.55 | role |
| `info@` | 0.50 | role |

A recruiting-ish title means the surrounding text matches
`recruit|talent|people|hr|human resources|hiring|staffing|sourc`.

Record `source_url` for every hit. This is not optional — see `docs/compliance.md`.

---

## Tier 2 — Pattern generation

Only run when Tier 1 found nothing above 0.55 **and** you have a real first/last name from a
team page or the job posting ("Reach out to Anna Schmidt"). Never generate patterns from a name
you invented.

### Infer before you guess

Before generating candidates, check whether any **known-good** address already exists for this
domain — from Tier 1, from a previous run, or from a sibling company row. If one does, reverse
it into a pattern and generate **only** that pattern. Cache it on `companies.email_pattern`.

This is the single highest-leverage step in the whole module: it turns twelve guesses at 0.4
into one guess at 0.85, and it means the second contact you look up at a company is nearly free.

### Pattern table

For `Anna Schmidt` at `acme.com`, ordered by real-world prevalence:

| Pattern | Example | Prior |
|---|---|---|
| `first.last` | `anna.schmidt@acme.com` | 0.35 |
| `first` | `anna@acme.com` | 0.15 |
| `flast` | `aschmidt@acme.com` | 0.14 |
| `firstlast` | `annaschmidt@acme.com` | 0.10 |
| `first_last` | `anna_schmidt@acme.com` | 0.07 |
| `firstl` | `annas@acme.com` | 0.05 |
| `f.last` | `a.schmidt@acme.com` | 0.05 |
| `last.first` | `schmidt.anna@acme.com` | 0.03 |
| `last` | `schmidt@acme.com` | 0.03 |
| `first-last` | `anna-schmidt@acme.com` | 0.02 |
| `lastf` | `schmidta@acme.com` | 0.01 |

Cap candidates at `settings.max_guesses_per_domain` (default 6) — the tail below 0.05 isn't
worth the SMTP probes.

Normalization before substitution: lowercase; strip accents to ASCII (`ä→a`, `ø→o`, `ç→c`);
drop apostrophes and spaces in surnames (`O'Brien→obrien`, `van der Berg→vanderberg`); use only
the first given name when several are present; drop suffixes (`Jr`, `III`) and honorifics
(`Dr.`, `Prof.`).

Unverified pattern guesses get `confidence = 0.4 × prior_weight`, capped at 0.5. They must not
be surfaced as actionable until Tier 3 promotes them.

---

## Tier 3 — Verification

Opt-in (`settings.verify_emails`, default `False`). The pipeline must be fully useful without it.

### State machine

```
                    ┌─────────────────┐
                    │  MX lookup      │
                    └───┬─────────┬───┘
                no MX   │         │  MX found
                        ▼         ▼
                   ┌────────┐  ┌──────────────────────┐
                   │invalid │  │ catch-all probe      │
                   └────────┘  │ RCPT zz9x7q@domain   │
                               └───┬──────────────┬───┘
                       accepted    │              │  rejected
                                   ▼              ▼
                            ┌──────────┐   ┌───────────────┐
                            │  risky   │   │ RCPT candidate│
                            │ (cache   │   └───┬───┬───┬───┘
                            │ catch_all│    250│550│else
                            │ =True)   │       │   │   │
                            └──────────┘       ▼   ▼   ▼
                                          valid invalid unknown
```

1. **MX lookup** (`dnspython`). No MX and no A record → `invalid`, stop. Free and kills a
   surprising number of typo'd domains.
2. **Catch-all probe.** RCPT a random address that cannot exist (`zz9x7q3m@domain`). If the
   server accepts it, the domain accepts everything, so verification is meaningless — mark every
   candidate `risky` and cache `catch_all=True` on the company so you never re-probe.
3. **RCPT probe** the candidate: `EHLO` → `MAIL FROM` → `RCPT TO` → `RSET` → `QUIT`.
   `250`/`251` → `valid`. `550`/`551`/`553` → `invalid`. `450`/`451`/`452`/timeout/anything
   else → `unknown` (greylisting; retry once after a long delay, then give up).

**Never issue `DATA`.** The probe must not be capable of sending a message.

### Not getting blocklisted

- `settings.smtp_delay_seconds` (default 3.0) between probes **to the same MX host**, serially.
  Never probe one host concurrently.
- Cap probes per domain per run at the candidate limit. Stop the moment one comes back `valid`.
- Cache results by email with a long TTL (~30 days). Never re-probe within a run.
- Set `smtp_helo_host` and `smtp_mail_from` to a domain you actually control. A HELO that
  doesn't resolve is an instant reject on well-configured servers.
- Expect Google Workspace and Microsoft 365 to return `unknown` regardless — they deliberately
  don't leak recipient validity. Treat `unknown` on those MX hosts as "no information", not as a
  negative signal. Detect them by MX host (`*.google.com`, `*.outlook.com`, `*.protection.outlook.com`).

Given that, verification is most useful for self-hosted and smaller-provider domains. That's
fine — those are also where pattern guessing is least reliable, so the coverage lines up.

---

## Output contract

`find_contacts(company, jobs) -> list[Contact]`, sorted by confidence descending. Every row
carries `email`, `kind`, `confidence`, `verify_status`, `discovery_method`, `source_url`.

The CLI shows the top contact per company by default. `--all-contacts` shows the rest,
`--include-risky` includes unpromoted guesses.

If nothing clears 0.55, return an empty list and say so plainly. "No contact found — apply
through the posting" is a useful, honest answer. A guessed address presented as a real one is
not.
