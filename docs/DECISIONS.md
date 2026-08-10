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

External review challenged it, and measurement against the installed SDK confirmed most of
the challenge:

| Claim | Verdict |
|---|---|
| No pagination; hard result ceiling | **Correct, and decisive.** `GoogleSearchResult` in the SDK exposes only `search_suggestions` — no result array. The only links available are the sources the model chose to cite. There is no `num`, `page` or offset parameter. Whatever it cites is the ceiling. |
| Non-deterministic | **Correct.** The model rewrites the query; identical runs can differ. `_interaction.executed_queries()` exists to observe this. |
| Cost and latency at scale | **Correct.** ~$14/1k grounded searches at ~2–4 s each, versus ~$1/1k at ~200–500 ms. |
| Risk of hallucination during extraction | **Rejected.** It describes a design where an LLM extracts fields from prose. ADR-003 rules that out structurally: only citation URLs and titles become data. |

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
