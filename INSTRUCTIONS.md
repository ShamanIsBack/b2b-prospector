# Operating manual

The README explains what this tool is and how to configure it. This file is the runbook for
actually running a campaign and turning its output into a contact list you can work.

---

## 1. One-time setup

```bash
python -m venv .venv && .venv/Scripts/activate     # or: source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env                # paste a key from https://serper.dev
cp search.example.yaml search.yaml  # your targets
```

Check it works without spending anything:

```bash
b2b-prospector run --demo      # full pipeline, bundled data, no API key
```

---

## 2. Before spending: probe your assumptions

**Do this on any new target list.** A handful of throwaway queries costs a fraction of a cent
and routinely overturns a plan. On the Dubai list this was the highest-value spend of the
whole project — it showed the geographic filter, not the role filter, was suppressing results,
and it found a company name that was simply wrong.

```bash
b2b-prospector run --dry-run   # see every query and the worst-case cost, send nothing
```

Then sanity-check two or three companies by hand. Paste one of the printed queries straight
into Google. If it returns nothing there, no amount of tuning will help — the usual causes,
in order of likelihood:

1. **The company name is wrong.** `FalconBayTravel` returned nothing under every query shape;
   `Falcon Bay Travel` returned a full page. Always suspect this first.
2. **The geographic phrase is too strict.** Requiring `"Dubai"` cut one agency from 10 results
   to 4. Set `location: ""` and rely on `country:` to bias the engine instead — you trade
   precision for recall, and the `Location hint` column lets you sort it out afterwards.
3. **The company genuinely has a small LinkedIn footprint.** Some real agencies return 1–3
   people. That is a fact about the world, not a bug. Footprint tracks headcount and market:
   German firms under roughly 30 staff are often on XING instead and return nothing at all.

**A full page of unrelated results means the same thing as an empty one.** When a quoted
phrase has too few genuine matches, Google loosens it rather than returning nothing — a search
for `"Orland Reisen"` came back with ten people *surnamed* Reisen. Ten results that share no
common employer is absence of evidence, not a result set. Check this before concluding a
company is well covered.

Widening `roles` is nearly free — they are one OR-group inside one query, so more terms means
better recall *and* a better confidence signal. Widen before you consider anything else.

---

## 3. Run the campaign

```bash
b2b-prospector run --max-queries 100 --out out/campaign.csv
```

`--max-queries` must exceed `targets x max_pages` or the run stops early and says so. The
default ceiling is 50, which is deliberately low — it is a guard against a runaway loop on a
metered API, not a recommendation.

Expect roughly 30 seconds per 14 companies and about $0.001 per query.

---

## 4. Read the output

Three files land next to each other: `campaign.csv` (the deliverable), `campaign.json` (same
data, full types) and `run_report.json` (what the run cost and discarded).

### Work the rows in this order

1. **`Needs review = no`, sorted by `Confidence` descending.** These had the target company in
   the *result title*, which is where LinkedIn states a person's current employer. This is
   your contactable list.
2. **`Needs review = yes` with a high `Confidence`.** Usually correct people with one weak
   signal. Worth a glance each.
3. **Everything else.** Skim only if the first two groups were too thin.

### What each review reason means

| Reason | What to do |
|---|---|
| `appears only in the result snippet, not the title -- this may be a former employer` | Open the profile. The company is mentioned somewhere on the page, but that includes past jobs. This is the single largest flagged group. |
| `target company '...' not found in the result` | Almost always a genuine miss. Skip unless the name is a near-match. |
| `no target role keyword in the headline or snippet` | Real person, unclear seniority. Judge from the headline. |
| `title did not split into both a name and a headline` | LinkedIn returned a bare name. Open the profile to see who they are. |
| `parsed name does not look like a person's name` | Usually a page that slipped through. Skip. |
| `phrase '...' appears only in the snippet, not in the headline` | A `phrase` target: the words appear somewhere on the page but the person did not choose them for their headline. Judge them on what they did write. |
| `excluded term '...' appears in the result` | One of your `exclude` terms matched, so the row is vetoed regardless of score. If this fires on people you wanted, the exclusion is too broad. |

### Always sanity-check these

- **`Location hint`** — if you dropped `location` from the brief, global brands will return
  staff from any office. A real run surfaced Ivory Key Club USA and Silk Lantern Journeys India
  alongside the Dubai teams.
- **Company supersets.** A company whose name *contains* your target's name will pass the
  gate. "Al Noor Majlis Concierge, LLC" matches a search for "Majlis Concierge".
- **Duplicate humans.** Deduplication is by profile URL and cannot merge two genuinely
  different URLs. Some people hold two LinkedIn profiles.
- **Long company names.** The gate reads the result *title*, and Google truncates long titles
  with an ellipsis. A company called "Nordlicht Ferienhäuser und Aktivreisen" gets cut off
  mid-name, so real staff score zero title-verified. If a company shows many rows and no verified ones, read the rows
  before believing the flag — this is the one case where the gate is wrong rather than strict.

