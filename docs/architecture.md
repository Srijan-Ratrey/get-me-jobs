# Architecture

The module map, the layering rule, and where each hard rule is actually enforced.

`PLAN.md` §3 has a conceptual sketch from before the code existed and has drifted
(it shows an LLM drafter, and predates `pipeline`, `policy`, `harvest`, `freehire`
and `importer`). **This file is the one derived from the code**, and
`tests/test_architecture.py` fails if it stops being true.

## The layering rule

Every module sits in a layer and may import only from layers strictly below it.
There are currently **zero** upward or sideways imports and **zero** cycles.

```
L7  cli                     typer + rich. The only module allowed to print.
L6  pipeline                run_scan / run_score / run_resolve / run_contacts
L5  harvest   export   outreach.sender
L4  sources.registry        outreach.policy
L3  sources.{greenhouse,lever,ashby,workable,freehire,careers_page}
    contacts.{finder,importer}       outreach.drafter
L2  sources.base   matching.{scorer,llm_scorer}
    contacts.{scraper,patterns,verify}
L1  http                    db
L0  config                  models
```

Read it as: a source adapter can use `http` and `models`, but nothing in
`sources/` may ever reach for `pipeline` or `db`. That is what keeps adapters
testable against a saved fixture with no database in the picture.

## Flow

```
companies.yaml ─┐
                ├─► sources.registry ─► adapters ─► list[RawJob] ─┐
careers URLs  ──┘        (PoliteClient: rate limit, robots, cache) │
                                                                   ▼
data/*.jsonl ──► harvest ──► candidate ATS tokens          db.upsert_job
                                                        (canonical_title,
                                                         seniority, remote,
                                                         compute_hash dedup)
                                                                   │
                                                                   ▼
                                                                 Job rows
                        ┌──────────────────────────┬───────────────┤
                        ▼                          ▼               ▼
              matching.scorer              contacts.finder      export
           (0-100 + per-component      1 scraper  2 patterns   CSV / XLSX
            reasons, or hard zero)     3 verify (opt-in)
                        │              contacts.importer (hand CSV)
                        └──────────┬───────────────┘
                                   ▼
                          outreach.policy          ◄── every send gate
                                   │
                        ┌──────────┴──────────┐
                        ▼                     ▼
                outreach.drafter      outreach.sender ─► Gmail (send scope only)
              Draft | Refusal
```

## Modules

| Module | L | Owns | Reusable alone? |
|---|---|---|---|
| `config` | 0 | `Settings` (env), `Target`/`Profile`/`Applicant` (YAML) | yes, but exports a module-level `settings` singleton — see Reuse notes |
| `models` | 0 | ORM tables + `RawJob`, `canonical_title`, `compute_hash` | yes |
| `http` | 1 | `PoliteClient`: per-host rate limit, robots, disk cache, backoff | yes — all ctor args override `settings` |
| `db` | 1 | engine/session, upserts, stale closing, suppression, additive migrations | one engine per process (module global `_engine`) |
| `sources/base` | 2 | `JobSource` Protocol + HTML/date helpers | yes |
| `matching/scorer` | 2 | `score_job(job, profile)` → score + reasons | yes — pure function |
| `matching/llm_scorer` | 2 | optional relevance pass; **provider undecided, parked** | yes |
| `contacts/scraper` | 2 | Tier 1, published addresses | yes |
| `contacts/patterns` | 2 | Tier 2, name → candidate addresses | yes |
| `contacts/verify` | 2 | Tier 3, MX + catch-all + RCPT | yes |
| `sources/*` | 3 | one ATS each, ~40-80 lines, returns `list[RawJob]` | yes — never touch the DB |
| `contacts/finder` | 3 | runs the three tiers in order, stops early | yes |
| `contacts/importer` | 3 | hand-researched HR CSV → `Contact` rows | needs `db` |
| `outreach/drafter` | 3 | one message from one (job, contact), or a `Refusal` | yes |
| `sources/registry` | 4 | `Target` → adapter dispatch, fingerprint handoff | yes |
| `outreach/policy` | 4 | **every** send gate | needs `db` session |
| `outreach/sender` | 5 | Gmail transport behind a `MailTransport` Protocol | yes — inject a fake |
| `harvest` | 5 | probe published ATS token lists for India hiring | yes |
| `export` | 5 | CSV / XLSX rows | needs `db` |
| `pipeline` | 6 | `run_scan` / `run_score` / `run_resolve` / `run_contacts` | yes — this is the library entry point, not `cli` |
| `cli` | 7 | typer + rich. The only module allowed to `print` | no, and shouldn't be |

