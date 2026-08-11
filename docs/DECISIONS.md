# Architecture decision records

Short records of the decisions that shaped this tool, including the three approaches that
were built and abandoned. They are kept because the discarded options explain the shape of
what remains — and because a decision log with no reversals in it is usually a decision log
that was written afterwards.

---

## ADR-001 — Do not scrape search engines

**Status:** accepted · **Date:** 2026-07-29

### Context

The first implementation drove a headless browser (Playwright) against DuckDuckGo's HTML
endpoint, parsing result headings for LinkedIn profiles.

It did not work, and then it worked in a way that was worse than not working:

1. **DuckDuckGo hard-blocked it.** A live diagnostic showed the first request receiving a
   202 bot-challenge interstitial, and every subsequent request from the same session — including a
   plain-text query with no `site:` operator at all — receiving a blanket 403. This is
   IP/session-level detection escalating within seconds. Tuning the user agent, locale or
   delays does not move it.
2. **DuckDuckGo's LinkedIn coverage was too thin anyway.** Manual side-by-side testing of
   the same query showed Google returning matches where DuckDuckGo returned none.
3. **Google worked — by violating its Terms of Service.** A persistent real-Chrome profile
   with manual CAPTCHA solving did return results. Google's `robots.txt` disallows
   `/search`, and its ToS prohibits automated access outside an approved API.

### Decision

No search-engine scraping, in any form. Use a vendor API whose purpose is programmatic
access.

### Consequences

The free path disappears and the tool now needs an API key. In exchange it is stable,
paginated, fast, and publishable — the browser-driving version could never have gone into a
public portfolio repository, because the honest README for it would have opened with "this
violates Google's Terms of Service."

---

## ADR-002 — Do not build on the Google Custom Search JSON API

**Status:** accepted · **Date:** 2026-08-10

### Context

Custom Search JSON API was the obvious compliant replacement for ADR-001: it is Google's
own, officially documented, and priced for small volumes (100 free queries/day).

Verification before writing any code found:

- The API is **closed to new customers**. New Cloud projects return
  `403 PERMISSION_DENIED — "This project does not have the access to Custom Search JSON API"`
  **even when the console shows the API as Enabled**. The gate sits below the enable toggle.
  Google staff confirmed this on their own developer forum, along with reports of existing
  users losing access.
- The API **retires on 1 January 2027**.
- Creating a new "search the entire web" Programmable Search Engine was **discontinued in
  August 2025**. Site-restricted engines (≤50 domains) are still creatable, which would have
  been sufficient here since every query is `site:linkedin.com/in/` — but that only
  mattered if the first point resolved favourably.

The project's own Cloud account might have been grandfathered; this was never tested.

### Decision

Skip it entirely, without testing whether this particular account has access.

### Consequences

The deciding argument was not technical. **A portfolio project nobody else can run has no
portfolio value.** Even if grandfathered access had worked here, every reviewer cloning the
repository would hit a 403 they cannot fix, on an API that dies within months. Personal
access is not a foundation to build on.

---

## ADR-003 — The language model is a search dispatcher, never a source of facts

**Status:** accepted · **Date:** 2026-08-10

### Context

With an LLM in the pipeline, the tempting design is to ask "who are the decision-makers at
X?" and parse the answer, or to hand it the raw results and ask for structured JSON.

Both fabricate. Not occasionally — routinely, and *plausibly*: a real first name attached to
a job title from an adjacent result, formatted identically to a correct row. In B2B outreach
that is worse than an empty result, because an empty result gets ignored and a fabricated
person gets emailed.

### Decision

The model may decide *what to search for*. It may never be the source of a name, title,
employer or URL.

Concretely:

- Only `url_citation` annotations become data — a URL and the title of the page behind it.
- `extract.py` parses names and headlines from those titles with pure, unit-tested string
  rules.
- The model's prose is stored in `llm_notes` and never read by anything downstream.
- Every exported row carries `Raw title`, `Snippet`, `Source query` and the profile URL, so
  any row can be verified by opening one link.

### Consequences

Recall is lower than an LLM-summarised list would appear to give, because the tool only
reports what a page title actually said. That difference is the fabrication rate, made
visible. This constraint survived the backend change in ADR-006 unaltered — it is a property
of the pipeline, not of any one vendor.

---

## ADR-004 — Company match is a gate, not a weight

**Status:** accepted · **Date:** 2026-08-10

### Context

Confidence started as a weighted sum of four signals. A unit test caught the flaw: a real
person with a matching role at a *different* company scored exactly `0.60`, the review
threshold — and `0.60 < 0.60` is false, so it passed review and would have been emailed as a
contact at a company they do not work for.

