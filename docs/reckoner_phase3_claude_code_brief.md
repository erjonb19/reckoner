# Reckoner Phase 3 — Claude Code build brief

Paste this as the first message in a Claude Code session opened at the Reckoner repo root. Fill in the two placeholders before sending.

---

## Context

You are working in the Reckoner repo: NY hospital contract and reimbursement modeling (APR-DRG payment engine, hospital-side price transparency ingest, LLM-assisted payer entity resolution, ~355 tests, strict typing). Read CLAUDE.md, the README, and the package layout before proposing anything.

Phase 3 adds the payer side and turns Reckoner into an operated pipeline. Two deliverables, built in this order:

1. A payer Transparency-in-Coverage (TiC) lakehouse on Microsoft Fabric, with three AI components that run inside the pipeline under human approval.
2. An AIOps layer that monitors that pipeline's own telemetry and proposes fixes.

Hard constraints:
- Total spend as close to zero as possible. Fabric 60-day trial for the lakehouse and scheduling. Heavy parsing runs locally on this PC. Only Parquet gets pushed to the cloud, never raw JSON.
- Public data only. No PHI, no Montefiore data.
- Do not modify the APR-DRG payment engine or its tests except to consume its outputs.
- Every AI component has a deterministic baseline, deterministic validation of every LLM proposal, a human approval step, and per-call cost/latency logging. Nothing auto-merges.
- Every phase ends with tests passing and a short entry in docs/BUILT_VS_PLANNED.md that says exactly what is built, what is scaffolded, and what is not started. I use that file to decide what I am allowed to claim on a resume; keep it honest.
- Work in small PR-sized branches. Plan before writing code. Ask before deleting anything or changing a public interface.

Carrier: [CARRIER, e.g. Aetna]
Fabric workspace name: [WORKSPACE]

Deadline: end of September 2026, inside the Fabric trial window.

---

## Phase 0 — Discovery and design (no code changes)

- Summarize the current architecture: modules, data flow, where the hospital-side MRF ingest lives, how the existing payer entity resolution and its eval harness are structured, how the backend abstraction (if any) is organized.
- Propose a module layout for `reckoner/payer_tic/` (ingest, contracts, lakehouse, agents, ops) and a `FabricBackend` alongside the existing local DuckDB path. Mirror the backend-abstraction pattern used in my governed-clinical-agent repo if that pattern is not already here.
- Write docs/adr/0001-payer-tic-lakehouse.md covering: one carrier only, NY-only filter via NPPES, local parse then cloud load, medallion layers, where each AI component sits.
- Stop and show me the plan before Phase 1.

Acceptance: I approve the ADR and module layout.

## Phase 1 — Local payer TiC ingest

- Fetch the carrier's TiC table-of-contents file, identify the in-network rate files and provider-reference files for the plans we will cover.
- Streaming parse (ijson, nothing loaded fully into memory) reusing the existing ingest guarantees: atomic staging, checksum idempotency, resumable downloads, bounded retry, reject quarantine with a reason taxonomy.
- Resolve provider references to NPIs and filter to New York providers using NPPES (practice location state = NY). Log the row counts before and after the filter.
- Emit Parquet partitioned by file/plan/billing-code-type. Produce `rates`, `providers`, `payers/plans`, and `file_manifest` tables.
- Define a versioned data contract for each table (schema, required columns, allowed values, key uniqueness) in code, not just docs.
- Tests built from real file samples, same convention as the hospital-side tests.

Acceptance: one full carrier drop parsed end to end on this PC, NY-filtered Parquet on disk, contracts validated, tests green, row counts and wall time recorded in docs/BUILT_VS_PLANNED.md.

## Phase 2 — Fabric lakehouse and scheduled loads