## Where the hard rules live

Each of these is enforced in exactly one place. If you need the rule, import that
module rather than re-implementing the check.

| Rule (CLAUDE.md) | Enforced in |
|---|---|
| 1 — all network I/O through `PoliteClient` | `http.PoliteClient`; adapters take a client, never construct one |
| 2 — no LinkedIn/Indeed/Glassdoor/ZipRecruiter | absence of an adapter; `sources/registry` has no path to one |
| 3 — sending capped and gated | `outreach/policy.py` only. `sender.send_batch` re-checks `may_send` immediately before the wire |
| 4 — contacts record provenance | `db.upsert_contact`, `contacts/importer.load_contact_csv` (rejects rows with no `source_url`) |
| 5 — prefer role addresses | `outreach/policy.best_contact` — **but not `export._best_contact`, see below** |
| 6 — catch-all is `risky` | `contacts/verify.py` |
| 7 — `verify_emails` defaults False | `config.Settings` |

## Reuse notes

**The `settings` singleton is the ceiling.** `config.py` ends with
`settings = Settings()` at import time, and eight modules bind to that object. One
process therefore has exactly one configuration. `PoliteClient` is the model for
how to escape it — every constructor argument overrides the corresponding setting,
so it can be driven entirely by injection. `outreach/policy` and `contacts/verify`
read `settings` directly and cannot. The visible cost is in
`tests/test_outreach.py`, which monkeypatches `settings.daily_send_cap` eighteen
separate times.

**`pipeline` is the library API, `cli` is not.** That split holds for scan, score,
resolve and contacts. It does not hold for `list`, `stats` and `outreach status`,
which build their queries inline in `cli.py` — about ten `session_scope()` blocks
with raw `select()` in the presentation layer. Nothing outside typer can reuse them.

**One query is written twice.** `cli.list_jobs` and `export.collect_rows` both
build the same `select(Job, Company)` with the same five filters, and they have
already drifted: `list` supports `--company` and applies `limit` *after* the
Python-side location filter; `collect_rows` applies it *before*, so
`collect_rows(limit=50, location=["bengaluru"])` returns the global top 50 by
score and then discards most of them. Not reachable from the CLI today — the
`export` command exposes no `--limit` — but it is a public function.

**Two different answers to "the best contact for this company."**
`policy.best_contact` orders role addresses first, then confidence — that is hard
rule 5. `export._best_contact` orders by confidence alone. They agree today only
because all three contacts in the database are role addresses. Importing
hand-researched named individuals is what makes them diverge, and at that point
the CSV you apply from names a different person than the one the outreach path
would write to.

**`outreach/drafter` imports `scorer._contains_word`**, a private name, so that
the drafter can never claim a skill the scorer would not have credited. Deliberate
and documented at the import; noted here because it is the one intentional
reach across a module boundary.

## Testing

Every module has a direct test importer except `sources/base`, whose helpers
(`html_to_text`'s BeautifulSoup fallback, `parse_iso`'s `Z` suffix,
`parse_epoch_ms`'s overflow guard) are currently exercised only through the
adapters. `tests/conftest.py` blocks sockets, DNS and SMTP for the whole suite.
