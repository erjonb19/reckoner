# Streamlit redesign — proposal

**Status: proposal, for approval.** Nothing here is deployed. The live app at
<https://reckoner-ny.streamlit.app> is untouched until this is approved.

## Who and what for

A **revenue-cycle analyst at one hospital**. Their question:

> *Where does my hospital's published rate disagree most with what the payer
> published — and why?*

Everything below is organised around that question. Three things about the
current app stop it answering:

1. **Its grain is the health system; the analyst's is the facility.** "Mount
   Sinai" is seven hospitals. Nothing in `summary/` is at facility grain except
   the exemplars.
2. **The exemplars cover 58 of about 220 facility × carrier pairs.** The cap is
   25 rows per carrier per *system*, so most pairs have no rows to drill into.
3. **There is no rate-level data.** A code lookup ("show me 70450 everywhere")
   needs every compared rate. Rates are not concentrated: 31.6M hospital rate
   rows span 90,997 codes, and the top 1,000 codes cover only 17.4%. There is no
   small slice that answers "any CPT or DRG".

A fourth problem is not about design, but the page would inherit it. **Nothing
scheduled regenerates `lake/summary`.** The mart rebuilds gold on the 1st, and
the summary workflow copies `lake/summary` on the 2nd. But only the `report`
stage writes `lake/summary`, and it isn't scheduled (nor is `triage`). October's
page would show September's numbers without saying so. See *Freshness* below.

---

## Information architecture

Four pages, one filter bar shared by all of them.

> **Figures inside the mockups are illustrative placeholders, not measurements.**
> The headline totals (3,622,830, 17,273,640, 171,078, 79%) are real, from the
> 2026-09-23 gold. Every per-facility number in a mockup is invented to show layout.

```
┌──────────────────────────────────────────────────────────────────────────┐
│ RECKONER   Rankings · Pair detail · Code lookup · Coverage & data quality │
├──────────────────────────────────────────────────────────────────────────┤
│ System [All ▾]  Facility [All ▾]  Carrier [All ▾]  Code type [All ▾]  ⟲  │  ← persists across pages
│ Data: gold built 2026-09-23 · hospital files 2026-04 · payer files 06–09  │  ← always visible
└──────────────────────────────────────────────────────────────────────────┘
```

- **Rankings** (landing): which facility × carrier pairs disagree most.
- **Pair detail**: one facility × one carrier, and why.
- **Code lookup**: one code, every facility and carrier.
- **Coverage & data quality**: what was never compared, and why, in plain
  language.

**Filters persist** in `st.session_state` and are mirrored into
`st.query_params`, so a URL reproduces a view. "Send me what you're looking at"
then works by pasting the address bar. Clicking a row on Rankings sets Facility
and Carrier and opens Pair detail. The filter bar is the only navigation state.

**Every page has** a one-line answer at the top, a collapsed *How to read this*
panel, and a *Download* button under every table. Downloads export the
filtered rows, as CSV, with the column names shown on screen.

---

## Rankings (landing)

```
WHERE DO WE DISAGREE MOST?
Across 7 systems, 3,622,830 of 17,273,640 hospital rates were compared with the
insurer's own range. 171,078 disagree materially and no rule explains why.

Rank by  (•) unexplained rates  ( ) summed price gap  ( ) median gap
                                                                  ⤓ Download
 #  Facility                        Carrier          Compared  Unexplained  Median gap  Inside range
 1  Cohen Children's Medical Ctr    UnitedHealthcare  41,210     9,812 ▇▇▇▇  +38%        22% ▎
 2  Mount Sinai Queens              Aetna             18,904     6,020 ▇▇▇   −41%        31% ▍
 3  NYU Langone | Tisch Hospital    Cigna             22,117     4,733 ▇▇    +17%        46% ▌
 …                                                                   (click a row → Pair detail)

▸ How to read this
```

- **Default ranking: count of unexplained material rates.** Summed dollar gaps
  are offered, but not first. They are dominated by a few DRGs priced in six
  figures, and they are **not money at stake**: the files publish prices, not
  volumes, so a gap on a code billed once a year counts the same as one billed
  daily. The *How to read this* panel says so in one sentence.
- **Median gap is signed.** "+38%" means the payer's median is 38% above the
  hospital's published rate. The direction matters to the analyst, since it's
  their rate or the payer's.
- **"Inside range"** is the share of the pair's compared rates that sit inside
  the insurer's own published range. A high value means most disagreement is
  within spread the insurer itself publishes.
