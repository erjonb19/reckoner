# ADR 0003 — An Azure-native lakehouse, not Fabric

- **Status:** Accepted
- **Date:** 2026-09-13
- **Supersedes:** the Fabric lakehouse in Phase 2 of `reckoner_phase3_claude_code_brief.md`
- **Does not disturb:** ADR 0001 (the payer boundary) or ADR 0002 (the storage seam)
- **Orchestration choice:** ADR 0004

## Context

Phase 2 was specified as a Fabric lakehouse: OneLake shortcuts over ADLS, notebooks,
scheduled pipelines. Three things happened.

The Montefiore tenant blocks Fabric workspace creation. A personal tenant was stood
up instead, where the Fabric **trial refused to activate**. Falling back to a paid F2
capacity then failed on quota: the subscription carries **zero Fabric capacity units
in East US**, and a quota request is neither instant nor certain.

Set against that, the F2 would have cost **$0.36/hour running, about $262/month if
left on** — for a dataset of 560 MB of payer parquet and 3.5 GB of hospital rates.

## Decision

**ADLS Gen2 remains the authoritative store. Everything above it becomes Azure-native
and Python-first.**

| layer | choice | why |
|---|---|---|
| store | ADLS Gen2, East US | unchanged; architecture rule 2 already said so |
| everyday query | **DuckDB / Polars** | the data is single-machine sized |
| heavy Spark | **Databricks Free Edition**, only when needed | no standing cost |
| scheduling | **Azure Container Apps Jobs** | scale-to-zero; pay per run |
| secrets | **Key Vault** | rule: secrets via environment, never in code |
| telemetry | **Azure Monitor** | feeds Phase 4's AIOps layer |
| reporting | **Power BI Desktop** over ADLS Parquet | free; only the service needs a licence |

## Why this is a better fit, not just a cheaper one

**The data is small.** 560 MB of payer parquet, 3.5 GB of hospital rates, 156M rows.
The largest single operation this project performs — the payer aggregation — runs in
seconds on one machine once it is sharded. Spark's distribution is overhead here, not
leverage. The reason a mart run ever took 58 GB of memory was an unbounded `to_table`,
which a cluster would have hidden rather than fixed.

**The pipeline is already PyArrow.** DuckDB queries Arrow datasets in place, so it
reads *through* the existing seam rather than opening its own connection to ADLS with
its own credentials. That keeps one authentication path and one code path, which is
the same argument ADR 0002 made against a `FabricBackend`.

**Nothing has to be torn out.** ADR 0002 deliberately built a filesystem resolver
rather than Fabric-specific code, on the reasoning that it "commits to not making
Fabric expensive to adopt". The corollary is that it made Fabric cheap to *abandon*:
`src/storage/` targets ADLS through `pyarrow.fs` and is unaffected. So is
`storage/publish.py`, the manifest, the contract, the load gate, and the 820 tests.
What is dropped is what was never built: shortcuts, a lakehouse, notebooks, and the
capacity.

**Scale-to-zero matches the cadence.** Payer files update monthly, hospital files
annually. A capacity billing by the hour to serve a monthly job is the wrong shape;
a job that costs nothing between runs is the right one.

## Consequences

- **No Spark by default.** If a step genuinely needs it, Databricks Free Edition is
  the escape hatch, and needing it is a signal to check whether the step is doing
  something unbounded — this project's history says that is the likelier cause.
- **Presentation is Power BI Desktop over the Parquet, not the Power BI service.**
  Desktop is free and reads ADLS Gen2 Parquet directly; a DuckDB export is the
  alternative where a single file is easier to hand over. What is out is the *service*
  — publishing, sharing and scheduled refresh — which needs a Pro licence or a
  capacity, and neither exists here. So Phase 5 keeps a reporting artifact; it loses
  the ability to host one. The published web artifacts already cover sharing.
- **DuckDB joins the dependency list.** ADR 0002 noted, correctly at the time, that
  the project had no DuckDB dependency and never had. That changes here, and the
  distinction matters: DuckDB arrives as a *query engine over Arrow*, not as the
  storage backend that ADR declined to abstract over.
- **Cost becomes per-run rather than per-hour**, which makes it measurable. Phase 4's
  telemetry can report it honestly instead of amortising a standing capacity.
- **The region constraint disappears.** East US throughout, with no quota to request.
