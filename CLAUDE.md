# CLAUDE.md

Project context for Claude Code. Read `docs/SPEC.md` for the full plan.

## What this project is

A pipeline that reconciles **two different federal price transparency disclosures** of the same negotiated rates for New York hospitals, quantifies where they disagree, and explains why. CMS has formally named the misalignment between these two rules as a barrier to price transparency.

This is a **production-grade personal project**: deployed, scheduled, monitored, tested. It is NOT production experience. Never write resume or README language claiming real users or business decisions depend on it.

## Domain facts you must not get wrong

There are **two separate rules**. Do not conflate them.

**Hospital Price Transparency (45 CFR 180)** — hospitals publish. Covers **every payer and plan the hospital contracts with**, including Medicare Advantage, Medicaid managed care, and CHP. CMS template layout required since 7/1/2024. Files are tens to hundreds of MB. Updated at least annually. Discoverable at `<hospital-domain>/cms-hpt.txt`.

**Transparency in Coverage** — payers publish. Covers **commercial group and individual market only**. CMS explicitly exempts Medicare, Medicare Advantage, Medicaid, and Medicaid MCO plans. Files are 100 GB to 1 TB+. Updated monthly.

**Consequence:** Medicare Advantage and Medicaid rates exist ONLY in hospital-side files. If code or docs imply otherwise, that is a bug.

**Third source:** CMS fee schedules (PFS, OPPS, IPPS) — the denominator for percent-of-Medicare, which is the unit contracting teams actually use.

## Architecture rules

1. **Parse once, land curated.** Read large files a single time, write a filtered slice to ADLS Gen2. Never rescan raw files.
2. **ADLS Gen2 is authoritative.** Fabric reads via OneLake shortcuts. Fabric is compute and presentation, never the only copy of the data.
3. **Logic in code, in git.** Notebooks and SQL. Do NOT use Dataflow Gen2 or GUI pipeline definitions — they do not port when the Fabric trial ends.
4. **Quarantine, never hard-fail.** Rows failing validation go to `_rejects` with a reason code. Alert on reject-rate thresholds.
5. **Every load is audited.** `LOAD_AUDIT`: batch id, source URL, file vintage, rows in, rows out, checksum. Loads must be idempotent and re-runnable.

## Agent rules

Four agents, all doing work **inside** the pipeline. NL-to-SQL over the marts is explicitly out of scope.

- **A1 Variance triage** — classify hospital-vs-payer discrepancies. Structured output only.
- **A2 Entity resolution** — match free-text payer/plan names across sources. Review decisions become labels.
- **A3 Schema adaptation** — generate parser adapters for non-conforming hospital files. Sandboxed.
- **A4 Ingest monitoring** — detect new file drops, judge material vs cosmetic change.

**Guardrails, non-negotiable:**
1. Agent proposes, deterministic code validates. Nothing reaches a curated table without passing tests.
2. Generated code executes sandboxed, no write access downstream.
3. Every agent action produces a testable artifact — adapter, match, or classification. Never just prose.
4. Bounded retries, human queue as terminal failure path, per-call cost and latency tracking.

**Build order:** deterministic implementation first, agent second, after the reject/variance tables show where the long tail is.

## Analytical hazards

- **Vintage mismatch is structural.** Hospital files update annually, payer files monthly. A variance may be a timing artifact. Never report a discrepancy as a finding without ruling this out.
- **Methodology heterogeneity.** Negotiated dollar, percentage, per diem, and case rate are not comparable. Comparability rules must be explicit and documented. Exclusions must be justified.
- **Percent-of-Medicare requires correct matching** on fee schedule, setting, and geographic locality. Not a lookup.

## Conventions

- Python 3.11+, type hints required
- pytest for tests; every parser needs tests built from **real files**, not from the CMS spec
- Streaming parsers for anything over ~1 GB; never load a full payer file into memory
- Secrets via environment, never in code
- PR-driven: CI gates on every push, no direct commits to main

## Layout