- A compact bar in the *Unexplained* column carries the ranking visually. No
  separate chart competes with the table.

*How to read this (draft):* "Each row is one of your facilities and one
insurer. A rate is *unexplained* when it differs from the insurer's median by
5% or more and no rule accounts for it: not a timing gap, not a plan the
insurer doesn't list, not a known methodology difference. Counts are rates, not
claims. There's no volume in either file."

---

## Pair detail (facility × carrier)

```
MOUNT SINAI QUEENS  ×  AETNA                                   ← Back to Rankings
18,904 hospital rates compared · 6,020 unexplained · median gap −41%

┌─ Why they differ ───────────────────┐  ┌─ Where the hospital sits ───────────────┐
│ unexplained          ████████ 32%   │  │  per-code strip: payer min ├──●──┤ max   │
│ plan unresolved      █████    21%   │  │  hospital rate ◆; 40 widest codes        │
│ inside payer range   ████     18%   │  │  sorted by gap                           │
│ vintage artifact     ███      13%   │  │                                          │
│ granularity          ██        9%   │  │                                          │
│ systematic offset    █         5%   │  │                                          │
│ other                          2%   │  │                                          │
└─────────────────────────────────────┘  └──────────────────────────────────────────┘

Widest unexplained gaps                                              ⤓ Download
 Code   Type   Setting     Hospital    Payer min – median – max    Gap     Plans  Triage
 G0422  HCPCS  outpatient  $1,534.46   $154.65 – $204.96 – $229    −87%    3      near offset
 55875  CPT    outpatient  $9,448.00   $20,946 – $27,760 – $31,020 +194%   2      unexplained
 …   (up to 100 rows per pair)

Refused for this pair: TiC-exempt 4,120 · no counterpart 2,310 · other class 880   → Coverage
▸ How to read this
```

- **Side by side**: *why* (explanation breakdown) on the left, *where*
  (distribution) on the right. The distribution is a range-strip per code: the
  insurer's min–max as a line, its median as a tick, the hospital's rate as a
  marker. That shows at a glance whether the hospital is outside the insurer's
  whole range or merely off its median.
- The exemplar table carries the A1 triage rule today, and A1's cause once
  labels exist and the agent passes its gate. Until then the column says
  *rule*, not *agent*.

---

## Code lookup

```
CODE LOOKUP     [ 70450              ]   CT head/brain without contrast · CPT
In 38 facilities, 5 carriers: hospital rates $180 – $2,940; insurer medians $140 – $1,610

 Facility                     Carrier          Hospital   Payer min – median – max   Gap    Why
 NYU Langone | Tisch          UnitedHealthcare $1,210     $402 – $515 – $1,180       −57%   plan unresolved
 NYU Langone | Tisch          Aetna            $1,210     $880 – $1,020 – $1,340     −16%   inside range
 White Plains Hospital        Cigna            $640       —                          —      no counterpart
 …                                                                    ⤓ Download

▸ How to read this
```

- One row per facility × carrier, including those **not compared** and why.
  Leaving them out would imply the insurer has no rate there, which is not
  always what the refusal says.
- Descriptions come from the hospital files' own `description` column (the
  most common one per code), so no licensed CPT text is needed.

---

## Coverage & data quality

```
WHAT WE COULD NOT COMPARE, AND WHY
79% of hospital rates were never compared. Most for reasons that are correct:

 ████████████████████████  TiC-exempt products     Medicare Advantage and Medicaid rates exist
                                                   only in hospital files, by federal rule.
 ███████                   No insurer rate          The insurer publishes nothing for that code
                                                   at that facility — or names a carrier we don't hold.
 ████                      Other billing class      A facility charge vs a professional fee.
 …

HOW OLD IS EACH SIDE?
 Facility / carrier grid of days between the hospital file and the insurer file,
 shaded; the rule refuses beyond 400 days.

▸ How to read this
```

- Plain-language reason names come from one mapping in `summary_view`, and the
  mapping is tested against every reason the comparability layer can emit, so a
  new reason can't appear on the page unlabelled.
- The vintage grid uses the existing `vintage_alignment` table.

---

## Visual design

Not default Streamlit. The subject is an audit of two published price lists, so
the look is a **ledger**: quiet, tabular and exact, with one accent kept for "the
hospital's number".