- Fabric Lakehouse with bronze (raw Parquet as landed), silver (conformed rates, providers, plans, with surrogate keys and contract enforcement), gold (benchmark marts plus the reconciliation table below).
- Reconciliation: join payer-published negotiated rates to the hospital-published rates already in Reckoner on NPI, billing code and code type, and plan, producing a variance table (dollar and percent, methodology mismatch flags). This is the deliverable that makes "reconciled" true.
- Incremental, idempotent loads keyed on file hash and ETag. A rerun with no new files is a no-op. A rerun with one new file touches only that file's partitions.
- Orchestration inside Fabric (pipeline or scheduled notebook) with a documented run schedule, plus a `reckoner backfill --from <date>` command that replays from bronze.
- `FabricBackend` implements the same interface as the local DuckDB backend so the payment engine and benchmarks can run against either.
- Every run writes a row to `ops.pipeline_runs` (run id, stage, start/end, rows in/out, bytes, status, error) and every contract check writes to `ops.dq_results`.
- Export all notebook and pipeline definitions to the repo as code so nothing lives only in the Fabric UI.

Acceptance: two consecutive scheduled runs visible in run history, second run correctly skips unchanged files, reconciliation table populated for at least one plan, backfill command demonstrated, definitions committed.

## Phase 3 — AI components inside the pipeline

All three follow the same shape: deterministic baseline first, LLM only for what the baseline cannot handle, schema-constrained output, deterministic validation of every proposal, human review queue, precision/recall or accept/reject eval, cost and latency logged per call. Reuse the existing payer entity resolution harness as the template.

3a. Provider entity resolution
- Extend payer ER to providers: payer provider references (TIN, NPI lists, names, addresses) to NPPES entities. Blocking plus deterministic matching handles the bulk; LLM handles residual ambiguous cases.
- Eval harness with a labeled sample; report precision, recall, and cost per 1,000 resolutions.

3b. Schema-drift agent
- On each new drop, diff the observed file structure against the current data contract.
- If drift is found, the agent proposes a contract or mapping change with rationale, opens a branch and PR with the diff and a test update, and blocks the load until a human approves. Never auto-merge.
- Include a fixture that simulates a renamed field and a new nested key so the agent can be tested without waiting for a real drop.

3c. Data-quality triage agent
- On failed contract checks, gather context (failing row sample, historical pass rate, recent code changes), produce a root-cause hypothesis and a proposed fix, and open an issue or PR.
- Eval: a set of seeded failures with known causes; measure how often the hypothesis names the right cause.

Acceptance: all three have evals with numbers in docs/BUILT_VS_PLANNED.md, all three have a demonstrable approval path, none can change data or code without approval.

## Phase 4 — AIOps layer

- Telemetry mart over `ops.pipeline_runs`, `ops.dq_results`, and the LLM cost/latency logs: per-stage duration trend, rows/sec, bytes processed, freshness (hours since last successful load per table), Fabric capacity usage where obtainable, LLM tokens and cost per run.
- Scheduled ops agent (rule-based anomaly detection first, LLM only for the narrative and the proposal): flags duration or volume anomalies vs a rolling baseline, stale tables, cost spikes, rising DQ failure rates; proposes concrete changes (partitioning, filter pushdown, retry policy) as issues or PRs.
- A status page or README section that renders the last N runs, freshness, and open proposals.

Acceptance: agent runs on schedule, at least one real anomaly detected and one proposal produced during the build window, cost per run reported.

## Phase 5 — Operate and present

- README rewrite: architecture diagram, what each AI component does and does not do, how to run locally vs on Fabric, cost numbers, eval numbers, run history screenshot.
- ADRs for the main decisions (carrier choice, NY filter, local-parse-then-load, approval-gated agents).
- 90-second demo script: new drop lands, drift agent catches a change, human approves, load runs, reconciliation updates, ops agent reports.
- Final pass on docs/BUILT_VS_PLANNED.md. Anything not fully working is listed under planned, not built.
- Before the trial ends: confirm every definition is in the repo and the demo is recorded.

---

## Working agreement for this session

- Start in plan mode. Show me the Phase 0 output before any code.
- One phase per branch, small commits, tests with every commit, CI green before merge.
- When something turns out harder than expected, tell me the trade-off and propose a cut rather than silently narrowing scope.
- Keep CLAUDE.md updated with any new commands, env vars, or conventions you introduce.
