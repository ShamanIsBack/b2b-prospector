# grounded-prospector

[![CI](https://github.com/ShamanIsBack/grounded-prospector/actions/workflows/ci.yml/badge.svg)](https://github.com/ShamanIsBack/grounded-prospector/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![Checked with mypy](https://img.shields.io/badge/mypy-strict-blue.svg)](https://mypy-lang.org/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Find B2B decision-makers at a list of target companies, from public search results —
**without scraping anything, and without letting a language model invent people.**

Give it a list of companies and the roles you care about. It builds Google X-ray queries,
runs them through a search API, and returns a scored, deduplicated CSV of LinkedIn profiles
with every claim traceable to the search result it came from.

```console
$ grounded-prospector run --demo
                                Top 8 prospects
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━┳━━━━━━┳━━━━━━━━┓
┃ Name                        ┃ Headline                 ┃ Agency            ┃ Conf ┃ Review ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━╇━━━━━━╇━━━━━━━━┩
│ Ahmed bin Rashid Al Maktoum │ Head of Incentive Travel │ Dune & Palm Ev... │ 1.00 │   no   │
│ Layla Haddad                │ MICE Manager             │ Dune & Palm Ev... │ 1.00 │   no   │
│ Marco Ferretti              │ Director of Outbound…    │ Falcon Bay Trav…  │ 1.00 │   no   │
│ Sara Okonkwo                │ MICE Consultant          │ Falcon Bay Trav…  │ 0.60 │  yes   │
│ Elena Rossi                 │ -                        │ Majlis Concierge  │ 0.15 │  yes   │
└─────────────────────────────┴──────────────────────────┴───────────────────┴──────┴────────┘
```

## Quickstart — no API key needed

```bash
git clone https://github.com/ShamanIsBack/grounded-prospector
cd grounded-prospector
python -m venv .venv && .venv/Scripts/activate   # or source .venv/bin/activate
pip install -e ".[dev]"

grounded-prospector run --demo
```

`--demo` replays bundled fixtures, so the full pipeline — pagination, deduplication,
scoring, the review gate, CSV export — runs offline with **no credentials at all**. That
is a deliberate acceptance test, not a convenience: if the demo ever needs a key, the
project has failed.

### Running it for real

```bash
cp .env.example .env          # paste a key from https://serper.dev (2,500 free, no card)
cp search.example.yaml search.yaml

grounded-prospector run --dry-run     # see every query and the cost, send nothing
grounded-prospector run               # write out/prospects.csv
```

## How it works

```mermaid
flowchart LR
    A[search.yaml] --> B[X-ray query builder]
    B --> C{SearchProvider}
    C -->|serper| D[Serper.dev<br/>Google organic JSON]
    C -->|gemini| E[Gemini grounding<br/>cited sources only]
    C -->|fixture| F[Recorded JSON<br/>--demo, no key]
    D --> G[cache · retry · rate limit]
    E --> G
    F --> H
    G --> H[Filter: linkedin.com/in only]
    H --> I[Deterministic title parser]
    I --> J[Confidence score<br/>+ review gate]
    J --> K[Canonicalise URL · dedupe]
    K --> L[CSV · JSON · run report]
```

The query for each company looks like this:

```
site:linkedin.com/in/ "Dune & Palm Events" "Dubai" ("MICE" OR "Director" OR …)
    -intitle:"profiles" -inurl:"dir/"
```

## Why it cannot invent people

The obvious way to build this is to ask an LLM "who works at X?" and parse its answer.
That produces confident, well-formatted, **fictional** people — and in B2B outreach a
fabricated name is worse than no result, because it gets emailed.

This tool never does that. It is structural, not a matter of prompt wording:

- **Names come from search-result titles, not from prose.** Public LinkedIn titles follow
  `First Last - Headline - Company | LinkedIn`. `extract.py` parses that with plain string
  rules — pure, synchronous, and covered by unit tests including Gulf name particles
  (`Ahmed bin Rashid Al Maktoum`) and en/em-dash separators.
- **No model output reaches the parser.** With the Gemini backend, only `url_citation`
  annotations become data. The model's prose is stored in a separate `llm_notes` field and
  never read for facts.
- **Every row carries its evidence.** `Raw title`, `Snippet`, `Source query` and
  `LinkedIn URL` all ship in the CSV, so any row can be checked by opening one link.
- **A company mismatch is a hard gate.** If the target company does not appear in the
  result, the row is flagged for review no matter how good the other signals are. Someone
  whose headline merely mentions your target is not a lower-confidence version of the right
  person — they are the wrong person.

### Confidence scoring

| Signal | Weight |
|---|---|
| Target company appears **in the result title** | 0.40 |
| Target company appears **only in the snippet** | 0.20 |
| A target role keyword appears in the headline or snippet | 0.25 |
| Title split cleanly into both a name and a headline | 0.20 |
| Parsed name looks like a person's name | 0.15 |

Below `0.60`, or without a company match **in the title**, the row gets `Needs review = yes`
plus a plain-English list of what was missing.

The title/snippet split is not pedantry. A LinkedIn result title names the person's *current*
employer; a snippet is free text that also contains past roles. On a real 433-prospect run,
96 matches came from titles and 150 from snippets alone — and the snippet-only group included
a profile headlined "Retired Banker". See ADR-004.

**Location is deliberately absent from scoring** — matching on it is exactly how a search for
people *in* Austin returns people *named* Austin. It is exported as a hint column only.

## Backends

| | **serper** (default) | **gemini** | **fixture** |
|---|---|---|---|
| Source | Google organic results as JSON | Grounding with Google Search | Recorded responses |
| Pagination | ✅ `page` (plus `num` on paid plans) | ❌ none available | ✅ replayed pages |
| Snippets | ✅ | ❌ | ✅ |
| Deterministic | ✅ | ❌ model rewrites the query | ✅ |
| Latency | ~200–500 ms | ~2–4 s | instant |
| Cost | 2,500 free, then ~$1/1k | 5,000 free/mo, then ~$14/1k | free |
| Key | `SERPER_API_KEY` | `GEMINI_API_KEY` | none |

```bash
grounded-prospector providers      # print this table live
```

Gemini grounding was the original design and was demoted after measurement, not taste. It
exposes no raw result list — only the handful of sources the model chose to cite, with no
page or offset parameter — so it cannot walk a result set. It is kept as a real second
implementation because a provider abstraction with one backend proves nothing. The full
reasoning is in [`docs/DECISIONS.md`](docs/DECISIONS.md) (ADR-006).

## Output

`out/prospects.csv` (UTF-8 with BOM, so Excel renders non-ASCII names correctly), alongside
`prospects.json` and `run_report.json`.

> **[`INSTRUCTIONS.md`](INSTRUCTIONS.md)** is the operator's runbook: how to probe a new target
> list before spending, which rows to trust and in what order, what each review reason means,
> and how to refine results for free against the cache.

Evidence columns are filled by the tool: name, headline, company from title, target agency,
segment, LinkedIn URL, location hint, confidence, needs-review flag and reasons, SERP
position, snippet, raw title, source query, provider, timestamp.

**The CRM columns — Email, Phone, Business profile, Client segment, Potential rating,
Notes — are deliberately left blank.** This tool finds people; it does not find contact
details. Filling them with guesses would invite someone to email an unverified address.

## Configuration

Two files, split by one rule: **`search.yaml` is what you look for, `.env` is how the tool
runs.** Retargeting never means hunting through environment variables.

### `search.yaml` — the search brief

Everything defining a search lives here, so a whole campaign is one file you can copy, diff
and keep alongside others.

| Field | Default | Purpose |
|---|---|---|
| `location` | *required* | Geographic anchor, quoted into every query |
| `country` / `language` | `ae` / `en` | Serper `gl` / `hl` — biases which results come back |
| `roles` | `[]` | Seniority/function keywords, OR-ed into one group |
| `keywords` | `[]` | Optional second OR-group, AND-ed with `roles` |
| `agencies` | *required* | Companies to search, each with an optional free-text `segment` |
| `max_pages` | `3` | Result pages per company |
| `min_confidence` | `0.0` | Drop rows scoring below this before writing the CSV |

**Retargeting is a single-file edit.** Dubai travel → Warsaw fintech:

```yaml
location: Warsaw
country: pl                        # was ae — otherwise results stay Emirati
language: pl
roles: [CTO, Head of Engineering, VP Engineering]
keywords: [fintech, payments]      # narrows without diluting the role list
agencies:
  - name: Booksy
    segment: saas
```

```bash
grounded-prospector run --search warsaw.yaml --dry-run   # check the queries, spend nothing
```

`--pages` and `--min-confidence` override the brief for a single run; the brief overrides the
built-in defaults. Nothing about the search subject lives in `.env`.

### `.env` — credentials and infrastructure

Keys are read from the environment and are **never** CLI options — arguments leak into shell
history, process listings and CI logs.

| Variable | Default | Purpose |
|---|---|---|
| `SERPER_API_KEY` | – | Serper.dev key |
| `GEMINI_API_KEY` | – | Gemini key, for `--provider gemini` |
| `GP_MAX_QUERIES` | `50` | Hard ceiling per run; an account-level guard, not part of a search |
| `GP_RESULTS_PER_PAGE` | *unset* | Serper page size. **Leave unset on a free plan** — see below |
| `GP_CONCURRENCY` | `3` | Simultaneous in-flight searches |
| `GP_RATE_LIMIT_PER_MINUTE` | `30` | Token-bucket pacing |
| `GP_CACHE_TTL_HOURS` | `168` | Response cache lifetime |

Responses are cached in SQLite keyed by provider, page size, country, query and page
number, so re-running a refined search costs nothing for the parts that did not change.

> **Free Serper plans and `num`.** Serper rejects `num` above 10 when the query uses search
> operators — which every X-ray query does — with
> `400 Query pattern not allowed for free accounts`. So `num` is not sent by default and
> Google picks the page size (~10 results). `page` is unaffected, so use `--pages` to fetch
> more. On a paid plan, set `GP_RESULTS_PER_PAGE=100` to get the same results in fewer
> billed queries.

## Compliance

- **No scraping.** No LinkedIn page is ever fetched. The tool reads a search index through
  the vendor's own API, which is what those APIs are for.
- **No Terms-of-Service violation.** An earlier iteration of this project drove a real
  browser against Google search results. That works, and it breaks Google's ToS
  (`robots.txt: Disallow: /search`). It was deleted rather than shipped — see ADR-001.
- **Public data only**, and only what a search engine already publishes in its results.
- **GDPR.** Names and job titles are personal data. An EU-based operator processing them
  for B2B outreach relies on *legitimate interest* (Art. 6(1)(f)), which carries obligations:
  a balancing test, disclosure of the source in first contact, and honouring objections.
  This tool produces a research list, not consent.

## Limitations

Stated plainly, because a prospecting tool that oversells itself wastes someone's week.
Numbers below are from a real 14-company, 433-prospect run.

- **Recall is not exhaustive.** You get what the search index surfaces, not an org chart.
  Coverage is also wildly uneven between companies: that run returned 48 prospects for one
  agency and 1 for another. A thin result usually means a small LinkedIn footprint, not a
  failed search.
- **About 22% of rows are ready to contact** (96 of 433); the rest carry a review reason.
  That ratio is honest scoring, not a defect — and it inverts if you set `location`, which
  trades recall for precision.
- **Company-name matching accepts supersets.** "Al Noor Majlis Concierge, LLC" contains
  every token of "Majlis Concierge", so its staff pass the gate for that target.
- **Dropping `location` widens geography too.** Global brands then return staff from any
  office — a real run surfaced Ivory Key Club USA and Silk Lantern Journeys India alongside the
  Dubai teams. Use the `Location hint` column, or set `location` and accept lower recall.
- **Job titles go stale.** A snippet reflects whenever the page was last indexed.
- **The same person can hold two LinkedIn profiles.** Deduplication is by profile URL, which
  cannot merge genuinely distinct URLs; six such pairs appeared in that run.
- **Company names must be right.** `FalconBayTravel` returned nothing under any query shape;
  `Falcon Bay Travel` returned a full page. Check a thin result before blaming the index.

## Development

```bash
ruff check . && ruff format --check .
mypy --strict src
pytest --cov
```

255 tests, 97% coverage, `mypy --strict` clean. The suite runs entirely offline against
recorded fixtures — there is no network in CI and no key required to contribute.

The demo fixtures contain **fabricated** people and companies. Shipping a real recording
would publish real individuals' personal data to no purpose, so a test asserts that only
the known-fictional profile slugs ever appear in them.

## Project background

Built for a Polish agritourism property seeking B2B partners abroad. It
went through three abandoned approaches
before this one — DuckDuckGo scraping (hard-blocked), Google scraping (works, violates
ToS), and the Google Custom Search API (closed to new customers, retiring January 2027).
Each is written up in [`docs/DECISIONS.md`](docs/DECISIONS.md).

## License

MIT — see [LICENSE](LICENSE).
