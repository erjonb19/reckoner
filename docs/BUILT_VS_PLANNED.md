# Built vs planned

What is actually built, what is scaffolded, and what has not been started. Kept honest on
purpose: this file is the reference for what may be claimed about the project, so anything
not fully working is listed under planned, not built.

**This is a personal project.** It is deployed, scheduled, tested and monitored, but no
real users and no business decisions depend on it. Nothing here is production experience.

Last updated: 2026-09-23.

**Since the last revision (2026-09-09 → 2026-09-23):** three more hospital systems
ingested (15 in the lake, 6 reconciled), a gold layer published by a cloud job, a
generated report and a public page, A1's agent loop and eval harness, a refusal
decomposition, a report-only fuzzy plan matcher, and a Log Analytics workbook. The
sections below are updated in place; the older findings further down are kept, dated,
because they are still true of what they measured.

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

**Measured:** 184,424,339 curated rate lines across **15 health systems**. The last
three (White Plains, the Westchester Medical Center network, Montefiore; 27,940,062
rows) were fetched by hand, because both sites block automated access. They were served
to the unchanged ingest over localhost, so they went through the same parse, quarantine,
checksum and `LOAD_AUDIT` path as the other twelve.

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
- **Gold, sharded and shard-invariant** — `src/reconcile/gold.py`, `src/pipeline/mart.py`.
  One system per execution, sharded by code prefix, then facility, then carrier, with the
  pairs streamed rather than collected. Every split is exact: the join key contains each
  split dimension, so no pair straddles one. Offsets are fixed once over the whole system
  at `close()`.
- **Reproducible across images, measured.** Mount Sinai, Northwell and NYP were rebuilt on
  2026-09-23 with a newer image. `coverage`, `outcomes`, `exemplars`, `magnitude` and the
  triage queue came out identical to the earlier builds. Only `refusals` changed, gaining
  the carrier grain added in between.
- **Report and page** — `src/pipeline/report.py` publishes gold as `summary/*.csv`,
  `summary/run.json` (with a no-PHI caveat) and `docs/reconciliation-report.md`.
  `streamlit_app.py` reads only those files and makes no network calls; it is live at
  <https://reckoner-ny.streamlit.app>.
- **Refusal decomposition** — `src/pipeline/levers.py`, `docs/refusal-decomposition.md`.
  Every refusal, by reason, system and carrier, with the lever that could recover it.
  It won't write the document unless candidates equal pairs plus refusals for every
  system, and they are equal for all six.
- **Vintage alignment** — `src/pipeline/vintage.py`: the gap between the two sides per
  hospital and carrier, reported as a distribution rather than a single figure.

### Cross-source reconciliation scope

- `docs/scope.md` — **six systems reconcile**: Mount Sinai, Northwell, NYU Langone,
  NewYork-Presbyterian, WMC and White Plains. A seventh, Montefiore, is ingested and
  verified in silver but not reconciled, because its cloud run is OOM-killed (see below).
  The other eight are hospital-side only **by design**, having never been payer-side
  targets. Not a gap.
- **`--assume-facility-when-unstated`** reads an absent hospital `billing_class` as `facility`,
  scoped by `reconcile.curated.facility_only_hospitals` — computed from the data, so a system
  that publishes any professional row is excluded automatically. Excludes exactly Maimonides
  (121,119 professional rows) and Upstate (6,418) today.
- It resolves to `facility` **specifically**, not to "compatible with anything", so a payer
  professional rate is still refused. On NYP that guard fires on 2,020,625 rows.
- Every assumed pair carries a note into the variance row and A1's queue.
- **Measured: NewYork-Presbyterian went from 0 pairs to 106,852.**
- **Cached per lake state.** `reconcile.eligibility` keys the answer to a fingerprint of the
  lake's file paths — metadata only, no rows read — and writes it to `_meta/` with the
  evidence per system (`professional_rows`, `eligible`, `computed_at`). Measured on the real
  lake: **19.49s cold, 0.01s warm**. A stale fingerprint forces a rescan rather than
  refreshing in place, because a stale answer would apply the assumption to a system that has
  since started publishing professional rates — the one case it is plainly wrong for.
- 29 tests, including one Maimonides case that must keep refusing and one proving a moved
  lake re-keys the cache.

### Payer data contract

