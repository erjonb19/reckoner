# ADR 0001 — Payer TiC lakehouse

- **Status:** Accepted
- **Date:** 2026-09-09
- **Supersedes:** the "one carrier only" scoping in `docs/reckoner_phase3_claude_code_brief.md`

## Context

Reckoner reconciles two federal disclosures of the same negotiated rates: what hospitals
publish under 45 CFR 180, and what payers publish under Transparency in Coverage. The
hospital side has been landed since Phase 1. This ADR records how the payer side is
sourced, scoped and stored, and where the AI components sit relative to it.

The Phase 3 brief that prompted this ADR was written against an earlier picture of the
repo. Several of its premises had expired by the time it was acted on, and this ADR
records the decisions actually taken rather than the ones the brief assumed.

## Decision

### 1. The parser stays in a separate repo; the boundary is the Parquet

`mrf_pipeline` fetches each carrier's table-of-contents, streams the in-network files with
`ijson`, filters to target providers, and writes Parquet. Reckoner never opens a raw payer
file — `src/payer/curated.py` reads only the Parquet.

This is not a new decision so much as a formalisation of one the architecture already
required. CLAUDE.md rule 1 is "parse once, land curated. Never rescan raw files." The
alternative — reimplementing the ingest inside reckoner, as the brief's Phase 1 describes —
would duplicate a working parser and re-read files that run to 15 GB apiece.

What follows from it is the real work: the boundary needs a **versioned data contract**,
because a directory of Parquet files is an interface whether or not anyone wrote it down.
Today reckoner infers the schema by reading whatever is there, which is why a column the
parser added (`last_updated_on`) sat unused for months while the code fell back to a
hardcoded date map covering 10% of the files.

### 2. Six carriers, not one

The brief scoped this to a single carrier. Six are parsed and on disk:

| Carrier | Files | Rows | Vintage |
|---|---:|---:|---|
| UHC | 6 | 24,445,054 | 2026-08-01 |
| Aetna (ALIC, group) | 6 | 22,407,168 | 2026-08-05 |
| Cigna | 5 (3 after dedup) | 6,460,030 | 2026-08-01 |
| Empire BCBS | 4 | 3,871,901 | 2026-09-01 |
| Aetna (NY, individual) | 1 | 1,324,087 | 2026-06-05 |
| EmblemHealth | 98 | 993,195 | 2026-09-04 |

The scope widened because Empire and Emblem are the two largest NY payers by membership,
and a reconciliation that omitted both would answer a narrower question than the one the
project exists to ask. Their file layouts differ sharply — Empire publishes four large
network files, Emblem publishes ~280 small plan-specific ones — which is itself a finding
about how unevenly the same rule is implemented.

Two Cigna files (`Cigna_PathwellOAP`, `Cigna_PathwellPPO`) are row-for-row duplicates of
their `National` counterparts and are dropped by default; see `DUPLICATE_PAYER_FILES`.

### 3. Provider filter: NPI plus a reviewed TIN list

Matching on NPI alone found roughly 10% of target hospitals, because payers list hospitals
under NPIs an NPPES name search never returns. Every provider group also carries the tax ID
it bills under, and a hospital has a handful. So the filter is the union of a 940-NPI
anchor list and a hand-reviewed TIN list (`target_tins.csv`, rows marked `include=Y`).

The list is reviewed by hand rather than generated, because a wrong TIN silently attributes
another organisation's rates to a target system. Rows marked `?` or `N` are ignored.

### 4. Medallion layers, mapped onto what exists

The brief asks for bronze/silver/gold. Rather than invent new storage, the existing layout
already is that shape and is named accordingly:

- **Bronze** — `mrf_pipeline/payer_parquet/*.parquet`: filtered rate rows as landed, one
  file per payer network, plus `_tin_names/` side lookups.
- **Silver** — `data/lake/curated/hospital_rates` (hive-partitioned) on the hospital side;
  on the payer side, the conformed shape `to_comparable_rates()` produces. Rejects go to
  `hospital_rejects`, never dropped.
- **Gold** — the variance and system-range marts behind `src/reconcile/mart_cli.py`.

Bronze lives in the parser repo, which is the one asymmetry. That is the cost of decision 1
and is accepted: the alternative is copying 60M rows to own them.

### 5. Where the AI components sit

Per CLAUDE.md, four agents, all inside the pipeline. NL-to-SQL over the marts is out of
scope. Current state:

- **A2 entity resolution — built.** Payer-level and plan-level matchers with an eval
  harness (`src/agents/`, 190 reviewed labels in `evals/plan_matching.jsonl`). Sits between
  bronze and silver: raw payer/plan strings resolve to canonical entities before a rate is
  comparable.
- **A1 variance triage — not started.** Would sit at gold, classifying what
  `src/reconcile/variance.py` currently labels with deterministic rules.
- **A3 schema adaptation — not started.** Would sit at the bronze/silver boundary, which is
  exactly where decision 1's data contract goes; A3 is the agent that proposes a contract
  change when a drop no longer conforms.
- **A4 ingest monitoring — not started.** Would watch bronze for new drops.

The build order is deliberate and stated in CLAUDE.md: deterministic implementation first,
agent second, after the reject and variance tables show where the long tail is. A1 is
therefore blocked on having enough variance rows to know what the categories should be —
not on the model.

### 6. Local-first; the cloud is a filesystem, not a rewrite

Fabric activation depends on a tenant question that is still open, so no Fabric-specific
code is written yet. The seam that makes the cloud path cheap is described in ADR 0002.

## Consequences

- Reckoner cannot fix a parser bug in its own repo; it files against `mrf_pipeline`. Both
  now have CI, which makes that boundary safe to depend on.
- A payer file yielding zero target rows writes no Parquet, so "absent" is ambiguous
  between "not parsed" and "parsed, nothing matched". `file_summary()` reports in-flight
  and superseded files explicitly for this reason; the manifest in the data contract is
  what makes the third case legible.
- Six carriers at four distinct vintages means vintage handling is load-bearing, not a
  footnote. Rates span 2026-06-05 to 2026-09-04 — a three-month spread, against a hospital
  side that updates annually.
- Rate lines are not evenly informative: ~46% of Emblem's rows carry a rate of exactly
  `$0`, refused by name as `ZERO_RATE`. Volume is not coverage.
