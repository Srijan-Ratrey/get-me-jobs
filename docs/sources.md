# Job Source Reference

Endpoints, response shapes, and field mappings for each supported source.

> **Verify before you parse.** These shapes come from documentation that drifts. Fetch one live
> response, save it to `tests/fixtures/`, diff it against what's below, and trust the live
> response. Correct this doc when they disagree.

---

## The adapter contract

Every source implements the same protocol. Adding a new ATS should be ~40 lines.

```python
class JobSource(Protocol):
    name: str
    def matches(self, target: Target) -> bool: ...
    async def fetch(self, client: PoliteClient, target: Target) -> list[RawJob]: ...
```

Rules for all adapters:

- Return `RawJob` objects. Never touch the DB, never normalize titles — `db.upsert_job` owns that.
- Take `PoliteClient` as a parameter. Never construct your own HTTP client.
- Raise `SourceUnavailable` on a failure the caller should log and skip past.
- Strip HTML from descriptions into plain text before returning. Descriptions feed both the
  scorer and the contact scraper, and both want text.
- Absolute URLs only. Resolve relative hrefs against the page you found them on.

`RawJob` fields: `source`, `external_id`, `title`, `location`, `description`, `url`,
`posted_at`, `company_name`.

---

## Greenhouse

Public, stable, no auth, no rate limit published (still go through `PoliteClient`).

```
GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true
```

`content=true` inlines the full description and saves you N extra requests. Without it you'd
need `GET /v1/boards/{token}/jobs/{id}` per job.

```json
{
  "jobs": [{
    "id": 4567890,
    "internal_job_id": 1234567,
    "title": "Senior Backend Engineer",
    "updated_at": "2026-07-14T09:12:33-04:00",
    "first_published": "2026-03-06T04:36:13-05:00",
    "requisition_id": "R-482",
    "company_name": "Acme",
    "location": { "name": "Berlin, Germany" },
    "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/4567890",
    "content": "&lt;p&gt;We are looking for...&lt;/p&gt;",
    "departments": [{ "id": 22, "name": "Engineering" }],
    "offices":     [{ "id": 11, "name": "Berlin", "location": "Berlin, Germany" }],
    "metadata": [{ "id": 1, "name": "Requisition Type", "value": "FTE - Regular" }],
    "data_compliance": [{ "type": "gdpr", "requires_consent": false }],
    "employment": "employment_required",
    "language": "en",
    "application_deadline": null
  }],
  "meta": { "total": 42 }
}
```

| RawJob | Source |
|---|---|
| `external_id` | `str(id)` |
| `title` | `title` |
| `location` | `location.name` |
| `description` | `content` — **HTML-entity-escaped**, so `html.unescape()` *then* strip tags |
| `url` | `absolute_url` |
| `posted_at` | `first_published`, falling back to `updated_at` |
| `company_name` | `company_name` |

**Gotchas.** The double-encoded `content` is the classic mistake — unescape before parsing.
Board tokens are the slug in `job-boards.greenhouse.io/{token}`; older boards use
`boards.greenhouse.io/{token}`.

**`first_published` is the field you want for `posted_at`** — verified present on 69/69 jobs of a
live board (2026-07). An earlier version of this doc said to use `updated_at` and warned it was
last-update rather than first-post; that warning is still true of `updated_at`, but
`first_published` makes it moot. Keep `updated_at` only as a fallback.

`location.name` is free text a recruiter typed, and **one board mixes spellings of the same city**
— PhonePe's live board returns both `"Bangalore"` and `"Bengaluru"`. Any location matching has to
alias these or it will silently drop half the postings in a city. `offices[].location` is coarser
(`"Karnataka, India"`) and often `null`, so it is a poor substitute.

---

## Lever

```
GET https://api.lever.co/v0/postings/{company}?mode=json
```

