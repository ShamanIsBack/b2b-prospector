# Design notes

How this tool arrived at its current shape, and what each wrong turn cost.

[`README.md`](README.md) documents what it does today. [`docs/DECISIONS.md`](docs/DECISIONS.md)
holds the formal record — nine ADRs, one per decision, each with its context and consequences.
This file is the narrative that connects them: the order things happened in, what the evidence
was, and which beliefs did not survive contact with live data.

It is written up because the reversals are the interesting part. A repository that shows only
the final design implies the design was obvious. It was not.

---

## The problem

A Polish agritourism property needed B2B partners abroad. That means a list of real people at
real companies, with names and roles good enough to open a conversation — built by one person,
on free tiers, without a data budget.

Two constraints shaped everything:

1. **No scraping.** The output has to be defensible if anyone asks where it came from.
2. **No invented people.** In B2B outreach a fabricated name is worse than an empty row,
   because an empty row gets skipped and a fabricated one gets emailed.

The second constraint is the harder one, and it is the reason for most of what follows.

---

## Four approaches, three abandoned

| Approach | How it ended | Record |
|---|---|---|
| **DuckDuckGo via Playwright** | Hard-blocked in production. First query hit a CAPTCHA, then 403. Not a bug to fix — the door is closed. | [ADR-001](docs/DECISIONS.md#adr-001--do-not-scrape-search-engines) |
| **Google via a real browser** | Worked. Also violates Google's ToS (`robots.txt: Disallow: /search`). Deleted rather than shipped. | [ADR-001](docs/DECISIONS.md#adr-001--do-not-scrape-search-engines) |
| **Google Custom Search JSON API** | Closed to new customers, retiring 2027-01-01. New projects get `403 PERMISSION_DENIED` **even with the console showing the API as "Enabled"** — which is how a day disappears. | [ADR-002](docs/DECISIONS.md#adr-002--do-not-build-on-the-google-custom-search-json-api) |
| **Gemini grounding** | Built, worked, then demoted to secondary after measurement. | [ADR-006](docs/DECISIONS.md#adr-006--serperdev-is-the-primary-backend-gemini-grounding-is-secondary) |

The Gemini demotion is worth dwelling on, because it is the one that was decided properly.

Grounding was the original design, and the project was called `grounded-prospector` for it.
The objection raised against it was that it cannot paginate. Rather than argue from
documentation prose, the response API's own types were inspected in the installed SDK:
`GoogleSearchResult` exposes `search_suggestions` and nothing else. There is no result list, no
`page`, no offset. The model cites a handful of sources it chose; you cannot walk a result set
that is never exposed.

That is a structural fact, not a limitation to work around, and it settled the question in
minutes. **Check the object, not the docs** — documentation describes intent, the type
signature describes what exists.

Gemini stayed as a real second backend anyway. A provider abstraction with one implementation
proves nothing; two implementations with genuinely different capabilities forced the interface
to be honest about what a backend can and cannot do
([ADR-005](docs/DECISIONS.md#adr-005--a-provider-abstraction-with-more-than-one-real-implementation)).

---

## The rule that survived every rewrite

Everything else changed. This did not:

> A search hit is **evidence**. A prospect is an **interpretation** of that evidence, produced
> by deterministic code. Model prose is never a source of facts.
> — [ADR-003](docs/DECISIONS.md#adr-003--the-language-model-is-a-search-dispatcher-never-a-source-of-facts)

In practice the language model, where one is used at all, is a *search dispatcher*: it runs a
query and cites URLs. Only `url_citation` annotations become data. Its actual sentences are
stored in a field that nothing reads.

This is why the default backend today contains **no language model in the data path**. Serper
returns Google's organic results as JSON; a hand-written parser turns titles into names. The
anti-hallucination guarantee is not a matter of prompt discipline — there is no prose to
hallucinate in.

### The project ends with less AI in it than it started with

Worth stating plainly, because it looks like a retreat and is not one.

The task is *retrieval with an exact-match filter* — find pages matching a boolean query, then
check whether a specific string appears in a specific position. That is a solved problem with
a deterministic answer, and a language model asked to do it will be slower, costlier,
non-reproducible, and occasionally wrong in ways no test catches. Every property the tool needs
— pagination, ranking, snippets, byte-identical re-runs, a cache that makes re-interpretation
free — turned out to be a property the model layer *removed*.

So the model was removed from the default path instead. The result is reproducible, measurably
faster, cheaper by list price (~$1 versus ~$14 per thousand searches — vendor figures, see
ADR-006), and it produced 1,249 rows across three markets for about $0.13.

What remains is the part where judgement genuinely helps: deciding *what* to search for, and
how to read what came back. Those are the two places a language model earns its keep here — and
they are exactly where a person is still doing the work, by writing the brief and triaging the
CSV.

**The transferable skill is not wiring up a model. It is recognising which part of a problem is
retrieval, which part is judgement, and refusing to pay model prices for the first.** A design
that reaches for an LLM before asking that question will usually ship something worse than this
and describe it as more advanced.

---

## Three reversals

### 1. The confidence gate went from binary to graded — because real data said so

The original rule was clean: if the target company appears anywhere in the result, the row is
good. The first production run made it look excellent — 246 of 433 rows "ready to contact".

Reading those rows as a user rather than checking that tests passed showed the problem. A
profile headlined **"Retired Banker"** scored 1.00. So did an operations manager at an entirely
different company.

The cause: the company name was being matched against the *snippet*, which is free text
containing past employment. Of the 246 passing rows, 96 matched in the result title — where
LinkedIn states the current employer — and 150 matched only in the snippet.

The fix was to grade the signal rather than move the threshold: a title match clears the row, a
snippet-only match earns partial credit and a flag saying *this may be a former employer*.
Ready-to-contact fell from 246 to 96. That drop was the tool becoming honest
([ADR-004](docs/DECISIONS.md#adr-004--company-match-is-a-gate-not-a-weight)).

**The lesson generalises past this project:** a green test suite tells you the code does what
you wrote. Only reading the output tells you whether what you wrote was worth doing.

### 2. Configuration moved out of the environment — because a knob was unreachable

Search settings had scattered across three places: a YAML file, environment variables
(`BP_COUNTRY`, `BP_MAX_PAGES`), and CLI flags. Retargeting the tool at a different country
meant remembering that `gl=ae` was set in `.env`.

Worse, `keywords` was reachable from nowhere at all. `build_xray_query()` accepted the
parameter, the config model had no field for it, and nothing ever passed one — **and a passing
test covered it**. That is precisely how dead code hides: the test proved the function worked
if called, not that anything called it.

Now one `search.yaml` describes a whole search, and `.env` holds secrets and infrastructure
only. `country` and `language` were *deleted* from settings rather than left as a fallback,
because a second place to set something is the problem, not the mitigation
([ADR-008](docs/DECISIONS.md#adr-008--the-search-subject-is-configuration-the-environment-is-infrastructure)).

### 3. `Agency` became `SearchTarget` — because a target stopped being a company

Two campaigns worked because the companies could be named. The third could not: Polish wedding
planners are a long tail of sole traders whose businesses are personal brands. There is no list
to hand the tool.

The way out came from noticing that the query builder never inspects the target text — it
interpolates it as a quoted phrase and nothing more. So a *job-title phrase* works in the
company slot, and the confidence gate quietly changes meaning: for a company, a title match
means "works there"; for a phrase, "describes themselves this way". For a sole trader, the
second is the more useful fact.

Shipping it properly took more than a rename
([ADR-009](docs/DECISIONS.md#adr-009--a-target-may-be-a-phrase-not-only-a-company)):

- **The review reasons had to fork.** `company 'konsultant ślubny' … this may be a former
  employer` is scored correctly and describes nonsense — nobody was employed *by* "wedding
  consultant". A wrong score gets checked; a confident wrong explanation gets believed.
- **Some false positives cannot be down-weighted, only vetoed.** `"mistrz ceremonii"` (master
  of ceremonies) returns *funeral* celebrants. They match the phrase exactly, have well-formed
  titles and plausible names — they score **1.00**. Nothing about them is weak, so no weighting
  removes them. Hence exclusion terms that veto a row outright.
- **The CSV needed a `Match type` column.** A column headed `Target agency` containing
  `konsultant ślubny` is a lie told by a header.

---

## What only live data could teach

### Mocks prove you can parse the response you imagined

The first real API call failed three times, for three reasons, none of which any mock could
have caught:

1. **`400 Query pattern not allowed for free accounts.`** Serper's free tier rejects `num > 10`
   for operator-heavy queries — and every X-ray query is operator-heavy. The message was in the
   response body, which `raise_for_status()` discarded before anything read it.
2. **`UnicodeEncodeError: '‏'`.** A real Dubai profile title carried a right-to-left mark,
   which crashed the Windows console outright.
3. **A fully-cached run reported a cost of $0.001.** Attempted queries were being counted
   instead of billed ones.

A mock transport validates your parser against your own assumptions. Only a real request proves
the vendor accepts it.

### Probe before spending, and let the probe overturn you

Standard practice on this project became: spend 20–30 queries testing the *assumptions behind* a
plan before running it. It changed the plan nearly every time.

- The Dubai probe showed the **geographic phrase**, not the role filter, was suppressing
  results — the opposite of the hypothesis. It also killed the proposed fix in three queries,
  and found a company name that was simply wrong (`FalconBayTravel` → `Falcon Bay Travel`, nothing → a
  full page).
- The German probe found `Nordlicht Reisen` should be `Nordlicht Busreisen`, and that a 30-year coach
  operator has no LinkedIn presence at all — a fact about the market, not a bug.

### `country` biases ranking; it does not restrict language

The most expensive single misconception, and it is not obvious from any documentation. Serper's
`gl` tilts *ranking* toward a country. It does not filter language, and it does not filter where
a person is.

```yaml
- name: wedding planner      # with country: pl → ten American planners
- name: wedding plannerka    # → ten Polish ones
```

**In an inflected language, the local word form is a free and stronger geographic filter than
any location phrase.** It costs no extra query term, and unlike requiring `"Warszawa"` it does
not depend on the person having written their city anywhere. This is now a load-time warning
rather than a paragraph in a runbook.

### A search engine loosens a query it cannot satisfy

Ask for a quoted phrase with too few genuine matches and Google will not return nothing — it
relaxes the phrase. `"Orland Reisen"` came back with ten people *surnamed* Reisen. **A full page
of unrelated results means the same thing as an empty page**, and the difference matters when
you are deciding whether a company is well covered.

The same mechanism defeats negative terms, which is why exclusions are re-checked during scoring
rather than trusted to the query.

### The gate reads a truncated string

Google truncates long result titles with an ellipsis, and the company gate reads the title. A
company called "Nordlicht Ferienhäuser und Aktivreisen" is cut off mid-name, so genuine staff score zero
title-verified. The gate is not wrong about the evidence; the evidence arrived clipped.

---

## What review caught that a green suite did not

Four defects survived a passing suite at 97% coverage and were found by review rather than by
tests. Three of them share one shape: each lived in the *interaction* between two things the
suite only ever exercised alone.

- **The dedupe ranking.** An exclusion veto deliberately does not lower the score, and the
  dedupe ranked duplicates on score alone — so a vetoed 1.00 row displaced a clean 0.85 row
  for the same person, deleting a usable contact. Every scoring test asserted on a single
  prospect; the bug needs two rows.
- **Normalisation was ASCII-only.** Matching tokenised on `[a-z0-9]+`, which silently deletes
  every letter outside it — consistently on both sides, so every ASCII test passed. But
  `ślub` (wedding) collapsed to `lub` and matched inside `klub` (club), and an exclusion like
  `łódź` collapsed to the bare letter `d` and vetoed nearly everything. The tool's own home
  market is Polish; the failure sat in the gap between the test inputs and the production
  inputs.
- **The cache key omitted `language`.** Correcting `language: en` to `pl` in a brief silently
  replayed the cached English results for the length of the TTL. Reaching the bug takes two
  runs with different settings, which no single-call test expresses.
- **A rejected API key exited 0** with an empty CSV and one warning per target —
  indistinguishable, to a script or a tired operator, from a thin market. It now aborts the
  run with exit code 2.

The lesson worth keeping: coverage measures which lines ran, not which combinations were
asserted. The suite's blind spot was never an untested line — it was two rows, two runs, two
settings, where every test had one.

---

## What it cost, and what it produced

Three campaigns, one codebase, no code changes between them — a new brief file and `--search`:

| Campaign | Targets | Rows | Title-verified |
|---|---|---|---|
| Dubai travel & MICE agencies | 14 companies | 433 | 96 |
| German operators selling Poland | 22 companies | 512 | 150 |
| Polish wedding planners | 7 companies + 11 phrases | 304 | 30 |

Roughly **$0.13 total**, about 210 of 2,500 free Serper queries.

Caching raw API responses — rather than parsed results — is what made iteration free. Every
scoring change since the first run has been re-applied to all three campaigns for **$0.000**,
because interpretation is cheap to redo when the evidence is on disk. Of all the early
decisions, this one paid off most often.

---

## Known limits, honestly

- **Phrase mode has a much worse signal-to-noise ratio, and always will.** Most people who
  mention a phrase are not that thing: 304 rows yielded about 45 usable. Where a real target
  list exists, company mode is better. Phrase mode is for when no such list exists.
- **Recall is not exhaustive and coverage is uneven.** One agency returned 48 people, another
  returned 1. Usually that is a small LinkedIn footprint, not a failed search — but the tool
  cannot tell you which.
- **Token matching accepts supersets.** "Al Noor Majlis Concierge, LLC" contains every token
  of "Majlis Concierge".
- **The title parser mishandles spaced double-barrelled surnames.** `"Katarzyna Nowak -
  Wiśniewska - Event Manager"` parses `Wiśniewska` as the headline. The separator pattern requires
  surrounding whitespace *precisely* to protect hyphenated names, so this needs a redesign
  rather than a patch, and it is still open.
- **Nothing here finds email addresses.** That is deliberate — see the README — but it means
  the CSV is a research list, not a campaign.

## What I would do differently

Read the first run's output before writing the second run's features. The scoring defect that
made 60% of "ready" rows fiction was visible in row three of the first CSV, and it survived a
green test suite, a code review and a full production run because nobody opened the file as a
user would.