- `src/payer/contract.py` — the boundary `mrf_pipeline` writes and `payer/curated.py` reads,
  declared in code. Three kinds of rule: shape (`string` and `large_string` are one logical
  type, so the 104/16 split across files is not a violation); domain (`billing_class` and
  `rate_type` are checked against the **federal TiC enums**, not against what we happened to
  observe); and the per-file invariants live code already assumed — one vintage per file,
  because `_file_vintage` reads row one and calls it the file's, and a `payer` column that
  agrees with the filename, because `discover_payer_files` derives carrier and network from
  it. Both were true and neither was checked.
- Severity follows consequence rather than tidiness. An empty `billing_code` is a **warning**:
  the code is the join key and the hospital side has zero empty codes, so the row cannot
  mis-join, only fail to match. An empty `billing_class` is an **error**, because that one is
  mis-grouped silently.
- Reports, never raises — rows are quarantined with a reason, per architecture rule 4.
- **Enforced at load time.** `discover_payer_files` consults the contract on every call and
  quarantines a file that would break the read — kept out of the dataset, kept visible in
  `file_summary` with its reason, per architecture rule 4. It defaults to a footer-only tier
  costing **0.07s across all 120 files**, against roughly two minutes to read every column;
  a gate nobody can afford to leave on is not a gate. `ContractCheck.FULL` opts into the
  thorough one.
- **The gate quarantines on what breaks the read, and no more.** The contract knows 18
  columns; the reader projects 10. A file missing one of the 10 is dropped, because the next
  `to_table` would raise; a file missing one of the other 8 is kept, because it is perfectly
  readable. Turning the gate on before that distinction existed quarantined every trimmed
  fixture in the suite — 33 tests — which is what over-strictness costs in real data. A test
  keeps the contract's required set in step with `NEEDED_COLUMNS`.
- **Measured against the real lake: 120 files, 59,501,435 rows, 0 errors, 49 warnings**
  (196 blank billing codes, all Emblem), 0 quarantined. 30 tests, built by damaging a real
  file one column at a time.

### A1 variance triage (deterministic half)

- `src/agents/variance_triage.py` — the residual surviving every deterministic explanation
  is unworkable as rows: 2,070 on one shard of one system. Grouped by service and carrier it
  is **718 ranked items**, and the pairs mart reports them.
- **This is the only agent with a real long tail.** A3 has no non-conforming file and A4 no
  non-empty diff; A1's residual is 1.8% of 113,718 pairs and genuinely unexplained.
- **The grouping deliberately does not average.** Only 28% of repeated services hold a ratio
  spread under 10%, so the repeats are real plan-level variation rather than duplicates.
  Spread is reported and drives the class: tight across plans is one contract-level fact,
  wide is a question about plans, single is the weakest evidence there is.
- **92% of the queue is one carrier**, reported as `largest_carrier_share`, because that
  changes what the queue is — one relationship to investigate rather than hundreds of
  findings.
- Ranking is symmetric in direction (0.5× ranks with 2×) and caps the evidence weight, so
  the fan-out cannot buy priority through repetition.
- 15 tests.

**Near-miss rules, as a job stage.** `src/pipeline/triage.py` (`--stage triage`) runs over
gold's exemplars with five rules whose thresholds are deliberately looser than the mart's.
They catch findings that fell just the wrong side of a line. On the published queue they
account for **166 of 200** findings: 91 near-offset and 75 vintage. **34 stay
`unexplained`**, and those are what the agent exists for.

**The agent loop is built; no model has been run.** `src/agents/triage_agent.py` has:

- a proposal from a fixed vocabulary of eight causes, which must cite its evidence fields;
- a deterministic `validate` that checks the vocabulary and the cited fields, and
  preconditions where a cause has one (a vintage artifact needs a 30-day gap, a units
  mismatch needs a ratio of 3× or more);
- retries bounded at three, only for rate limits, 5xx, connection errors and unparseable
  output, with the rejection reason fed back to the model;
- a human queue for anything refused, low-confidence or out of attempts;
- a cost and latency on every attempt, including failed ones. An unpriced model costs
  `None`, not $0.

`src/agents/triage_evals.py` scores any triager from `evals/triage_labels.csv`. **The
labels are in: all 250 findings.** The rules baseline scores precision 0.000 and coverage
0.884. That is by construction: its only `units_or_methodology` route needs a ratio of 10×
or more, which never occurs in the queue, and 210 of the 250 labels are that cause
(`docs/labelling-a1.md`). `scripts/run_a1_eval.py` runs the agent behind a pluggable
provider, Gemini 2.5 Flash on the free tier by default. It paces for rate limits and
resumes after a daily cap. The first real run is waiting on a key.