**Filter on `Needs review`, not on confidence alone.** An exclusion veto flags a row without
lowering its score — the score describes the evidence, the flag describes our judgement of it —
so a vetoed row sails through any `min_confidence` threshold you set.

Every row carries `Raw title`, `Snippet`, `Source query` and `LinkedIn URL`, so any row can be
verified by opening one link. If a row looks wrong, it is checkable in seconds — that is the
point of shipping the evidence alongside the claim.

---

## 5. Refine without spending again

Raw API responses are cached, so **re-running the same brief costs nothing**:

```bash
b2b-prospector run --max-queries 100 --out out/campaign.csv   # 100% cache hits, $0.000
```

This means scoring and filtering are free to iterate on after the data is collected. Changing
`min_confidence`, or any scoring logic, then re-running takes under a second. `run_report.json`
should show `searches_billed: 0` — if it does not, something in the brief changed the cache
key (query text, `country`, `language`, or `results_per_page`).

Use `--no-cache` only when you deliberately want fresh results.

---

## 6. Retarget to a different country or sector

One file. Copy `search.yaml`, edit, point at it:

```yaml
location: ""                        # or a city, if precision matters more than recall
country: pl                         # biases the engine; leaving this at `ae` is a classic mistake
language: pl
roles: [CTO, Head of Engineering, VP Engineering]
keywords: [fintech, payments]       # optional second AND-ed OR-group
max_pages: 5
min_confidence: 0.0
targets:
  - name: Booksy
    segment: saas
```

```bash
b2b-prospector run --search warsaw.yaml --dry-run   # always dry-run a new brief first
```

Nothing about the search lives in `.env`. If you find yourself editing environment variables to
change *what* you are searching for, something is in the wrong place.

---

## 7. Searching a category instead of a company

When your targets are an unknown long tail rather than a list of named firms, set a target's
`kind` to `phrase` and put a job-title phrase in `name`:

```yaml
exclude: []                       # optional, applies to every target

targets:
  - name: Wytwórnia Ślubów        # kind: company is the default
    segment: agencja
  - name: konsultant ślubny
    kind: phrase
    segment: kategoria
  - name: mistrz ceremonii
    kind: phrase
    segment: kategoria
    exclude: [pogrzeb]            # otherwise funeral celebrants match perfectly
```

The query becomes `site:linkedin.com/in/ "konsultant ślubny" (roles...)` and the confidence
gate changes meaning: instead of "does this person work there" it asks "does this person
describe themselves this way" — the more useful question when the market is sole traders with
no company worth naming. Background in `docs/DECISIONS.md`, ADR-009.

Three things to know:

- **Use the local wording.** `country` biases ranking; it does not restrict language. Searching
  the English `"wedding planner"` with `country: pl` returned ten American planners; the
  Polish-inflected `"wedding plannerka"` returned ten Polish ones. Where a language inflects,
  the local word form is a free and stronger geographic filter than any location phrase. The
  tool warns at load time if a non-English brief carries plain-ASCII phrase targets.
- **Expect to need `exclude`.** A phrase is a substring of longer phrases meaning something
  else: `"mistrz ceremonii"` returns *funeral* celebrants who match perfectly and score 1.00.
  Exclusion terms veto a row outright, and are re-checked during scoring because a search
  engine loosens negative terms along with the rest of a rare query. Add them after you have
  seen the noise, not before — guessing produces briefs that quietly exclude real prospects.
  Terms match as a **substring** of the normalised title and snippet, so one stem covers a
  whole inflection family: `pogrzeb` also catches `pogrzebowej` and `pogrzebowy`. The cost is
  that a short term matches inside longer words — `art` would match `smart` — so prefer a
  distinctive stem.
- **Read `Match type` before `Target`.** For `phrase` rows the `Target` column holds the
  wording matched, not an employer. The CSV states which it is per row.

**Phrase mode has a far worse signal-to-noise ratio than company mode**, and that is inherent:
most people who mention a phrase are not that thing. A real run returned 320 rows of which
about 43 were usable. Budget a human pass. Where a real target list exists, use `company`.

---

## 8. What this tool does not do

**It finds people, not contact details.** The `Email`, `Phone`, `Business profile`,
`Client segment`, `Potential rating` and `Notes` columns are deliberately left blank. Filling
them with guesses would invite someone to email an unverified address.

Getting from this CSV to actual outreach needs an enrichment step, which is a separate
decision with a cost attached:

| Option | Rough cost | Gives you |
|---|---|---|
| **Apollo.io** | from ~$49/mo | Email + phone + company data, from its own licensed B2B database |
| **Hunter.io** | free 25/mo, then ~$49/mo | Email only, with a confidence score |
| **Manual** | free, slow | Whatever is on the company's own website |

Feed it the `LinkedIn URL` and `Company from title` columns. Note that an enrichment provider's
own database may already cover your target companies directly — in which case running it as
the *primary* source, rather than as enrichment on top of this list, may be the better path.

**Before any outreach**, remember what the CSV is: names and job titles of real people,
collected under legitimate interest. Disclose where you got the data in first contact, and
honour any objection immediately.
