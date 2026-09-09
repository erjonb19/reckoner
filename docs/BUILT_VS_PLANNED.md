# Built vs planned

What is actually built, what is scaffolded, and what has not been started. Kept honest on
purpose: this file is the reference for what may be claimed about the project, so anything
not fully working is listed under planned, not built.

**This is a personal project.** It is deployed, scheduled, tested and monitored, but no
real users and no business decisions depend on it. Nothing here is production experience.

Last updated: 2026-09-09.

---

## Built

Working, tested, and reproducible from the repository.

### Hospital-side ingest (Phase 1)

- Discovery via `cms-hpt.txt`, TOC walk, HEAD size probe — `src/discovery/`.
- CMS-template parser with deviation handling, streaming throughout — `src/hospital/`.
  Files up to 8 GB parse in ~200 MB of memory via `ijson` event streaming.
- Landing layer with the guarantees the architecture rules require — `src/hospital/landing.py`:
  atomic staging then promote, checksum idempotency, ETag/Last-Modified short-circuit so an
  unchanged file is not re-downloaded, reject quarantine with reason codes, and a
  `LOAD_AUDIT` row per batch written whether the batch succeeds or fails.
- Facility resolution (`facility.py`) and regional peer grouping (`region.py`).

**Measured:** 156,484,277 curated rate lines across **12 health systems**.

### Medicare benchmark (Phase 2)

- PFS / OPPS / IPPS loaders, crosswalk, percent-of-Medicare — `src/benchmark/`.
- NY Medicaid APR-DRG payment model, FFS and managed care — `src/model/nys_medicaid.py`.
- Margin and scenario modelling — `src/model/margin.py`, `scenarios.py`.

### Payer TiC reader (Phase 3)

- Reader over already-parsed TiC Parquet — `src/payer/curated.py`. Never opens a raw payer
  file. Handles the documented hazards: `.part` files as open writer handles, two
  row-for-row duplicate Cigna files, 17.8% exact-duplicate rows in `Aetna_NY`, rate units
  varying with `rate_type`, `group_tins` fan-out, and 13.1% multi-system rows.
- Parsing itself lives in the separate `mrf_pipeline` repo (see ADR 0001).

**Measured:** 59,501,435 payer rate lines across 120 files and **6 carriers** — UHC,
Aetna (ALIC group + NY individual), Cigna, Empire BCBS, EmblemHealth. Vintages span
2026-06-05 to 2026-09-04. All 118 non-duplicate files carry a vintage.

### Reconciliation (Phase 4, in progress)

- Comparability layer with named refusal codes — `src/reconcile/comparability.py`.
- Variance mart with explanation rules and systematic-offset detection — `variance.py`.
- System-range comparison with implausibility guards — `system_range.py`.
- **A committed runner** — `src/reconcile/mart_cli.py`. Before it, every real-data figure
  came from throwaway scripts and no number in any write-up could be re-derived.

### A2 entity resolution

- Payer-level and plan-level matchers — `src/agents/entity_resolution.py`, `plan_resolution.py`.
- Eval harness reporting precision/recall/F1 with abstention treated as a recall cost, not
  an error — `src/agents/evals.py`. Results appended, never overwritten.
- 190 reviewed labels — `evals/plan_matching.jsonl`.
- Per-call cost and latency logged (`CallCost`), so the agent's price is a number.

### Engineering

- **651 tests** on `main`, all passing (653 with #16, which is open). Parser tests are
  built from real files, not from the CMS spec.
- `mypy strict`, `ruff` with a broad rule selection, CI gating every push in both repos.

---

## Scaffolded

Real code, but not yet load-bearing.

- **Storage seam for the cloud.** Designed in ADR 0002, not yet implemented. Every read
  already funnels through two functions, which is what makes it cheap.
- **Provenance.** `src/reconcile/provenance.py` attaches vintage spans and caveats to
  reported figures. Wired into the mart; not yet surfaced in every artifact.
- **Discovery at scale.** The crawler works, but 5 of 8 probed health systems return HTTP
  403 to automated `cms-hpt.txt` requests, so full-automatic discovery is not a claim I can
  make.

---

## Not started

- **A1 variance triage.** Blocked by design, not by capability: CLAUDE.md sets the build
  order as deterministic first, agent second, once the variance table shows where the long
  tail is.
- **A3 schema adaptation.** Needs the bronze/silver data contract from ADR 0001 to diff
  against — that contract does not exist yet.
- **A4 ingest monitoring.**
- **Payer data contract and file manifest.** `src/payer/contract.py`, `manifest.py` —
  proposed in ADR 0001, not written.
- **Ops tables.** `ops.pipeline_runs`, `ops.dq_results` — no telemetry mart, no AIOps layer.
- **Fabric lakehouse, scheduled loads, backfill command.** Gated on tenant access. No
  Fabric-specific code is written, deliberately (ADR 0002).
- **README.** Absent.

---

## Findings, with the caveats attached

Numbers reproduce via `python -m reconcile.mart_cli --hospital <H> --system <S>
--payer-root ../mrf_pipeline/payer_parquet --mode range`.

- **~30% of hospital rate lines are reconcilable at all** (47,539,517 of 156,484,277).
  `billing_class` is optional for hospitals and required for payers, so a hospital that
  omits it cannot be compared to an insurer. 7 of 12 systems publish it at 100%, 4 at 0%,
  and Northwell at 1.6%.
- **34.6% of NYU Tisch's disclosure can never reconcile** — Medicare Advantage and Medicaid
  managed care are exempt from the payer rule, so those rates exist only in hospital files.
  This is a property of the regulations, not a data quality problem.
- **Volume is not coverage.** ~46% of EmblemHealth's rows carry a rate of exactly `$0`,
  refused by name as `ZERO_RATE`. Of ~280 Emblem plan files, 182 contain no target-system
  provider at all.
- **Cross-source agreement, Mount Sinai:** of 5,690 comparisons against 5 carriers, 5,609
  were comparable and **70.4% of insurer rates fell inside the range the system's own
  hospitals published**. Median published range is 1.78× wide, which is itself the point —
  a "price" for one service at one system is a spread, not a number.

### Cross-source coverage is narrower than the totals suggest

Both sides must name the same system. The hospital lake holds 12 systems; the payer target
list holds 7; **4 systems appear in both** — Mount Sinai, NYU Langone, NewYork-Presbyterian,
Northwell. Only those 4 can be reconciled at all, regardless of row counts.

| System | Cross-source run |
|---|---|
| Mount Sinai | Measured — 70.4% inside range, 5 carriers |
| NYU Langone | Not yet run |
| NewYork-Presbyterian | Not yet run |
| Northwell | Not yet run |

---

## Known-stale claims corrected on 2026-09-09

Recording these because the failure mode they represent — a status line that quietly stops
describing the project — is the reason this file exists.

- CLAUDE.md claimed 40M payer rows across 14 files, "Aetna, Cigna, UHC only", and that
  Empire and Emblem were **not** parsed. Both are parsed; the real figures are above.
- CLAUDE.md listed "there is no committed runner" as an open gate. `mart_cli.py` landed
  in #14.
- `PAYER_SOURCE_VINTAGES` dated 12 of 120 files while the Parquet had carried its own
  `last_updated_on` for months (#16).
