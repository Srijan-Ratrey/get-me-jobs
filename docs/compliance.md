# Compliance Guardrails

Not legal advice — I'm not a lawyer, and you should get one if this ever goes beyond personal
use. What follows is the set of engineering constraints that keep a personal job-search tool on
the defensible side of the line, and the reasoning behind each.

The distinction that matters: **this is a research assistant that surfaces openings and one right
person to contact, not a lead-generation scraper.** Every rule below follows from that. A tool
that harvests thousands of personal emails to blast is a different product with a different legal
profile, and the code should make that hard to build by accident.

---

## Site terms of service

| Source | Status | Why |
|---|---|---|
| Greenhouse, Lever, Ashby, Workable public board APIs | **Use freely** | These are public endpoints that exist to syndicate job listings. Publishing them widely is the point. Verified 2026-07-31: none of these hosts disallow their board API in `robots.txt`. |
| SmartRecruiters postings API | **Do not use** | Listed as usable in an earlier version of this table; that was wrong. `api.smartrecruiters.com/robots.txt` is `Disallow: /` for `*`, with `Allow: /v1/companies/` granted **only to LinkedInBot**. Being public is not the same as being crawlable, and the site owner's directives decide. Presenting ourselves as LinkedInBot to obtain that Allow is prohibited below. See `docs/sources.md`. |
| Company careers pages | **Use, respecting robots.txt** | Public pages a company wants indexed. Honour their crawl directives. |
| LinkedIn, Indeed, Glassdoor, ZipRecruiter | **Do not scrape** | ToS explicitly prohibit automated collection, and they enforce it. `hiQ v. LinkedIn` established that scraping public data isn't a *CFAA* violation, but it left breach-of-contract and state-law claims alive — and they'll block you long before any of that matters. Use their official APIs or paid partners if you need this data. |
| Aggregators reselling scraped listings | **Check individually** | Many prohibit redistribution. |

Implementation requirements:

- `robots.txt` is checked before every fetch and cached per origin. `respect_robots` may be
  configurable but defaults to `True`, and nothing in the codebase should set it to `False`.
- **A 401 or 403 on `robots.txt` disallows the entire origin.** Only a 404 (or a network error)
  means unrestricted. A missing file says the site published no rules; an access-controlled one
  says the rules exist and are not for us, and reading that as permission would grant this client
  access to precisely the hosts that declined to state terms. This matches stdlib
  `RobotFileParser.read()` and Google's specification. Corrected 2026-08-17: the client previously
  treated every non-200 alike.
- Identify the bot honestly in `User-Agent`, with a URL a site owner can visit to understand what
  it is and how to block it. Never impersonate a browser to evade detection — that turns a
  technical measure into a circumvented one, which is where the argument gets much worse.
- Per-host rate limit, default 1 req/sec, with backoff on 429 and `Retry-After` honoured. Cache
  aggressively; a second run within the TTL should hit almost no network.
- No paywall, login-wall, or CAPTCHA circumvention. Ever. If a page needs auth, it's out of scope.

---

## Personal data (GDPR / UK GDPR)

An email address that identifies a person is personal data. Role addresses like `careers@acme.com`
generally aren't, which is the main practical reason to prefer them beyond deliverability.

What the code must do:

**Prefer role addresses.** When both a role address and a named individual are available, the
role address wins. This is enforced by the Tier-1 ranking in `docs/contact-discovery.md`.

**Record provenance.** Every `contacts` row stores `source_url` and `discovery_method`. If someone
asks where you got their address, you must be able to answer precisely. A pipeline that can't is
indefensible.

**Data minimisation.** Store what outreach needs — name, email, role title, source — and nothing
more. No scraping social profiles, inferring demographics, or building personal dossiers.

**Support erasure.** `jobhunter purge --email x@y.com` hard-deletes the contact and adds the
address to a suppression list so a later run doesn't rediscover it. The suppression list stores a
hash, not the address, so honouring the request doesn't require retaining the data.

**Lawful basis.** For a personal job search, contacting a named recruiter about a role they're
advertising is a textbook legitimate-interest case: they published a hiring intent, and your
message is directly relevant to it. That basis evaporates if you contact people unrelated to
hiring, or send unsolicited bulk mail. The design constraints below are what keep you inside it.

**Retention.** Contacts with no outreach after ~12 months should be purged. Add a
`jobhunter prune --older-than 365d` command in Phase 3.

---

## Outreach (CAN-SPAM / PECR / CASL)

**There is no send path in Phases 1–3.** The drafter writes rows to `outreach` with
`status='draft'`. A human reads each one and sends it from their own mail client. This isn't
squeamishness — it's the single design decision that most reduces risk, because a human in the
loop is what makes each message genuinely individual rather than a campaign.

Every draft must be:

- **1:1 and job-specific.** It references *this* posting at *this* company and says something
  concrete about why you fit. If a draft would read identically with the company name swapped,
  the drafter is broken.
- **Honestly identified.** Your real name and a real way to reach you.
- **Opt-out-respecting.** One line offering to not follow up. Honour it — that's what the
  `suppressed` flag is for.
- **Non-deceptive.** No fake subject lines, no "re:" on a thread that doesn't exist, no implying
  a prior conversation.

Rate discipline, if you ever add sending: a handful of messages a day from a personal address.
Volume is what converts a job search into a spam operation, both legally and in the eyes of every
spam filter between you and the recipient.

**One follow-up maximum**, and only after ~7 days. Anything beyond that is harassment and it
doesn't work anyway.

---

## SMTP verification ethics

RCPT probing is a grey area. It's widely done and technically just an unfinished SMTP
conversation, but at volume it looks exactly like the reconnaissance step of a spam operation,
and mail providers treat it that way.

- Off by default (`verify_emails=False`).
- Never issue `DATA`. The probe must be structurally incapable of delivering a message.
- Serial, delayed, hard-capped per domain. Stop on first `valid`.
- Cache for ~30 days. Re-probing the same address is pure downside.
- Accept `unknown` gracefully. Google and Microsoft won't tell you, and pressing them just gets
  your IP rate-limited.

If verification feels uncomfortable, leave it off. Tier-1 role addresses need no verification and
are the addresses you actually want.

---

## What this project will not do

Keep this list in mind if a future task seems to require crossing it. If one does, stop and raise
it rather than implementing it.

- Scrape LinkedIn/Indeed or any login-walled source
- Solve or bypass CAPTCHAs, or rotate IPs/proxies to evade rate limits
- Spoof `User-Agent` to impersonate a human browser
- Send email automatically or in bulk
- Harvest contacts unrelated to an actual open role
- Store personal data without a source URL
- Sell, share, or publish the contact database
- Ignore an opt-out or erasure request

---

## Quick self-check before a run

1. Is every source either a public ATS API or a robots-allowed page? 
2. Is the rate limit at or below 1 req/sec per host? 
3. Does every contact row have a `source_url`? 
4. Would each draft embarrass you if the recipient forwarded it to the hiring manager? 
5. Is anything sending automatically? (Correct answer: no.)
