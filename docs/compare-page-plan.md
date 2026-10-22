# Plan: Head-to-head Compare page for reckoner-ny

## Status (Oct 21 2026)

- **Built:** an early version of Step 2, the Compare page, reading the current `rates.parquet`. It has all three views, both presets, clean hospital names, the "check before quoting" flag, shareable links and a definitions box. Every view carries an "Early look" note, because those rates are still pooled (problem 1 below).
- **Not built:** Step 1 (the new comparison table that separates plan types and settings). It needs the Azure storage, so it runs in Claude Code on Erjon's machine. Once it lands, the page switches its data source and drops the "Early look" note.
- **Not done:** Step 3. The live site still runs `streamlit_app.py` until the main file is switched in Streamlit Community Cloud (Erjon's login).
- **Decisions taken for the early version** (change any of them): facility level by default; the 5 insurers already in the file; a hand-picked list of 23 common services instead of the CMS list.

## Goal

A page where you pick 4 or more hospitals, a payer, and a code (or a set of codes) and see their rates side by side, fairly, in one screen.

## What the current data can and cannot do

Checked on Oct 21 2026 against the live data release (`data-2026-10-01`, `rates.parquet`, checksum matches `run.json`).

What it has:
- 1.24M rows: 7 systems, about 50 facility entries, 5 insurers (UnitedHealthcare, Aetna, Anthem/Empire, Cigna, Emblem), about 30,000 codes.
- Enough overlap to compare: for the same insurer, 556 codes are published by 6 systems, 2,033 by 5, and 5,466 by 4.

Why it cannot be used as-is for head-to-head:

1. **Rates are pooled.** Each row's hospital rate is the median of every rate the hospital lists for that insurer and code. That mixes commercial plans with Medicare Advantage and Medicaid plans, inpatient with outpatient, and facility fees with professional fees. Example: CT head (70450) for Aetna ranges from $113 to $1,923 across facilities. From this file you cannot tell how much of that is real price difference and how much is mixing.
2. **Only 5 insurers.** The file only keeps insurers that also publish their own files, because it was built for the hospital-vs-insurer check. Healthfirst, Fidelis, MetroPlus and Medicare Advantage plans are in the hospital files but not here.
3. **Some locations cannot be split.** Montefiore publishes Moses, Weiler, Wakefield, Hutchinson, Westchester Square and the Children's Hospital in one combined file section, so they share one rate. NYU Langone and some Northwell Long Island hospitals look the same way. The page has to show these as a group, not pretend they are separate. (Montefiore's names also have a text-encoding glitch to fix.)
4. **Same code, different label.** One system labels 70450 as CPT and another as HCPCS. Matching must be on the code itself.

### Montefiore's own hospitals differ a lot

Same Aetna rate file, Montefiore's 5 separately published hospitals (pooled rates, so directional only):

| Code | Bronx campuses | Nyack | St. Luke's Cornwall | Mount Vernon | White Plains |
|---|---|---|---|---|---|
| 70450 CT head | $543 | $113 | $129 | $347 | $703 |
| 99285 ER visit, highest level | $2,625 | $896 | $1,260 | $3,337 | $6,582 |
| DRG 470 hip/knee replacement | $14,920 | $32,370 | $69,716 | $57,557 | $73,798 |

The DRG 470 Bronx figure looks too low next to the others, which is probably the pooling problem above (point 1). The new table in Step 1 should settle it.

## Build steps

### Step 1. New comparison table (pipeline, Claude Code on your machine)

Build `compare.parquet` from the hospital silver layer (hospital files only, no insurer files needed), published in the monthly data release next to `rates.parquet`.

One row per: system, facility group, payer (resolved name), product line (commercial, Medicare Advantage, Medicaid), code, setting (inpatient/outpatient), billing class (facility/professional).

Columns: median, lowest and highest rate, number of rates behind it, main payment method (fee schedule, case rate, percent of charges), file date.

Size control: keep only codes published by at least 2 systems. Measure the size on one system before running all 7. Target under 50 MB so the free Streamlit host can load it.

### Step 2. Compare page (in `analyst_app.py`)

Pickers, top of page:
- **Hospitals:** 2 to 6, at facility level, grouped where the file groups them. Clean labels: "Montefiore Medical Center (Bronx campuses)" instead of the joined name list.
- **Preset hospital sets** (one click):
  - *Montefiore family:* Bronx campuses, Nyack, St. Luke's Cornwall, Mount Vernon, White Plains. These are priced very differently from each other, so each stays its own entry and is never rolled up into "Montefiore."
  - *Bronx vs. Manhattan peers:* Montefiore Bronx campuses, The Mount Sinai Hospital, NYP Columbia, NYU Tisch.
- **Payer** and **product line.** Default Aetna, commercial.
- **Code:** search by number or description, or pick a preset set of codes.
- **Setting and billing class.** Defaults: outpatient facility for CPT/HCPCS, inpatient facility for DRGs. Shown in plain words, not hidden.

Three views:
1. **One code, head to head.** One bar per hospital with its low-to-high range, lowest highlighted. Table: rate, difference from the lowest, difference from the group median, number of rates.
2. **Scorecard (several codes).** Rows are codes, columns are hospitals, each cell colored by how far it sits from that row's median. Bottom line: a price index per hospital (typical ratio to the group median) and "lowest on X of Y codes."
3. **One hospital, all payers.** One code, every payer side by side, to answer "who pays this hospital the most for this."

Fairness rules built in:
- Cells are only compared when setting, billing class and product line match.
- If the payment method differs (for example case rate vs percent of charges), the cell is shown with a flag, not hidden.
- "Not published" and "published but not comparable" are shown differently.

Also: a definitions box, a CSV download, and a link that saves the current picks so a comparison can be shared.

### Step 3. Make it the live page

Finish the pending switch of reckoner-ny from `streamlit_app.py` to `analyst_app.py`, with Compare as the first page.

### Step 4. Check it before showing anyone

- Tests built from real rows (start with 70450 Aetna).
- Hand-check 5 cells against the original hospital files.
- Screenshots of each view for the README and as a meeting fallback.

## Open decisions (yours)

1. **Hospital level:** facility by default with a "whole system" switch, or system by default? Recommendation: facility.
2. **Payers:** only the 5 insurers, or also hospital-only payers (Healthfirst, Fidelis, MetroPlus, Medicare Advantage)? Recommendation: include them, since this page only needs hospital files.
3. **Preset code set:** CMS's list of shoppable services plus the most common shared DRGs, or a list you pick? Recommendation: start with the CMS list.
4. **Cost:** this stays at $0 (same job, same storage, same free host) as long as the new file stays under the size target.
