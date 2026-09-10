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
- **Measured against the real lake: 120 files, 59,501,435 rows, 0 errors, 49 warnings**
  (196 blank billing codes, all Emblem). 20 tests, built by damaging a real file one column
  at a time.

### A2 entity resolution

- Payer-level and plan-level matchers — `src/agents/entity_resolution.py`, `plan_resolution.py`.
- Eval harness reporting precision/recall/F1 with abstention treated as a recall cost, not
  an error — `src/agents/evals.py`. Results appended, never overwritten.
- 190 reviewed labels — `evals/plan_matching.jsonl`.
- Per-call cost and latency logged (`CallCost`), so the agent's price is a number.

### Engineering

- **683 tests**, all passing. Parser tests are
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
- **A3 schema adaptation.** No longer blocked — `src/payer/contract.py` is the thing it
  would diff a new drop against — but not started.
- **A4 ingest monitoring.**
- **Payer file manifest.** `src/payer/manifest.py` — proposed in ADR 0001, not written.
  `file_summary()` already reports in-flight and superseded files, so what is missing is the
  third case: a file parsed that produced no target rows leaves no parquet, so "absent"
  stays ambiguous between "not parsed" and "parsed, nothing matched". 182 of ~280 Emblem
  files are in exactly that state.
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

## Known-stale claims corrected on 2026-09-09

Recording these because the failure mode they represent — a status line that quietly stops
describing the project — is the reason this file exists.

- CLAUDE.md claimed 40M payer rows across 14 files, "Aetna, Cigna, UHC only", and that
  Empire and Emblem were **not** parsed. Both are parsed; the real figures are above.
- CLAUDE.md listed "there is no committed runner" as an open gate. `mart_cli.py` landed
  in #14.
- `PAYER_SOURCE_VINTAGES` dated 12 of 120 files while the Parquet had carried its own
  `last_updated_on` for months (#16).
