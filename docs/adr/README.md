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
| [0005](0005-billing-class-in-the-join-key.md) | **Billing class in the join key.** A hospital rate meets only payer rates of its own class. Rates whose only counterparts are the other class are refused once and never compared. Raw and like-class shares are reported side by side for this release. | Accepted | 2026-09-23 |
| [0006](0006-compare-against-the-carrier-distribution.md) | **One outcome per hospital rate**, against the carrier's distribution (min, median, max, count, inside), with a stated refusal order and a `within_payer_range` explanation. Resolves #70 with option (a). | Accepted | 2026-09-23 |

## Open decisions without a record yet

- **Whether to drop the raw share** once a release has shown both (ADR 0005).