Two hypotheses about the residual were tested and **both failed**, which is why neither is a
rule: ratios do not cluster near integers (8.5% within 5% of one, so not a units multiple),
and the codes do not concentrate in a few families (top 6 of 90 cover 21.6%).

### A3 schema adaptation (deterministic half)

- `src/hospital/conformance.py` — when a file yields no curated rows, say *which kind of
  nothing*. The ingest recorded both cases as "no curated rows produced", and they need
  opposite responses: a file we could not read needs an adapter; a file with nothing in it
  to read needs nothing at all.
- **The distinction is real and was costly.** Mount Sinai Brooklyn is 82 MB, CMS template
  3.0.0, **217,957 charge items and not one `payers_information` entry** — gross and cash
  prices only. Establishing that took a full download and six manual probes. It is now a
  line in the audit, and the verdict is `no_negotiated_rates`, *not actionable*.
- Required one new observation the ingest was not collecting: `MrfParser.items_seen` and
  `structure_found`. Yielded rates alone cannot separate the two cases — both are zero.
- 12 tests, with the gross-and-cash document copied in shape from the real file.

**The generative half is not built, and the reason is evidence rather than effort.** A3
exists to write parser adapters for non-conforming files, and there are none: all 12 systems
parse into one of three known layouts, and the only two files yielding nothing yield nothing
correctly. An adapter generator today would have no non-conforming file to be tested
against — exactly the condition guardrail 1 forbids, generated code no deterministic check
can validate. `STRUCTURE_NOT_FOUND` is the verdict that would trigger it, and it has never
once fired on real data.

### A4 ingest monitoring (deterministic half)

- `src/agents/ingest_monitor.py` — the manifest says *what* moved; this says whether anyone
  should care. Every verdict is a claim about consequence: a vanished file makes a figure
  unreproducible, a moved vintage is structural by the standing rule, a schema change may
  quarantine the file at the next load. The one **cosmetic** verdict — rewritten at the same
  vintage, row count, schema and state — states the assumption it rests on in its own reason
  string, and names `--hash` as the way to settle it outright.
- Wired into the daily task, so a scheduled run now reports materiality, not just a diff.
- 18 tests, built by moving a real payer file. Includes a fake assessor that lies, to prove
  an unverifiable assessment is surfaced rather than dropped.

**The agent half is deliberately not built**, and that is the build order rather than an
omission: CLAUDE.md says deterministic first, agent second, *once the tables show where the
long tail is*. Every diff observed so far reports no change at all, so there is no
distribution of hard cases to measure — writing an LLM classifier now would mean inventing
one and then scoring it against labels invented from the same imagination. What exists is
the shape it slots into: an `Assessor` protocol, `validate_assessment` already rejecting
invented files, fields and verdicts, and a review queue that is the eval set when it starts
filling. Until then the queue is the honest answer.

### Payer file manifest

- `src/payer/manifest.py` — a snapshot of the boundary: one row per file with vintage, row
  count, and an identity taken from the Parquet footer rather than a hash of the bytes, so
  it is cheap enough to take on every run. `--hash` computes a real SHA-256 when a specific
  claim needs one; it reads 4.3 GB and is not the default.
- **The diff is the part that earns its keep.** Two snapshots turn "the payer data changed"
  into a named list — added, removed, and per-file field changes. The change most likely to
  go unnoticed is a re-parse at a newer vintage under the same filename, and that is a
  test.
- **Measured: 120 files, 118 read, 2 duplicates, 56,784,415 rows** across 5 vintages. A
  second snapshot diffs clean against the first.
- **Taken on a schedule.** `scripts/snapshot_payer_manifest.ps1 -Register` installs a daily
  Windows Scheduled Task that snapshots into `data/manifests/`, diffs against the previous
  one, prunes to 30, and logs. Verified end to end through Task Scheduler, not just by
  running the script by hand. It runs locally by necessity: the payer Parquet is a
  gitignored 4.3 GB directory in a sibling repo, so CI and cloud schedulers cannot see it.
  The task runs as the current user while logged on, so no credential is stored.
- A first run reports "nothing to compare against" rather than an empty diff — those are
  different statements and only one is reassuring.