- **Color.** Ink `#1D2433` on paper `#F7F6F2`. Rules `#D9D6CC`. Accent
  (hospital) `#B4532A`, and insurer `#2F5D7C`. Semantic colors are separate:
  within range `#4A7C59`, unexplained `#A33B3B`. Dark theme: paper `#15181E`,
  ink `#E7E4DC`, with the same hues at adjusted lightness.
- **Type.** *Source Serif 4* for page titles and the one-line answers, *Inter
  Tight* for UI, *JetBrains Mono* for codes and figures, with tabular numerals
  throughout.
- **Layout.** Wide, left-aligned, generous whitespace, no cards. Tables are the
  primary object, charts sit beside them, and nothing is centred.
- **Implementation.** A `.streamlit/config.toml` theme, one injected stylesheet,
  and Altair charts on a shared palette. The emoji page icon goes.

---

## Speed

Every view reads precomputed tables. Nothing aggregates on a click.

- `st.cache_data` on every load, keyed on `summary/run.json`'s `built_at`, so a
  new dataset invalidates the cache and nothing else does.
- Code lookup filters one indexed table. Target: under 200 ms per interaction
  on Community Cloud.

---

## Schema extension

The views need facility grain, which gold doesn't produce today. **All of it is
computed in the mart, so it first exists after a mart run.**

| table | grain | new or changed | est. rows | feeds |
|---|---|---|---:|---|
| `pairs` | facility × carrier | **new** | ~220 | Rankings |
| `outcomes` | + **facility** | changed (a key column) | ~2,000 | Pair detail: why |
| `refusals` | + **facility** | changed (a key column) | ~5,000 | Pair detail, Coverage |
| `residual` | facility × carrier, top 100 by gap | **new**, replaces the per-system exemplar cap | ≤ 22,000 | Pair detail table |
| `rates` | facility × carrier × code | **new**: hospital rate, insurer min/median/max/count, explanation, reason if refused | ~1–3M, estimated | Code lookup |
| `codes` | code | **new**: most common hospital description | ~91,000 | Code lookup search |

`pairs` columns: `hospital_slug, system, facility, carrier, hospital_rates,
compared, material, unexplained_material, median_signed_gap,
summed_abs_gap_usd, inside_range_share, like_class_share`.

**`rates` is the one that doesn't fit the current delivery.** At an estimated
1–3M rows it is 15–60 MB as zstd Parquet, measured properly once the mart
writes it. Committing that monthly bloats git history. Three options:

1. **A GitHub Release asset per month (recommended).** The report stage
   uploads `rates.parquet` to a release. The page downloads it once on start
   and caches it. There is no secret, it stays out of git history, and it's
   versioned. Cost: the page makes one network call, to GitHub. Today it makes
   none, and the streamlit-deploy note says so.
2. **Commit it to the repo.** Simplest, and no network, but around 0.5 GB of
   history a year.
3. **Narrow code lookup to compared rates of the top 3,000 codes** (39.7% of
   rows). It fits in the repo, but it no longer answers "any code".

## Freshness

Close the gap above before the redesign relies on the data: **schedule `triage`
and `report` after the mart.** The smallest change is for `reckoner-mart` to
run `mart`, then `triage`, then `report` in one execution. Both are small, and
the page then refreshes on the 2nd as designed. The alternative is two more
scheduled executions. A decision for you.

---

## Build plan

1. **Schema** (one PR): the mart writes the new tables. Tests on fixtures, and
   `summary_view` gets typed readers for each.
2. **App** (two to three PRs): a new entry point, `app/` with `st.navigation`
   pages, built against a fixture dataset. The **live app keeps running
   `streamlit_app.py` until you approve the preview.**
3. **Preview**: a second Community Cloud app pointed at the branch, for you to
   try on real data, then the live app switches entry points.

**Real data for the preview** needs a mart run with the new tables. The
September grant allows single-system runs only. One run of White Plains (about
8 minutes, roughly 2,000 vCPU-seconds) would give a real single-facility
preview before 1 October. Otherwise the preview waits for 1 October's
scheduled run.

## Decisions needed

1. **Ranking default**: count of unexplained rates (proposed), or summed gap?
2. **Rate-level delivery**: GitHub Release asset (proposed), commit, or top-3,000
   codes only?
3. **Freshness**: fold `triage` and `report` into the mart execution (proposed),
   or schedule them separately?
4. **Preview data**: one White Plains run now, or wait for 1 October?
5. **Visual direction**: the ledger palette and type above, or another direction?
