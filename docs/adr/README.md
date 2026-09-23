# Architecture decision records

Each record states the decision, the alternatives that were rejected, and why. A
decision that is reversed gets a new record that says so. The old one is never
edited to agree.

| # | decision | status | date |
|---|---|---|---|
| [0001](0001-payer-tic-lakehouse.md) | **Payer TiC lakehouse.** The payer parser stays in the separate `mrf_pipeline` repo, and the boundary between the two repos is its Parquet, declared as a contract in `src/payer/contract.py`. Medallion layers are mapped onto what already existed. | Accepted | 2026-09-09 |
| [0002](0002-storage-seam.md) | **The storage seam, not a backend class.** Readers take an optional `pyarrow.fs` filesystem, and `storage.resolve()` picks local or ADLS. One code path, tested once. This is why dropping Fabric was a configuration change. | Accepted | 2026-09-09 |
| [0003](0003-azure-native-lakehouse.md) | **Azure-native, not Fabric.** ADLS Gen2 is the authoritative store, with Python engines reading it. Fabric was dropped: the trial wouldn't activate, there was zero regional capacity quota, and an F2 costs $262 a month to serve 4.3 GB. | Accepted | 2026-09-13 |
| [0004](0004-orchestration.md) | **Container Apps Jobs, Consumption profile.** Functions was ruled out on memory and timeout. One stage per execution. `reckoner-mart` is sized at 4 vCPU / 8 GiB from measured peaks, with the table of measurements in the record. | Accepted | 2026-09-14 |

## Open decisions without a record yet

Written down here so they aren't mistaken for settled:

- **What the join keys on.** Today it keys on carrier, not plan, so one hospital rate
  meets every plan-level payer rate of that carrier (#70). Option (a) collapses to one
  representative payer rate; option (b) puts the matched plan in the key.
  `docs/plan-matching.md` measured the plan strings' ceiling at 14.7% of rows, which
  favours (a).
- **Whether billing class belongs in the join key.** 70.9% of all candidates are
  professional-against-institutional refusals the join builds and then rejects. Keying on
  it changes the pooled comparable share from 5.28% to 18.15% with no pair gained.
  `docs/refusal-decomposition.md` has the numbers.
- **How the last two systems fit in 8 GiB** (#47). The allocation is in `hospital_shard`,
  outside Arrow's pool. The fix is a code change, not a new decision, unless it ends in a
  different runtime.