- 26 tests, including two that pin down what it refuses to claim and two regression tests
  for snapshot naming (see below).

**What it cannot tell you, by construction.** A payer file that parsed but matched no target
hospital leaves no Parquet, so its absence is identical to never having been attempted —
182 of ~280 Emblem files are in that state. That fact lives upstream in `mrf_pipeline`'s
config and logs, on the far side of the boundary ADR 0001 draws. The manifest defines no
"expected but missing" state rather than guessing at one; what it does instead is make the
absence *enumerable after the fact*, since a file that vanishes between two snapshots is
named.

### A2 entity resolution

- Payer-level and plan-level matchers — `src/agents/entity_resolution.py`, `plan_resolution.py`.
- Eval harness reporting precision/recall/F1 with abstention treated as a recall cost, not
  an error — `src/agents/evals.py`. Results appended, never overwritten.
- 190 reviewed labels — `evals/plan_matching.jsonl`.
- Per-call cost and latency logged (`CallCost`), so the agent's price is a number.
- **A fuzzy plan pass, report-only** — `src/agents/plan_fuzzy.py`, `docs/plan-matching.md`.
  It uses aliases with confidence tiers, and nothing in the mart calls it. Measured over
  the real plan space: plan-level matchable share goes **13.87% → 14.72%** of hospital rate
  rows. Comparable-share lift is **0.00 pp, by construction**, because an unresolved plan is
  an explanation, never a refusal. Precision on the reviewed set is unchanged (0 false
  positives), but none of those labels covers a case the pass changes. 25 proposed
  labels are waiting unreviewed in `evals/plan_matching_proposed.jsonl`.

### Engineering

- **1,134 tests**, all passing. Parser tests are
  built from real files, not from the CMS spec.
- `docs/silent-failures.md` — 13 bugs that reported success and were wrong, each with the
  check that now catches it.
- `mypy strict`, `ruff` with a broad rule selection, CI gating every push in both repos.

---

### Scheduled pipeline on Azure (Phase 2)