Returns a bare JSON **array**, not an object. Optional filters: `&location=`, `&team=`,
`&commitment=`, `&department=`, `&group=team` (changes shape to grouped — don't use it).

```json
[{
  "id": "a1b2c3d4-0000-4444-8888-abcdef123456",
  "text": "Senior Backend Engineer",
  "categories": {
    "commitment": "Full-time",
    "department": "Engineering",
    "location": "Berlin",
    "team": "Platform",
    "allLocations": ["Berlin", "Remote - EU"]
  },
  "description": "<p>About the role</p>",
  "descriptionPlain": "About the role",
  "lists": [{ "text": "Requirements", "content": "<li>5+ years</li>" }],
  "additional": "<p>Benefits</p>",
  "additionalPlain": "Benefits",
  "opening": "",
  "openingPlain": "",
  "descriptionBody": "<div>About the role</div>",
  "descriptionBodyPlain": "About the role",
  "country": "IN",
  "hostedUrl": "https://jobs.lever.co/acme/a1b2c3d4",
  "applyUrl": "https://jobs.lever.co/acme/a1b2c3d4/apply",
  "createdAt": 1751030400000,
  "workplaceType": "remote"
}]
```

| RawJob | Source |
|---|---|
| `external_id` | `id` |
| `title` | `text` — **not** `title` |
| `location` | `categories.location` |
| `description` | `descriptionPlain` + each `lists[].text`/`content` + `additionalPlain` |
| `url` | `hostedUrl` |
| `posted_at` | `createdAt` — **epoch milliseconds**, divide by 1000 |

**Gotchas.** `descriptionPlain` alone omits the requirements — they live in `lists[]`, and
requirements are exactly what the scorer needs. Concatenate all three parts.
`workplaceType == "remote"` is a more reliable remote signal than string-matching the location —
but **compare case-insensitively**: a live board returns lowercase `"onsite"` / `"hybrid"`, while
Ashby returns CamelCase `"OnSite"` for the same concept.

Undocumented fields present on a live board (2026-07), none of them needed but worth knowing so
they are not mistaken for the real ones: `descriptionBody` / `descriptionBodyPlain` (the body
without the opening paragraph — `descriptionPlain` already includes it), `opening` /
`openingPlain` (empty on all 49 postings checked), and `country` (ISO-2, e.g. `"IN"`).
`categories.allLocations` was present on 49/49, formatted as `["Bangalore, Karnataka"]` — note
this is a *different* spelling of the city than `categories.location` may use.

---

## Ashby

```
GET https://api.ashbyhq.com/posting-api/job-board/{job_board_name}?includeCompensation=true
```

```json
{
  "apiVersion": "1",
  "jobs": [{
    "id": "8f7e6d5c-1111-2222-3333-444455556666",
    "title": "Senior Backend Engineer",
    "department": "Engineering",
    "team": "Platform",
    "employmentType": "FullTime",
    "location": "Berlin, Germany",
    "secondaryLocations": [{
      "location": "Remote - EU",
      "address": { "postalAddress": {
        "addressLocality": "Munich", "addressRegion": "Bayern", "addressCountry": "Germany" } }
    }],
    "address": { "postalAddress": {
      "addressLocality": "Berlin", "addressRegion": "Berlin", "addressCountry": "Germany" } },
    "publishedAt": "2026-07-01T12:00:00.000Z",
    "isListed": true,
    "isRemote": false,
    "workplaceType": "OnSite",
    "shouldDisplayCompensationOnJobPostings": false,
    "descriptionHtml": "<p>...</p>",
    "descriptionPlain": "...",
    "jobUrl": "https://jobs.ashbyhq.com/acme/8f7e6d5c",
    "applyUrl": "https://jobs.ashbyhq.com/acme/8f7e6d5c/application",
    "compensation": { "compensationTierSummary": "€80K – €110K" }
  }]
}
```

| RawJob | Source |
|---|---|
| `external_id` | `id` |
| `title` | `title` |
| `location` | `location`, plus `secondaryLocations[].location` joined |
| `description` | `descriptionPlain` |
| `url` | `jobUrl` |
| `posted_at` | `publishedAt` |

**Gotchas.** **Filter out `isListed == false`** — those are unlisted/internal postings that
should not surface. On a live board (2026-07) every posting came back `isListed: true`, so the
public endpoint appears to filter already — keep the check anyway, since relying on an
undocumented server-side filter to protect against surfacing internal postings is not a trade
worth making.

`isRemote` is authoritative; prefer it over keyword detection. `workplaceType` is CamelCase here
(`"OnSite"`) where Lever uses lowercase — compare case-insensitively if you use it at all.

`secondaryLocations` entries are **richer than a bare `{location}`**: each also carries an
`address.postalAddress`. Only 3/63 postings on the live board had any, so this is easy to get
wrong from a small sample. The posting also has a top-level `address.postalAddress` with
`addressLocality` / `addressRegion` / `addressCountry`, which is structured where `location` is
free text — useful if you ever need to normalise cities properly.

---

## Workable

```
GET https://apply.workable.com/api/v1/widget/accounts/{subdomain}?details=true
```

```json
{
  "name": "Acme",
  "description": "...",
  "jobs": [{
    "id": "ABC123DEF4",
    "title": "Senior Backend Engineer",
    "shortcode": "ABC123DEF4",
    "code": "R-482",
    "employment_type": "Full-time",
    "telecommuting": true,
    "department": "Engineering",
    "url": "https://apply.workable.com/acme/j/ABC123DEF4",
    "shortlink": "https://acme.workable.com/j/ABC123DEF4",
    "application_url": "https://apply.workable.com/acme/j/ABC123DEF4/apply",
    "published_on": "2026-07-01",
    "created_at": "2026-07-01",
    "country": "Germany", "city": "Berlin", "state": null,
    "description": "<p>...</p>",
    "requirements": "<ul><li>5+ years</li></ul>",
    "benefits": "<p>...</p>"
  }]
}
```

| RawJob | Source |
|---|---|
| `external_id` | `shortcode` |
| `title` | `title` |
| `location` | join non-null `city`, `state`, `country` |
| `description` | `description` + `requirements` + `benefits`, tags stripped |
| `url` | `url` |
| `posted_at` | `published_on` (date only, no time) |

**Gotchas.** `telecommuting` is the remote flag. Without `?details=true` the description fields
are absent entirely. Some accounts only respond on the v3 endpoint —
`POST https://apply.workable.com/api/v3/accounts/{subdomain}/jobs` with `{}` as the body — so
fall back to it on a 404.

**Verified against a live account (2026-07-31), and the shape above is optimistic:**

- **There is no `id` field.** `shortcode` is the only identifier, which is what `external_id`
  maps to anyway — but do not reach for `job["id"]`, it raises `KeyError`.
- **`requirements` and `benefits` were absent**, with only `description` present, even with
  `?details=true`. Treat all three as optional and concatenate whichever exist, or a missing key
  takes the adapter down.
- Undocumented but present and useful: **`experience`** (a free seniority signal such as
  `"Mid-Senior level"`, like SmartRecruiters' `experienceLevel` — prefer it over parsing the
  title), plus `education`, `function`, `industry`, and a structured
  `locations: [{country, countryCode, city, region, ...}]` array alongside the flat
  `city`/`state`/`country` fields.
- Account subdomains are hard to guess: 12 of 25 plausible slugs 404'd and 13 returned
  `{"name": ..., "description": ..., "jobs": []}` — a valid account with nothing open. An empty
  `jobs` array is not an error, so do not treat it as one.

---

## SmartRecruiters — NOT USABLE, robots.txt disallows it

> **Do not build this adapter.** Checked 2026-07-31: `api.smartrecruiters.com/robots.txt` is
>
> ```
> User-agent: LinkedInBot
> Allow: /v1/companies/
>
> User-agent: *
> Disallow: /
> ```
>
> The postings API is disallowed for every user-agent except LinkedInBot. With
> `respect_robots=True` — which `docs/compliance.md` requires and which nothing in this codebase
> may switch off — every request raises `RobotsDisallowed`. All 25 candidate companies probed
> failed on the robots check alone, before a single request was issued.
>
> Claiming to be LinkedInBot to collect the `Allow` is exactly the User-Agent spoofing that
> `docs/compliance.md` prohibits, and it would convert a technical measure into a circumvented
> one — the worst version of this argument to have to make. There is no compliant path here.
>
> The endpoint documentation below is retained only so nobody rediscovers it and assumes it was
> an oversight.

Two calls: a paginated list, then a detail fetch per posting for the description.

```
GET https://api.smartrecruiters.com/v1/companies/{company_id}/postings?limit=100&offset=0
GET https://api.smartrecruiters.com/v1/companies/{company_id}/postings/{posting_id}
```

```json
{
  "offset": 0, "limit": 100, "totalFound": 137,
  "content": [{
    "id": "743999900000123",
    "name": "Senior Backend Engineer",
    "uuid": "aaaa-bbbb",
    "refNumber": "REF-1",
    "company": { "identifier": "Acme", "name": "Acme" },
    "location": { "city": "Berlin", "region": "BE", "country": "de", "remote": false },
    "department": { "label": "Engineering" },
    "typeOfEmployment": { "label": "Full-time" },
    "experienceLevel": { "id": "senior_professional", "label": "Senior" },
    "releasedDate": "2026-07-01T10:00:00.000Z",
    "ref": "https://api.smartrecruiters.com/v1/companies/Acme/postings/743999900000123"
  }]
}
```

Detail response adds `jobAd.sections.{companyDescription,jobDescription,qualifications,additionalInformation}.text`
(HTML) and `applyUrl`.

| RawJob | Source |
|---|---|
| `external_id` | `id` |
| `title` | `name` — **not** `title` |
| `location` | `location.city`/`region`/`country` |
| `description` | detail: concatenate `jobAd.sections.*.text` |
| `url` | `https://jobs.smartrecruiters.com/{company_id}/{id}` |
| `posted_at` | `releasedDate` |

**Gotchas.** Paginate on `totalFound` vs `offset + limit`; `limit` caps at 100.
`experienceLevel.label` is a free seniority signal — use it instead of parsing the title.
Detail fetches multiply your request count by the posting count, so let the cache do its job
and only refetch details for jobs that are new.

---

## Generic careers page crawler

The fallback when a company isn't on a supported ATS, and the way you discover which ATS they
*are* on.

**Step 1 — Fingerprint.** Fetch `careers_url` and look for these markers in the HTML (links,
iframe `src`, inline script config). If one hits, hand off to the real adapter with the token
extracted from the URL — always better than scraping.

| Marker in HTML | ATS | Token location |
|---|---|---|
| `boards.greenhouse.io`, `job-boards.greenhouse.io`, `grnhse` | greenhouse | path segment after host |
| `jobs.lever.co` | lever | first path segment |
| `jobs.ashbyhq.com` | ashby | first path segment |
| `apply.workable.com`, `*.workable.com` | workable | subdomain or path |
| `jobs.smartrecruiters.com`, `careers.smartrecruiters.com` | smartrecruiters | first path segment |
| `*.recruitee.com` | recruitee | subdomain |
| `*.jobs.personio.de`, `.personio.com` | personio | subdomain (XML feed at `/xml`) |
| `*.teamtailor.com` | teamtailor | subdomain |
| `*.bamboohr.com/careers` | bamboohr | subdomain |
| `*.myworkdayjobs.com` | workday | *not supported — log and skip* |

Log every unrecognised fingerprint. That log is your backlog of which adapter to write next.

**Step 2 — Detect SPA shells.** Compute the text-to-markup ratio. Under ~5% text, or fewer than
~200 characters of visible text on a page that should list jobs, means client-side rendering.
Retry with Playwright if the `spa` extra is installed; otherwise record
`SourceUnavailable("spa")` and move on.

**Step 3 — Extract.** In priority order:

1. **JSON-LD.** Look for `<script type="application/ld+json">` with `@type: JobPosting`. This is
   a schema.org standard many careers pages emit, and it gives you `title`, `datePosted`,
   `jobLocation`, `description`, `hiringOrganization` cleanly. Always try this first.
2. **Repeated-structure heuristic.** Find the container holding the most sibling elements whose
   links match `/job|career|position|opening|vacanc/i`. Treat each sibling as a posting; take
   link text as title and look for a nearby location string.
3. **Link harvest.** Collect anchors matching a job-URL pattern, then fetch each and parse the
   detail page. Cap at ~50 links per company to avoid crawling an entire site.

Every extraction path must set `external_id` to a stable value — the URL path is fine.
Non-deterministic IDs break dedup across runs.

---

## Adding a new ATS

1. Find the public JSON endpoint. Most ATSs have an undocumented one powering their own widget —
   open a customer's board with DevTools Network open and watch what it calls.
2. Save a real response to `tests/fixtures/{ats}.json`.
3. Write the adapter, mapping to `RawJob`.
4. Write a parser test against the fixture asserting field-by-field. No network in tests.
5. Add the fingerprint marker to the table above.
