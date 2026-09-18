"""Splitting only the shards that are too big.

NYU Langone is why this exists: the largest payer side of the four, and it
exceeded 8 GiB on shard "0" in a container holding nothing else. 8 GiB is the
Consumption ceiling, so the work had to get smaller.

Splitting every shard into two characters would be 1,296 of them and 1,296
passes over payer silver. The point of measuring is that the common case stays
at 36 and only the shards that are actually large pay for the split.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq

from pipeline.mart import SHARDS, plan_shards

CODE_TYPES = ("CPT", "MS-DRG", "HCPCS")


def payer(tmp_path: Path, counts: dict[str, int]) -> ds.Dataset:
    """A payer silver tree with `counts[prefix]` rows under each leading char."""
    root = tmp_path / "silver" / "payer_rates" / "carrier=Aetna" / "vintage=2026-08"
    root.mkdir(parents=True)
    codes: list[str] = []
    for prefix, n in counts.items():
        codes.extend(f"{prefix}{i:04d}" for i in range(n))
    total = len(codes)
    pq.write_table(
        pa.table(
            {
                "billing_code": pa.array(codes, pa.string()),
                "code_type": pa.array(["CPT"] * total, pa.string()),
                "negotiated_rate": pa.array([1.0] * total, pa.float64()),
                "rate_type": pa.array(["negotiated"] * total, pa.string()),
                "billing_class": pa.array(["institutional"] * total, pa.string()),
                "service_codes": pa.array([""] * total, pa.string()),
                "payer": pa.array(["Aetna_Ppo"] * total, pa.string()),
                "systems": pa.array(["Mount Sinai"] * total, pa.string()),
                "system_count": pa.array([1] * total, pa.int64()),
                "last_updated_on": pa.array(["2026-08-01"] * total, pa.string()),
            }
        ),
        root / "part-0.parquet",
    )
    return ds.dataset(tmp_path / "silver" / "payer_rates", partitioning="hive")


class TestItSplitsOnlyWhatIsLarge:
    def test_a_small_lake_keeps_single_character_shards(self, tmp_path):
        dataset = payer(tmp_path, {"1": 50, "2": 50})

        planned = plan_shards(dataset, "Mount Sinai", CODE_TYPES, threshold=1000)

        assert planned == ("1", "2")

    def test_a_shard_over_the_threshold_becomes_thirty_six(self, tmp_path):
        dataset = payer(tmp_path, {"1": 500, "2": 10})

        planned = plan_shards(dataset, "Mount Sinai", CODE_TYPES, threshold=100)

        assert "1" not in planned, "the oversized prefix must not also run whole"
        assert len([p for p in planned if p.startswith("1")]) == len(SHARDS)
        assert "2" in planned, "a small shard is left alone"

    def test_empty_prefixes_are_not_planned(self, tmp_path):
        """36 passes over a lake with two populated prefixes is 34 wasted."""
        dataset = payer(tmp_path, {"1": 10})

        planned = plan_shards(dataset, "Mount Sinai", CODE_TYPES, threshold=1000)

        assert planned == ("1",)

    def test_the_split_is_reported(self, tmp_path):
        """A run whose shape changed silently is a run nobody can compare."""
        dataset = payer(tmp_path, {"1": 500})
        seen: list[tuple[str, int, int]] = []

        plan_shards(
            dataset,
            "Mount Sinai",
            CODE_TYPES,
            threshold=100,
            on_plan=lambda prefix, rows, children: seen.append((prefix, rows, children)),
        )

        assert seen == [("1", 500, 36)]

    def test_a_system_with_no_rows_plans_nothing(self, tmp_path):
        dataset = payer(tmp_path, {"1": 10})

        assert plan_shards(dataset, "Nobody", CODE_TYPES, threshold=1000) == ()

    def test_children_cover_the_parent(self, tmp_path):
        """Every code under "1" must still be reachable through some child."""
        dataset = payer(tmp_path, {"1": 500})

        planned = plan_shards(dataset, "Mount Sinai", CODE_TYPES, threshold=100)

        assert {p[1] for p in planned} == set(SHARDS)