- **ADLS Gen2 `reckonerlake0914`** (East US, HNS on) holds two layers, both verified by
  reading back through `storage.resolve()` rather than trusting the upload log:
  - **bronze/payer_tic** — 118 files, 56,784,415 rows, 559,607,543 bytes.
  - **silver/hospital_rates** — 93 files, 156,484,277 rows, 3,749 MB, partitioned
    `hospital_slug/code_type/vintage` (73 partitions, median 56,016 rows), every system's row
    count checked against the source before the manifest was written.
  - **silver/payer_rates** — 14 files, 56,784,415 rows, 438 MB, partitioned `carrier/vintage`,
    derived from bronze rather than from the parser a second time. Compaction falls out of the
    key: EmblemHealth's 98 files, one per plan, share a carrier and a vintage and become one.
    118 files in, 14 out, and 22% smaller than bronze on the same rows.
  - **silver/hospital_rates is now 122 files, 184,424,339 rows**, republished and verified
    with the three new systems.
  - **gold/** — seven tables per system, written by two stages: the mart writes five and
    triage writes two.
  - **Total 5.282 GB, 0.282 GB over the 5 GB free tier: $0.0056 a month, measured.** The
    pre-write projection said $0.079. It errs high by design, because it doesn't model
    partitions being replaced.
- **Container Apps Job `reckoner-pipeline`** — schedule `0 6 1 * *`, 2 vCPU / 4 GiB, image from
  ghcr.io, authenticating with a user-assigned managed identity. One green run on demand;
  the first scheduled firing is 1 October.
- **Startup memory check.** Every execution logs the cgroup-reported ceiling against the
  stage's measured peak. Confirmed against a live container: `memory_ceiling_mib: 4096`,
  matching the job definition.
- **Log Analytics `reckoner-logs`** — 0.5 GB/day cap, 31-day retention.
- **Cost, measured:** a 36-second execution consumed 72 vCPU-s and 144 GiB-s = **$0.00216 at
  list price, $0.00 after the monthly free grant** (0.04% of it). The $0.10/hour environment
  management meter does **not** apply — verified Consumption-only profile, no private
  endpoint, no VNet (ADR 0004).
- **Stage 1 (`--stage manifest`) is wired.** It diffs every layer against the manifests
  that described them — files, rows and bytes per carrier and per hospital — and emits one
  `manifest_group` record per group plus a `manifest_summary`, carrying the Log Analytics cap
  status so a capped day is visible rather than silent. Row counts come from the Parquet
  footers, not the blob listing: a file can be the right size and the wrong content. **A
  mismatch exits non-zero**, so the execution reports Failed rather than Succeeded with a bad
  diff buried in the logs.
- **Stage 1 checks four layers**: bronze, both silvers and gold.
- **Stage 2 (`--stage mart`) reconciles silver into gold.** `reckoner-mart`, 4 vCPU /
  8 GiB, **scheduled `0 8 1 * *`** again as of 2026-09-23. Since ADRs 0005 and 0006,
  each hospital rate is compared once, against the carrier's distribution for the same
  service and billing class. Every system is published by the cloud job:

  | system | hospital rates | compared | raw share | like-class share | residual | peak RSS of 8,192 MiB |
  |---|---:|---:|---:|---:|---:|---:|
  | Mount Sinai | 1,348,398 | 625,523 | 46.39% | 47.67% | 54,637 | 1,960 |
  | WMC | 170,710 | 58,238 | 34.12% | 34.12% | 0 | 1,674 |
  | NYU Langone | 9,300,114 | 2,000,760 | 21.51% | 26.02% | 50,300 | 4,029 |
  | White Plains | 350,530 | 66,542 | 18.98% | 19.75% | 212 | 1,668 |
  | Montefiore | 2,597,798 | 427,064 | 16.44% | 17.17% | 3,760 | 2,431 |
  | Northwell | 3,001,740 | 426,316 | 14.20% | 16.89% | 62,169 | 2,198 |
  | NewYork-Presbyterian | 504,350 | 18,387 | 3.65% | 3.83% | 0 | 1,655 |

  **The OOM is fixed (#47, closed).** A per-step profile in the container (#82) found it:
  the hospital-side aggregate's `approximate_median` kept a t-digest per group, outside
  Arrow's pool. On NYU shard `1` that took RSS from 628 MiB after the scan to 6,269 after
  the aggregate. Pruning and readahead were measured and ruled out. An exact median (#83)
  took the shard's high-water from 8,028 to 1,634 MiB. It also corrected a bias: on
  groups of two rates the approximate median returned the lower value in 4.9% of cases.

  **Untested:** the scheduled run does all seven systems in one execution. Per system
  they sum to 87 minutes against a 10,800 s timeout (raised from 7,200 on 2026-09-23), and memory carried between systems
  hasn't been measured. The first run is 1 October.

  Verification caught three gaps in my own earlier fixes during these rebuilds (#85,
  #87, #89): the mart claiming triage's tables, an emptied table keeping last run's rows,
  and a triage summary written outside every partition. Each would otherwise have
  published stale or unverified gold.
- **Stage 3 (`--stage triage`)** writes the near-miss queue into gold and rewrites the gold
  manifest, which it didn't do before #75.
- **Stage 4 (`--stage report`)** republishes gold as the summary dataset and the written
  report. It reads the gold schema as the union across partitions (#74). Previously a
  column added in a newer partition was silently dropped for every system.
- **Workbook** — `deploy/workbook/`: run history per attempt, duration per stage, peak RSS
  against the ceiling, manifest match per layer. Every query was run against
  `reckoner-logs`, and a test fails if one filters on an event the code no longer emits.
  **Imported** on 2026-09-23 as the shared workbook "Reckoner pipeline" in `rg-reckoner`,
  after the owner registered the `Microsoft.Insights` provider. Its queries were read back
  and are identical to the committed file.
- **Not built:** the `contract`, `verify`, `publish` and `eligibility` stages still log
  `stage_not_implemented`.

## Scaffolded

Real code, but not yet load-bearing.

- **Storage seam for the cloud.** `src/storage/` — implements ADR 0002. `resolve()` returns
  a root and a `pyarrow.fs` filesystem, defaulting to local with no configuration; both
  readers take an optional `Location`. Verified transparent on the real lake: 156,484,277
  hospital rows and 56,784,415 payer rows identical with the seam and without it. **No longer
  scaffolded** — the ADLS path has published 156M rows to a real account and been read back
  from a container job.
- `src/storage/publish.py` — the "cloud load" half of ADR 0001's "local parse then cloud
  load", which nothing implemented before: the seam could only read. Streams via
  `write_dataset` so an 89M-row system never materialises, and **verifies by reading back**
  rather than trusting the write.
- **Publishing repairs the #13 partition corruption rather than copying it.** The local lake
  still holds paths written before that fix, where a US-format date was sliced mid-field and
  its slashes read as directory separators. Rochester Regional carries both `vintage=2026-04`
  and `vintage=4/1/202` for the same month; published, they collapse to one clean partition
  with all 841,244 rows intact. Copying bytes would have carried the defect into the
  authoritative store.
- Proven locally end to end on two real systems — Crouse Health (216,206 rows) and Rochester
  Regional (841,244) — into one shared tree. The cloud run is the same code with a different
  filesystem.
- **Provenance.** `src/reconcile/provenance.py` attaches vintage spans and caveats to
  reported figures. Wired into the mart; not yet surfaced in every artifact.
- **Discovery at scale.** The crawler works, but 5 of 8 probed health systems return HTTP
  403 to automated `cms-hpt.txt` requests, so full-automatic discovery is not a claim I can
  make.

---

## Not started

- **A1's model run.** Labels are in, and the runner is built and resumable across days:
  `scripts/run_a1_eval.py --provider gemini --record` runs Gemini 2.5 Flash on the free
  tier. The provider is pluggable, and Anthropic stays available behind `--budget-usd`.
  Waiting on a run with `GEMINI_API_KEY` set.
- **A1 follow-up: a component-pricing detector in deterministic triage, then a stratified
  re-label.** *Motivation:* 210 of 250 A1 labels are `units_or_methodology`, nearly all
  component-versus-facility mismatches, so the agent's score on this queue mostly measures
  one skill. *Detector:* for one facility and carrier pair, a code family whose ratios sit
  consistently near ~6× or ~0.1×, where the hospital rate looks like a component (a
  professional or technical component, or a single unit of a multi-unit service). That is
  a rule, not a judgement, so it belongs in `pipeline/triage.py` beside the other near-miss
  rules. *Then:* regenerate a smaller queue without what the detector explains, and
  re-label it stratified by cause, so the agent is measured on the causes the rules cannot
  reach.
- **A2 v2: an employer-group-to-network crosswalk, built from the TiC index files.**
  *Motivation:* plan-level matching by string reaches 15.0% of hospital rate rows, and
  UnitedHealthcare stays **82% unmatchable** (`docs/plan-matching.md`). Hospitals name the
  employer who bought the plan ("APWU HEALTH PLAN 1027", "SCREEN ACTORS GUILD 1220"), not
  the network, and no alias table can recover a network from an employer's name.
  *Source:* each carrier's table-of-contents file lists, for every in-network file, the
  `reporting_plans` that use it: `plan_name`, `plan_id` (an EIN for employer plans) and
  `plan_market_type`. `mrf_pipeline/find_files.py` already reads those entries to pick
  files, then discards them, so the parsed Parquet carries no plan identity.
  *Build:* keep that index as a small table (employer plan → EIN → in-network file →
  network label), and match hospital plan strings to employer `plan_name`s: normalised
  exact first, fuzzy second, with the plan codes some hospitals append as a tiebreak.
  This reads only the index, never a rate file, so it respects "parse once".
  *Gate:* the same as today's matcher. Precision on reviewed labels first, with an
  employer-group label set built from the matches it proposes, because the current 190
  labels contain none. *What it would unlock:* a plan-level join for the matched share, and
  a measured answer to whether ADR 0006's distribution grain should give way to plan
  grain where the plan is known.
- **A3's generative half.** Blocked by evidence, not capability: no hospital file in the
  corpus is non-conforming.
- **A4's agent half.** Blocked by build order: every manifest diff so far reports no
  change.
- **Dropping the raw share.** ADR 0005 reports raw and like-class shares side by side
  for one release. Whether to keep both after that is an open decision.
- **The `contract`, `verify`, `publish` and `eligibility` job stages.** Each one runs
  locally as a CLI; none is a cloud stage yet.
- **Key Vault.** Not needed so far: the only credential is a managed identity, and no key,
  SAS token or connection string exists anywhere.
- **Fabric is dropped, not deferred.** See ADR 0003.

---

## Findings, with the caveats attached

### Refusals and levers (2026-09-23, seven systems, ADR 0006 grain)

Reproduce with `python -m pipeline.levers`; details in `docs/refusal-decomposition.md`.

- **17,273,640 hospital rates, 3,622,830 compared: a raw share of 20.97%, and 24.15%
  like-class.** Every count is now a hospital rate.
- **The largest fixable lever is payer-name matching**: 5.63% of hospital rates name a
  payer string that never resolved to a carrier. With billing class settled by
  definition (ADR 0005), it tops the ranking.
- **36.78% of comparisons are `plan_unresolved`**: the hospital plan matches none of the
  networks in the carrier's distribution. That motivates A2 v2 (below).

### Refusals and levers (2026-09-23 morning, six systems, the old pair grain)

Recorded from that morning's summary. The grain has changed since, so these figures no
longer reproduce; they are kept because the ADR 0005 decision was made on them.

- **286,599,414 candidates, 15,123,140 pairs: a pooled comparable share of 5.28%.**
- **70.92% of candidates are billing-class refusals**, mostly correct ones. A professional
  rate meets an institutional one because the join doesn't key on billing class. That is
  a definition to decide, not a lever to pull.
- **20.16% are TiC-exempt products** (Medicare Advantage, Medicaid). Correctly refused, by
  rule.
- **Plan matching and vintage tolerance recover nothing** under the current join. Plan
  matching moves the 42.5% of pairs explained only as `plan_unresolved`, not the share.
- **A high share is a smaller file, not a better result.** WMC's 66.30% comes from 256,608
  pairs, which is fewer than Mount Sinai's refusals alone.

### Earlier findings (2026-09-10, range mode, four systems)

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
- **Cross-source agreement varies far more by system than by payer**, and no single figure
  describes it: Mount Sinai 70.4% of insurer rates inside its hospitals' published range,
  NYU Langone 16.1%, NYP 14.8%. Quoting the 70.4% alone would be quoting the outlier — see
  the table below before using any of these numbers.
- **A published "price" is a spread, not a number.** The median range for one service at one
  system runs 1.18× to 1.78× wide before any payer is named. This one holds across every
  system measured.

### Cross-source coverage is narrower than the totals suggest

Both sides must name the same system. The hospital lake holds 12 systems; the payer target
list holds 7; **4 systems appear in both** — Mount Sinai, NYU Langone, NewYork-Presbyterian,
Northwell. Only those 4 can be reconciled at all, regardless of row counts.

| System | Facilities | Comparisons | Inside range | Median range width | Carriers |
|---|---:|---:|---:|---:|---:|
| Mount Sinai | 8 | 5,609 | **70.4%** | 1.78× | 5 |
| Northwell | 24 | 6,897 | **50.9%** | 2.14× | 4 |
| NYU Langone | 4 | 20,438 | **16.1%** | 1.18× | 5 |
| NewYork-Presbyterian | 3 | 5,450 | **14.8%** | 1.50× | 4 |

All four reconcilable systems are now measured.

**Mount Sinai is the outlier, and 70.4% should not be quoted as the project's headline.**
Two of the three systems measured sit near 15%. Any claim of the form "the two disclosures
agree about 70% of the time" rests on the one system that behaves least like the others.

**Two explanations were proposed and both are dead.** They are recorded because the
negative results are the durable part; the mechanism is still unknown.

*Range width.* "Inside range" asks whether an insurer's rate falls between the cheapest and
dearest price a system's hospitals published, so a narrower range is a smaller target and
should score lower. The widths do not order with the shares: NYU is the **narrowest**
(1.18×) yet beats NYP (1.50×), and Northwell is the **widest** (2.14×) yet sits 20 points
below Mount Sinai (1.78×).

*Facility count.* This survived three systems — 8, 4, 3 against 70.4%, 16.1%, 14.8% — and
Northwell killed it. Northwell discloses **24 facilities, three times Mount Sinai's eight,
and scores 20 points lower.**

What the four points do show is a **split, not a gradient**: two systems land at 51–70% and
two at 15–16%, with nothing in between. The split lines up with both disclosure breadth and
range width, but those two are confounded — more facilities tends to mean a wider range —
and neither orders the systems *within* the groups. So something separates
{Mount Sinai, Northwell} from {NYU, NYP} and it is not any single variable measured here.
**Do not quote any of these four as "the" agreement rate.**

**Northwell is the clearest evidence that volume is not coverage.** It publishes 89M rate
lines — more than the other three systems combined, and 6.6× NYU's — and yields the fewest
comparisons per row of data by a wide margin:

| System | Hospital rows | Comparisons | Comparisons per 1M rows |
|---|---:|---:|---:|
| Mount Sinai | 1.83M | 5,609 | 3,065 |
| NewYork-Presbyterian | 2.79M | 5,450 | 1,946 |
| NYU Langone | 13.46M | 20,438 | 1,514 |
| **Northwell** | **88.96M** | **6,897** | **77** |

That is a **20–40× lower yield**, and it is the `billing_class` gap made concrete: Northwell
states it on 1.6% of rows, and a hospital rate with no billing class cannot be matched to an
insurer's without assuming a facility rate is a professional one. The single largest
publisher in the state is very nearly unreconcilable, for a reason that has nothing to do
with how much it publishes. One shard in the run showed it plainly: 79,024 payer rows met
760 hospital rows and produced zero comparisons.

Two further observations that hold across systems:

- **A published "price" is a spread, not a number** — 1.18× to 1.78× wide at the median for
  one service at one system, before any payer is named.
- **Misses skew low.** NYU's insurer rates fall below the hospital range about 1.5× as often
  as above (10,166 vs 6,991); NYP's split 1,870 below to 2,771 above. Neither is the even
  split a pure narrow-band artifact would give.

NYU's four facilities carry a `|` in their published names (`NYU Langone|Tisch Hospital`).
That is how the value arrives in the source and it separates four genuinely distinct
hospitals, so it is cosmetic rather than a resolution failure.

**NYU ran only after the payer side could be sharded.** Before #18 it reached ~58 GB of
virtual memory on a 15.6 GB machine and took the terminal down with it three times (Windows
Resource-Exhaustion events, 2026-09-09 19:44 and 19:51, 2026-09-10 07:52). The cause was
`aggregate_rates` materialising the whole filtered payer table and then taking a DISTINCT
over all ten columns, so peak memory scaled with rows entering the call rather than results
leaving it:

| System | Rows into `to_table()` | Before #18 | With `--all-shards` |
|---|---:|---|---|
| NYP | 5,165,289 | completes | completes, ~2 min |
| Mount Sinai | 8,762,681 | completes | — |
| Northwell | 9,663,999 | untested | completes, ~5 min |
| NYU Langone | 15,149,291 | **exhausted memory** | completes, ~15 min |

Peak resident memory across the sharded NYU and Northwell runs was **9,808 MB** on a
15,600 MB machine — bounded and oscillating per shard, against the unbounded climb to 58 GB
before. Still 63% of the machine at peak, so the headroom is real but not generous; a system
materially larger than NYU would want a finer shard than one character.

`--shard` had also never worked: it called `Expression.starts_with`, which pyarrow does not
define, so the flag raised `AttributeError` whenever it was used. It shipped that way in #14
because `mart_cli` had no tests. Both are fixed in #18, and a sharded run is now checked
against an unsharded one — on NYP they agree to full precision on every field.

---

## Corrections

- `mart_cli --mode pairs` produced **zero pairs from #14 until #26**. It joins on the
  provider; the hospital side names a facility and the payer side resolves only to a system,
  and the runner passed no map between them, so every hospital row was excluded as having no
  counterpart. The mode whose docstring calls it "what produces the refusal profile"
  produced nothing at all, and nothing noticed because no test covered it against real data.

- **Gold verification reported correct writes as failures** (#75). The mart counted the
  triage stage's rows in the same partitions as its own. Found by the workbook's first
  query, while the portal still said *Running*.
- **The report dropped a column for every system** (#74). The first partition it opened
  predated the column. Silent failure #12.
- **The page's filter tests never filtered** (#76). The test fake answered sidebar
  selections with itself. Silent failure #13.
- **The job-exit tests wrote a file into the repository**, and it was committed twice
  (#77).

## Known-stale claims corrected on 2026-09-23

- This file claimed 12 systems, four reconciled, 867 tests, and "README: absent". It
  listed WMC and White Plains hospital files as not started and A1's agent as not started.
  All of those are corrected above.

## Known-stale claims corrected on 2026-09-09

Recording these because the failure mode they represent — a status line that quietly stops
describing the project — is the reason this file exists.

- CLAUDE.md claimed 40M payer rows across 14 files, "Aetna, Cigna, UHC only", and that
  Empire and Emblem were **not** parsed. Both are parsed; the real figures are above.
- CLAUDE.md listed "there is no committed runner" as an open gate. `mart_cli.py` landed
  in #14.
- `PAYER_SOURCE_VINTAGES` dated 12 of 120 files while the Parquet had carried its own
  `last_updated_on` for months (#16).