The general failure is well documented in sourcing: searching for engineers "in Austin"
returns people *named* Austin. The same trap applies to any company name made of ordinary
words.

### Decision

If the target company does not appear in the result title or snippet, the row is flagged for
review regardless of its score. Location is excluded from scoring entirely and exported as a
hint column only.

### Consequences

More rows are flagged, including some correct ones whose title truncated the employer. That
is the right direction to err: a flagged correct row costs a glance, an unflagged wrong row
costs a misdirected email to a real person.

### Amendment (2026-08-10) — *where* the company name appears is the gate

The first full production run, 433 prospects across 14 agencies, showed the gate was weaker
than it looked. Company matching searched the title **and** the snippet, and of the 246 rows
that passed, only **96 matched in the title**; **150 matched on the snippet alone**.

A snippet is free text containing employment *history*. Among the rows it cleared for
outreach was a profile headlined "Retired Banker", and an "Operations Manager at Charity
Challenge" — both counted as current staff of the target agency because the agency appeared
somewhere in their snippet text.

A result *title* states the current employer. A snippet does not. So the gate now requires a
title match; a snippet-only match earns partial credit (0.20 rather than 0.40) so it still
outranks a non-match, and carries the reason "appears only in the result snippet, not the
title -- this may be a former employer". Ready-to-contact rows fell from 246 to 96, which is
the fabrication in the earlier number made visible.

Re-scoring the whole run against the response cache cost **zero queries and 0.1 seconds** —
the cache stores raw API responses, so scoring is free to iterate on after the data is
collected.

**Known limitation this does not solve:** token-subset matching still accepts a *superset*
company name. "Al Noor Majlis Concierge, LLC" contains every token of "Majlis Concierge",
so a retired banker there passes the title gate. Tightening it would need the target's legal
name, which the brief does not have. Documented rather than guessed at.

