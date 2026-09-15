# ADR 0004 — Container Apps Jobs for scheduled runs

- **Status:** Accepted
- **Date:** 2026-09-14
- **Context for:** ADR 0003's scheduling row

## Context

Phase 2 needs something to run the pipeline on a schedule: fetch new payer files,
parse, publish to ADLS, verify, and emit telemetry. The cadence is low — payer files
update monthly, hospital files annually — and the budget is "as close to zero as
possible".

Three candidates, and the constraints that decide between them are memory and
timeout, not price. Every option here is free at this cadence.

## The measured constraint

This pipeline's peak resident memory is **9,808 MB**, observed on the sharded NYU
Langone mart. That number, not cost, is what rules options in and out.

| option | free allowance | memory ceiling | timeout | verdict |
|---|---|---|---|---|
| **Container Apps Jobs** | 180,000 vCPU-s + 360,000 GiB-s / month | **4 vCPU / 8 GiB** | hours | **chosen** |
| Azure Functions (Consumption) | 1M executions + 400,000 GB-s | **1.5 GB** | 10 min default, 60 max | rejected |
| GitHub Actions | unlimited for a public repo | ~7 GB (standard runner) | 6 h | rejected |

## Decision

**Azure Container Apps Jobs, Consumption profile.**

**Functions is out on memory.** 1.5 GB against a pipeline that has needed 9.8 GB is
not a close call, and the 10-minute default timeout is under the 15 minutes a single
sharded mart run already takes. Making it fit would mean restructuring the work to
suit the runtime, which is the tail wagging the dog.

**GitHub Actions is out on data locality.** Minutes are free for a public repo, and
OIDC federation would authenticate without a stored secret, which is genuinely
attractive. But the data is 4 GB sitting in ADLS in East US, and a GitHub runner is
outside Azure: every run would pull that across the internet, paying egress and
latency to compute next to nothing. Actions stays where it belongs — CI, which it
already does.

**Container Apps Jobs fits the shape.** Scale-to-zero with no idle charge, so a job
serving a monthly cadence costs nothing between runs. At 2 vCPU / 4 GiB a 30-minute
run consumes 3,600 vCPU-seconds and 7,200 GiB-seconds, against a monthly free
allowance of 180,000 and 360,000 — roughly **50 such runs a month before anything is
billed**, which is far more headroom than a monthly job needs.

## The ceiling, stated plainly

**8 GiB is below this pipeline's observed peak of 9.8 GB.** That is not a reason to
reject the choice, but it is a constraint to design against rather than discover:

- The 9.8 GB peak came from the *analysis* mart, not the scheduled ingest. The
  scheduled path is fetch, parse, publish, verify — all of which stream.
- Sharding is already the knob. `--all-shards` exists precisely because a whole-system
  pass did not fit in memory, and a finer shard lowers the peak further.
- Databricks Free Edition is the escape hatch, per ADR 0003, for any step that
  genuinely will not fit. On this project's history an operation that needs more than
  8 GiB has usually been unbounded rather than large, so hitting the ceiling is a
  signal worth reading before it is a limit worth raising.

## The management meter, and why it does not apply

The retail rates list an **Environment Management Hour at $0.10/hour**. Left
running that is about $73 a month, which would exhaust the $5 subscription budget
on its own, so it is worth stating why it is not charged here rather than
assuming.

That meter applies only to an environment with a **dedicated workload profile**,
a **private endpoint**, or **planned maintenance** enabled. Verified against the
live environment on 2026-09-15:

| condition | checked | result |
|---|---|---|
| workload profiles | `containerapp env show` | **`Consumption` only** — one profile, type `Consumption` |
| private endpoint | `network private-endpoint list` on the resource group | **none** |
| VNet integration | `properties.vnetConfiguration` | `null` |
| zone redundancy | `properties.zoneRedundant` | `false` |

So the environment itself is free and only job executions bill. If a dedicated
profile or a private endpoint is ever added, this meter starts and the budget
alert at 50% of $5 is the thing that will notice.

**Confirmed against billing, not just configuration.** The table above is a
prediction from the environment's settings; this is the invoice. Cost Management
(`Microsoft.CostManagement/query`, 2026-09-01 to 2026-09-16, grouped by
`MeterCategory`/`Meter` and again by `ServiceName`/`ResourceId`) returns **no
Container Apps meter of any kind** — no environment management hour, no vCPU-second,
no GiB-second — across roughly two days of a live environment and four job
executions. Zero-cost meters do appear in that output (`Standard Data Transfer In`,
`Analytics Logs Data Ingestion`), so absence here is absence, not a filtered zero.

The whole subscription bills **$0.033** for the period:

| meter category | meter | cost |
|---|---|---|
| Storage | Hot LRS Write Operations | $0.010218 |
| Storage | Hot LRS Other Operations | $0.002837 |
| Storage | Hot LRS Read Operations | $0.001727 |
| Storage | Hot Iterative Read Operations | $0.000371 |
| Storage | Hot LRS Data Stored | $0.000178 |
| Storage | Hot Other Operations | $0.000008 |
| Log Analytics | Analytics Logs Data Ingestion | $0.000000 |
| Bandwidth | Standard Data Transfer In / Out - Free | $0.000000 |
| Microsoft Fabric | Compute Pool Capacity Usage CU | **$0.017706** |

The storage line is dominated by write operations — hospital silver was published
twice, once with the wrong codec (`docs/silent-failures.md`, entry 6).

The Fabric line is not a running cost. It is two F2 capacities created in
`westus2` and `centralus` as a quota probe and deleted within a minute, before
ADR 0003 dropped Fabric entirely. A read-only quota check was available and was the
right tool; this is what using `create` to ask a question costs. Both are gone, the
meter is closed, and it is recorded here rather than netted out of a total.

## Consequences

- Jobs are defined as code in the repo, image and job definition together. There is no
  UI to export from, which is the point.
- A job that exceeds 8 GiB fails rather than silently paging, which is the failure
  worth having.
- Cost is per-run and therefore measurable, so Phase 4 can report it honestly instead
  of amortising a standing capacity.
