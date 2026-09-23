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
2. **ADLS Gen2 is authoritative.** Everything else — DuckDB, Polars, Power BI Desktop, a notebook — is a *reader* of it. No engine ever holds the only copy. Fabric is not part of this: ADR 0003 dropped it (no capacity quota, and $262/month for 4.3 GB of data), so there is no OneLake shortcut and no workspace this project writes to.
3. **Logic in code, in git.** Python and SQL, run from `src/`. No GUI-defined pipelines or dataflows: a definition that exists only in a portal cannot be reviewed, tested, or moved.
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
│   ├── agents/        # A2 (payer + plan matchers) and their eval harnesses
│   ├── storage/       # the seam (ADR 0002) and the cloud publisher
│   ├── pipeline/      # the scheduled job's stages: manifest diff, cap probe
│   └── reckoner_job.py  # container entrypoint, one stage per execution
├── deploy/            # the Container Apps Job definition, as YAML
├── tests/
└── evals/             # labeled sets, scoring, results history
```

## Current phase

**Phase 3 done, Phase 4 in progress.** See `docs/SPEC.md` for the plan; this
section is the state, and it is the first thing to correct when it drifts.

Landed: hospital ingest, the Medicare benchmark, the payer TiC reader, the
comparability and variance layers, the A2 matchers at payer and plan level, and
A4's deterministic half (`agents/ingest_monitor.py`, wired into the daily
snapshot), A3's (`hospital/conformance.py`, wired into the ingest) and A1's
(`agents/variance_triage.py`, reported by the pairs mart). No generative half is
built. For A3 and A4 the block is evidence rather than capability: every manifest
diff so far reports no change, and no hospital file in the corpus is
non-conforming. A1 is the exception -- its residual is real, 718 ranked items
from one shard of one system, so its block is labels and effort. Note that a file yielding
zero rows is usually correct -- Mount Sinai Brooklyn publishes 217,957 charge
items with no payer rates at all -- so `no_negotiated_rates` is the expected
verdict there, not a defect.

`docs/BUILT_VS_PLANNED.md` is the detailed built/scaffolded/not-started split and
the place to check before claiming anything. `docs/adr/` holds the design decisions.

**Data on hand** (local, not committed):

- 184M curated hospital rate lines across 15 health systems
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
lake holds 15 systems and the payer target list holds 7. All seven appear in both,
and all seven reconcile, each hospital rate compared once against the carrier's
distribution for the same service and billing class (ADRs 0005, 0006). See
`docs/scope.md`.

**Deployed (Phase 2, Azure-native per ADR 0003; orchestration per ADR 0004).**
All East US, all in `rg-reckoner`:

- **ADLS Gen2 `reckonerlake0914`** (hierarchical namespace on) is the
  authoritative store, holding `bronze/payer_tic` (118 files, 56,784,415 rows)
  and `silver/hospital_rates` (122 files, 184,424,339 rows, partitioned
  `hospital_slug/code_type/vintage`), plus `silver/payer_rates` and `gold/`. Both written with zstd, matching the
  curated lake; taking `write_dataset`'s snappy default once cost 2.45 GB.
- **Container Apps Job `reckoner-pipeline`** — Consumption profile, 2 vCPU /
  4 GiB, cron `0 6 1 * *`, image from ghcr.io (no ACR: ~$5/month would trip the
  budget). One stage per execution; `manifest`, `mart`, `triage` and `report` are wired,
  the rest log `stage_not_implemented`. The mart runs as the separate job
  `reckoner-mart` (4 vCPU / 8 GiB, cron `0 8 1 * *`). Every system fits since
  #83; the all-systems single execution first runs on 1 October.
- **Authentication is a user-assigned managed identity**, named explicitly via
  `AZURE_CLIENT_ID`. Not left to `DefaultAzureCredential`: pyarrow's bundled
  Azure C++ chain shells out to the Azure CLI, which no container has. No key,
  SAS token or connection string exists in the repo or the image.
- **Log Analytics `reckoner-logs`** — 0.5 GB/day cap, 31-day retention. The job
  reads its own `dataIngestionStatus` through ARM and carries it on every
  summary record, so a capped day is visible rather than silent.
- **Cost target: the free tier.** 5 GB of hot LRS blob (5.282 GB measured,
  0.6 cents a month over) and the monthly Container Apps grant of 180,000 vCPU-s /
  360,000 GiB-s, of which a run uses ~0.04%. The $0.10/hour environment
  management meter does not apply — Consumption-only, no private endpoint, no
  VNet. Anything that would cost money beyond this is a question for the human,
  not a decision to make.

**Open gates:**

- **Fabric is closed, not open.** ADR 0003 dropped it: the trial would not
  activate, the subscription carries zero Fabric capacity units in East US, and
  an F2 is $0.36/hour — $262/month to serve 4.3 GB. Phase 2 is Azure-native
  instead, and the seam (ADR 0002) made that a configuration change rather than
  a rewrite. Do not reintroduce Fabric-specific code.
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