```
├── CLAUDE.md
├── docs/
│   ├── SPEC.md              # the plan
│   ├── BUILT_VS_PLANNED.md  # what is actually built; check before claiming
│   └── adr/                 # design decisions, numbered
├── src/
│   ├── discovery/     # cms-hpt.txt crawler, TOC walker, HEAD size probe
│   ├── hospital/      # CMS template parser, deviation handlers, facility and
│   │                  #   region resolution, the landing/audit layer
│   ├── payer/         # reader over the already-parsed TiC parquet
│   ├── benchmark/     # CMS fee schedule loaders
│   ├── reconcile/     # comparability rules, variance mart, system-range compare
│   └── agents/        # A2 (payer + plan matchers) and their eval harnesses
├── notebooks/         # Fabric notebooks, exported
├── tests/
└── evals/             # labeled sets, scoring, results history
```

## Current phase

**Phase 3 done, Phase 4 in progress.** See `docs/SPEC.md` for the plan; this
section is the state, and it is the first thing to correct when it drifts.

Landed: hospital ingest, the Medicare benchmark, the payer TiC reader, the
comparability and variance layers, the A2 matchers at payer and plan level, and
A4's deterministic half (`agents/ingest_monitor.py`, wired into the daily
snapshot) and A3's (`hospital/conformance.py`, wired into the ingest). A1 does
not exist. Neither generative half is built, and in both cases the block is
evidence rather than capability: every manifest diff so far reports no change,
and no hospital file in the corpus is non-conforming. Note that a file yielding
zero rows is usually correct -- Mount Sinai Brooklyn publishes 217,957 charge
items with no payer rates at all -- so `no_negotiated_rates` is the expected
verdict there, not a defect.

`docs/BUILT_VS_PLANNED.md` is the detailed built/scaffolded/not-started split and
the place to check before claiming anything. `docs/adr/` holds the design decisions.

**Data on hand** (local, not committed):

- 156M curated hospital rate lines across 12 health systems
- 59.5M payer rate lines across 120 TiC files and 6 carriers — UHC, Aetna (group
  and individual), Cigna, Empire BCBS, EmblemHealth. Vintages span 2026-06-05 to
  2026-09-04, so vintage handling is load-bearing rather than a footnote.

**What actually reconciles is much smaller than those totals.** `billing_class`
is optional for hospitals and required for payers, so a hospital that omits it
cannot be compared against an insurer at all: seven of twelve systems publish it,
four do not, Northwell publishes it on 1.6% of rows, and about 30% of rows are
reconcilable. Volume is not coverage either — roughly 46% of EmblemHealth's rows
carry a rate of exactly $0.

**Cross-source coverage is bounded by name overlap, not row counts.** The hospital
lake holds 12 systems and the payer target list holds 7; four appear in both —
Mount Sinai, NYU Langone, NewYork-Presbyterian, Northwell. Only those four can be
reconciled at all.

**Open gates:**

- Fabric trial activation depends on tenant access. If unresolved, do not write
  Fabric-specific code yet. The seam that keeps this cheap is ADR 0002.
- **The payer contract is declared but not enforced.** `src/payer/contract.py`
  states the boundary `mrf_pipeline` writes and `src/payer/curated.py` reads —
  shape, the federal TiC enums, and the per-file invariants live code assumes.
  Run it with `python -m payer.contract --payer-root ../mrf_pipeline/payer_parquet`;
  it is clean on all 120 files bar 49 warnings for blank billing codes.
  `discover_payer_files` now consults it on every load (footer-only tier, 0.07s
  across the lake) and quarantines a file that would break the read, reporting it
  through `file_summary` rather than dropping it silently.

  The manifest half is closed too: `scripts/snapshot_payer_manifest.ps1
  -Register` installs a daily Scheduled Task that snapshots the boundary into
  `data/manifests/` and logs what moved. It runs locally because the payer
  Parquet is a gitignored 4.3 GB sibling directory no cloud runner can reach.
  Remove it with `-Unregister`.
