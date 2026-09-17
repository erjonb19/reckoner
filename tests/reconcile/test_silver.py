"""Reading both sides of the comparison out of silver.

The claim worth testing is that aggregating the payer side one carrier at a time
gives the same rows as aggregating it whole. That is not an optimisation detail:
``aggregate_rates`` takes a DISTINCT over every column before the median per key,
and in a 4 GiB container the undivided version peaked at 4,063 MiB on one shard
and was killed on the next.

It is exact because every group-by key includes ``payer`` -- the config label
that was the filename stem -- and a stem belongs to exactly one carrier. No group
and no duplicate row can straddle two carriers, so splitting on carrier cannot
move a row between groups.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq

from payer.curated import PayerFilter
from payer.curated import aggregate_rates as aggregate_payer
from payer.curated import to_comparable_rates as payer_to_rates
from reconcile.silver import payer_files_from_silver, payer_shard

CODE_TYPES = ("CPT", "MS-DRG", "HCPCS")


def silver(tmp_path: Path, rows: list[tuple[str, str, str, str, float]]) -> ds.Dataset:
    """A payer silver tree: (carrier, stem, code, billing_class, rate) per row."""
    root = tmp_path / "silver" / "payer_rates"
    by_carrier: dict[str, list[tuple[str, str, str, str, float]]] = {}
    for row in rows:
        by_carrier.setdefault(row[0], []).append(row)
    for carrier, members in by_carrier.items():
        target = root / f"carrier={carrier}" / "vintage=2026-08"
        target.mkdir(parents=True, exist_ok=True)
        n = len(members)
        pq.write_table(
            pa.table(
                {
                    "billing_code": pa.array([m[2] for m in members], pa.string()),
                    "code_type": pa.array(["CPT"] * n, pa.string()),
                    "description": pa.array(["x"] * n, pa.string()),
                    "negotiated_rate": pa.array([m[4] for m in members], pa.float64()),
                    "rate_type": pa.array(["negotiated"] * n, pa.string()),
                    "billing_class": pa.array([m[3] for m in members], pa.string()),
                    "service_codes": pa.array([""] * n, pa.string()),
                    "expiration_date": pa.array([""] * n, pa.string()),
                    "matched_npis": pa.array([""] * n, pa.string()),
                    "matched_tins": pa.array([""] * n, pa.string()),
                    "group_tins": pa.array([0] * n, pa.int64()),
                    "network_names": pa.array([""] * n, pa.string()),
                    "payer": pa.array([m[1] for m in members], pa.string()),
                    "reporting_entity_name": pa.array(["r"] * n, pa.string()),
                    "last_updated_on": pa.array(["2026-08-01"] * n, pa.string()),
                    "schema_version": pa.array(["1.0"] * n, pa.string()),
                    "systems": pa.array(["Mount Sinai"] * n, pa.string()),
                    "system_count": pa.array([1] * n, pa.int64()),
                }
            ),
            target / "part-0.parquet",
        )
    return ds.dataset(root, partitioning="hive")


def a_lake(tmp_path: Path) -> ds.Dataset:
    rows = []
    for i in range(40):
        rows.append(("Aetna", "Aetna_Ppo", f"1{i:04d}", "institutional", 100.0 + i))
        rows.append(("UHC", "UHC_NY_Choice", f"1{i:04d}", "institutional", 200.0 + i))
        # An exact duplicate, which the DISTINCT is there to remove.
        rows.append(("Aetna", "Aetna_Ppo", f"1{i:04d}", "institutional", 100.0 + i))
    return silver(tmp_path, rows)


def key(rate: object) -> tuple[str, ...]:
    return tuple(
        str(getattr(rate, name))
        for name in (
            "hospital",
            "code",
            "code_type",
            "payer",
            "plan",
            "billing_class",
            "rate_dollar",
        )
    )


class TestAggregatingPerCarrierIsExact:
    def test_it_gives_the_same_rows_as_aggregating_whole(self, tmp_path):
        dataset = a_lake(tmp_path)
        files = payer_files_from_silver(dataset)

        whole = payer_to_rates(
            aggregate_payer(
                dataset,
                PayerFilter(systems=("Mount Sinai",), code_types=CODE_TYPES, code_prefix="1"),
            ),
            files,
        )
        split = payer_shard(dataset, files, "Mount Sinai", CODE_TYPES, "1")

        assert sorted(map(key, split)) == sorted(map(key, whole))
        assert len(split) == 80, "two carriers, forty codes, duplicates removed"

    def test_the_duplicate_removal_still_happens(self, tmp_path):
        """Each Aetna row is written twice; one must survive, not two."""
        dataset = a_lake(tmp_path)
        files = payer_files_from_silver(dataset)

        split = payer_shard(dataset, files, "Mount Sinai", CODE_TYPES, "1")

        aetna = [r for r in split if r.payer.startswith("Aetna")]
        assert len(aetna) == 40

    def test_one_carrier_can_be_asked_for_alone(self, tmp_path):
        dataset = a_lake(tmp_path)
        files = payer_files_from_silver(dataset)

        only_uhc = payer_shard(dataset, files, "Mount Sinai", CODE_TYPES, "1", carriers=("UHC",))

        assert len(only_uhc) == 40
        # The converter canonicalises the config label, so UHC arrives as the
        # payer's actual name. Asserted as one distinct value rather than a
        # literal, because the canonical form is the converter's business.
        assert len({r.payer for r in only_uhc}) == 1
        assert not any(r.payer.startswith("Aetna") for r in only_uhc)


class TestRebuildingFileMetadata:
    def test_stems_decompose_into_carrier_and_network(self, tmp_path):
        files = payer_files_from_silver(a_lake(tmp_path))

        by_stem = {f.stem: f for f in files}
        assert by_stem["Aetna_Ppo"].carrier == "Aetna"
        assert by_stem["Aetna_Ppo"].network == "Ppo"
        assert by_stem["UHC_NY_Choice"].carrier == "UHC"

    def test_the_vintage_comes_from_last_updated_on(self, tmp_path):
        """The column the reader ignored for months (ADR 0001)."""
        files = payer_files_from_silver(a_lake(tmp_path))

        assert {f.vintage for f in files} == {"2026-08-01"}
