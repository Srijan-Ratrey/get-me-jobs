# Test fixtures

Real captured responses from live ATS boards, which is what makes the parser
tests worth having: a passing test here is a parser that works on production
data, not on a shape someone hand-wrote from documentation. Several corrections
in `docs/sources.md` came from diffing these against what the docs claimed.

**They are trimmed to five postings each.** The captures were 2.2MB and 182 full
job descriptions across three named companies; republishing three companies'
entire job boards in this repository is more than the tests need. Trimming to
five keeps the file under 100KB each.

Postings were not picked at random — each file keeps the shapes the parsers
branch on:

| Fixture | Kept because |
|---|---|
| `greenhouse.json` | Both `"Bangalore"` and `"Bengaluru"` spellings, which one live board really does mix; and a posting whose `offices[].location` is `null` |
| `lever.json` | A posting with `lists[]` (where the requirements live), one with `additionalPlain`, and a `hybrid` `workplaceType` |
| `ashby.json` | A posting with non-empty `secondaryLocations`, the field the documentation described incompletely and which only 3 of 63 postings had |
| `workable.json` | The single posting the live account had, which is also what proved `requirements` and `benefits` can be absent entirely |

Tests assert exact counts, so re-capturing a fixture means updating the
`len(jobs) == 5` assertions in `test_sources.py` to match.

To re-capture, fetch through `PoliteClient` (never `httpx` directly) and diff the
result against `docs/sources.md` before trusting either.