**Amended by [ADR-009](#adr-009--a-target-may-be-a-phrase-not-only-a-company)** (2026-08-11):
the gate now applies to any target, and its *meaning* depends on the target's kind — for a
company a title match still means "works there now", for a phrase it means "describes
themselves this way". The arithmetic here is unchanged; only the wording of the reason forks.
ADR-009 also adds exclusion terms, which close the gate on rows this ADR would have opened.

---

## ADR-005 — A provider abstraction, with more than one real implementation

**Status:** accepted · **Date:** 2026-08-10

### Context

Three backends had already been considered and two discarded before a line of pipeline code
existed (ADR-001, ADR-002). Vendor churn was clearly the norm here, not an edge case.

### Decision

Define a `SearchProvider` protocol returning normalised `SearchHit`s. Keep all parsing,
scoring and deduplication downstream of it, so they are identical for every backend.
Declare backend differences explicitly through a `Capabilities` record rather than letting
them surface as surprising behaviour.

### Consequences

The pipeline asks `capabilities.supports_pagination` instead of discovering at page two that
a backend silently re-serves page one and bills for it.

An abstraction with a single implementation is speculative design, and would have been fair
to criticise as such. It now has three: `serper`, `gemini`, and `fixture` — the last being
load-bearing rather than a stub, since it powers both `--demo` and the entire offline test
suite. ADR-006 is what turned the second one from a hypothetical into a real one.

---

## ADR-006 — Serper.dev is the primary backend; Gemini grounding is secondary

**Status:** accepted · **Date:** 2026-08-10 · **Supersedes:** the original choice of grounding as primary

### Context

The tool was first built on the Gemini API's *Grounding with Google Search*. The reasoning
was defensible: first-party Google, open to new signups, generous free tier, and officially
sanctioned — a direct answer to ADR-001 and ADR-002.

External review challenged it. Most of the challenge held up, though not all of it was
established the same way — the third column records how each verdict was reached, because a
figure taken from a price list and a fact read off a type signature do not deserve equal
weight:

| Claim | Verdict | How it was established |
|---|---|---|
| No pagination; hard result ceiling | **Correct, and decisive.** `GoogleSearchResult` in the SDK exposes only `search_suggestions` — no result array. The only links available are the sources the model chose to cite. There is no `num`, `page` or offset parameter. Whatever it cites is the ceiling. | Inspected the installed SDK's own types. |
| Non-deterministic | **Correct.** The model rewrites the query, so identical runs can differ. | Follows from the response shape: `google_search_call` steps record the queries the model chose, which need not be the one it was given. `_interaction.executed_queries()` exposes them. |
| Cost and latency at scale | **Accepted.** ~$14/1k grounded searches at ~2–4 s each, versus ~$1/1k at ~200–500 ms. | **Vendor-published pricing and latency, not measured here.** Serper's side has since been borne out in practice; the grounding side has not been benchmarked, and would need a paid key to be. |
| Risk of hallucination during extraction | **Rejected.** It describes a design where an LLM extracts fields from prose. ADR-003 rules that out structurally: only citation URLs and titles become data. | Structural — a property of the pipeline, so there is nothing to measure. |

The cost row is the weakest of the four and it did not need to be strong. The pagination
finding decides the question on its own: no amount of favourable pricing buys a result set the
API never exposes. Had cost been the *only* objection, it would have been worth measuring
before acting on it.

The same review also proposed collapsing the project into a single class returning a
DataFrame. That was declined — it would have discarded the test suite, type checking, cache,
retry and rate limiting for no gain in capability.

### Decision

`serper` becomes the default. Grounding stays as a fully supported second backend, declaring
`supports_pagination=False` so the pipeline stops after one page instead of paying for
repeats.

### Consequences

Pagination, snippets, rank and determinism arrive together; snippets in particular supply the
location hint and let company matching succeed when a title truncates the employer.

About 85% of the codebase was unaffected — `extract.py`, `urls.py`, `infra/*` and their tests
did not change at all. That is the abstraction from ADR-005 doing the job it was built for,
and it is the reason this reversal cost roughly a day rather than a rewrite.

Three of four objections landing is a normal outcome for a first architecture, and the one
that did not land was worth defending rather than conceding.

---

## ADR-007 — Let the search API choose its own page size

**Status:** accepted · **Date:** 2026-08-10 · **Source:** live testing, not documentation

### Context

The Serper adapter was written to request `num=100`, on the reasoning that one large page is
cheaper than ten small ones — each request is billed the same regardless of size.

The first live call returned `400 Bad Request`. Isolating the parameters gave a precise
answer:

| Request | Result |
|---|---|
| X-ray query alone | 200, 10 results |
| `+ page=1` / `page=2` | 200 |
| `+ num=10` | 200 |
| `+ num=20` / `num=100` | **400 — "Query pattern not allowed for free accounts"** |

Free accounts cap `num` at 10 *when the query uses search operators* — which every X-ray
query does, by definition. `page` is unrestricted on every plan.

Two further findings from the same session:

- Omitting `num` entirely returned **10** results where `num=10` returned **7**. Letting the
  API choose is not merely safer, it was better.
- The original code called `raise_for_status()` before reading the body, discarding
  `{"message": "Query pattern not allowed for free accounts."}` — the one part of the
  response that said what to change. Diagnosis took a manual probe that the error message
  should have made unnecessary.

### Decision

Do not send `num` unless explicitly configured. Paginate with `page`. Surface the API's own
error message on every failure, and translate this specific one into advice naming the
setting to change.

### Consequences

The default configuration now works on the free plan, which is what the README promises any
reader can do. Paid users set `GP_RESULTS_PER_PAGE=100` and get the same results in fewer
billed queries.

The wider lesson is about testing: a mock transport proves the code handles the response
shape you *imagined*. Only a real call proves the request is one the vendor will accept.
Three defects surfaced on the first live run and none of them were reachable through
`httpx.MockTransport` — this one, a Windows console that could not encode the Arabic
right-to-left marks in genuine Dubai profile titles, and a cost estimate derived from
attempted rather than billed queries, which charged for a fully cached run.

---

## ADR-008 — The search subject is configuration; the environment is infrastructure

**Status:** accepted · **Date:** 2026-08-10

### Context

Retargeting the tool — a different country, sector or seniority — meant touching three
places, and one setting could not be reached at all:

| Setting | Where it lived | Why that was wrong |
|---|---|---|
| `location`, `roles`, `agencies` | `agencies.yaml` | fine |
| `country`, `language` | `GP_COUNTRY`, `GP_LANGUAGE` | These decide *which* results a search engine returns. A Warsaw search silently kept `gl=ae` unless you knew to look in `.env`. |
| `max_pages` | `GP_MAX_PAGES` | Search depth, split from the rest of the brief |
| `min_confidence` | CLI flag only | Could not be saved with a campaign at all |
| `keywords` | nowhere | `build_xray_query()` accepted the parameter, the model had no field, and the pipeline never passed it. A passing unit test covered it — which is precisely how dead code survives review. |

### Decision

One rule decides where a setting goes:

- **`search.yaml`** — anything describing *what* you are looking for or *how hard*: location,
  country, language, roles, keywords, agencies, `max_pages`, `min_confidence`.
- **`.env`** — secrets and infrastructure: API keys, provider, model, cache, concurrency,
  rate limit, timeouts, and the `max_queries` spend ceiling (an account-level guard, not a
  property of any one search).
- **CLI** — one-off overrides. Precedence is **flag > brief > built-in default**.

`country` and `language` were *deleted* from `Settings` rather than kept as a fallback
override. A second place to set them is the exact problem being solved, and a silent
precedence chain between a file and an environment variable is worse than either alone.
`results_per_page` stayed in the environment because it is a property of your Serper *plan*.

The file was renamed `agencies.yaml` → `search.yaml`, since a file called "agencies" that
also sets country and search depth misleads anyone reading the repo cold. A missing
`search.yaml` next to an existing `agencies.yaml` produces migration instructions rather than
a bare "not found".

### Consequences

A campaign is now one copyable, diffable file, and `--dry-run` shows the full effect of an
edit before spending anything. Wiring `keywords` through was a two-line change once the field
existed — the work was never the plumbing, it was noticing that a green test suite was
guarding a code path no user could reach.

LinkedIn deliberately stays hardcoded. The `site:` filter, the URL canonicaliser and the
title parser are coupled on purpose (ADR-003): deterministic extraction only works because it
knows one source's exact title format. Exposing the site filter alone would let someone write
a valid-looking brief that silently produces garbage.

---

## ADR-009 — A target may be a phrase, not only a company

**Status:** accepted · 2026-08-11

### Context

The tool was built to answer "who works at these companies", which assumes you can name the
companies. That assumption held for two campaigns and then broke on the third: Polish wedding
planners are a long tail of sole traders whose businesses are personal brands. There is no
list to hand the tool.

While running that campaign we noticed the query builder never inspects the target text — it
interpolates it as a quoted phrase and nothing more. So a job-title phrase can be put in the
company slot, and the query becomes `site:linkedin.com/in/ "konsultant ślubny" (roles…)`. It
worked, and it produced the only usable rows in the run.

Used that way, the confidence gate quietly changes meaning. For a company, a match in the
result title means *works there now*, because that is what a LinkedIn title states (ADR-004).
For a phrase, the same match means *describes themselves this way* — which, for a sole trader,
is the more useful fact.

### Decision

Make it explicit rather than clever. A target carries a `kind` of `company` (default) or
`phrase`. The kind changes nothing about the query or the arithmetic — the evidence is
identical — and changes only how a match is *explained*, plus what it is checked against.

Three things forced their way in once it was a real feature rather than a trick:

**1. The review reasons had to fork.** A company found only in a snippet is probably a former
employer, so the reviewer checks the profile's history. A phrase found only in a snippet means
the person did not choose those words, so the reviewer judges the words they did choose. Using
the company wording for a phrase was actively misleading — it invited people to look for an
employment record that was never claimed.

**2. Exclusion terms became necessary, not a nicety.** A phrase is a substring of longer
phrases that mean something else entirely. Searching `"mistrz ceremonii"` (master of
ceremonies) returns *funeral* celebrants, who match the words perfectly, pass every other
signal and score 1.00. No weighting removes them, because nothing about them is weak — they
are simply the wrong people. So `exclude` terms veto a row outright. They are also added to
the query as negative terms, but the scoring check is the load-bearing one: a search engine
given a rare phrase loosens the query, and the negative terms loosen with it.

**3. The CSV needed a `Match type` column.** `Target agency` holding `konsultant ślubny` reads
as a company that does not exist. The column was renamed to `Target` and a `Match type` column
added, so a row states how it should be read instead of relying on the reader knowing.

### The lesson that cost the most

`country` biases ranking; it does **not** restrict language. Searching the English `"wedding
planner"` with `country: pl` returned ten American planners. The Polish-inflected `"wedding
plannerka"` returned ten genuine Polish ones. In an inflected language the local word form is
a stronger geographic filter than any location phrase — and a *free* one, since it costs no
extra query term.

That is now a load-time lint rather than a paragraph in a runbook: a brief whose `language` is
not `en` but whose phrase targets are plain ASCII gets a warning naming the offending phrases.
It warns and proceeds — it is a judgement call about a valid brief, and the operator may have
good reason.

### Consequences

The tool now covers two shapes of market instead of one, with no new backend, no new
dependency and no change to the query builder. `--demo` exercises both kinds offline.

The honest limitation: **phrase mode has a much worse signal-to-noise ratio.** The campaign
that motivated it returned 320 rows of which roughly 43 were usable. The gate is doing its job
— most people who mention a phrase are not that thing — but a phrase run needs a human pass in
a way a company run does not. Where a real target list exists, `company` remains the better
tool. Phrase mode is for when no such list exists, which is exactly when nothing else works.
