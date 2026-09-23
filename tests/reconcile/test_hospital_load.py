"""The hospital-side shard load, and the variants the OOM profile measures.

Pruning by the ``hospital_slug`` partition and turning off readahead are both
candidates for where NYU Langone's missing gigabytes go. Neither may change a
single rate: a memory fix that moved a result would be a results change
disguised as a performance one. So each variant is held to the baseline's
output, row for row.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.dataset as ds
import pytest

from reconcile.silver import hospital_shard

CODE_TYPES = ("CPT", "HCPCS")


def row(hospital: str, code: str, rate: float, plan: str = "Aetna PPO") -> dict[str, object]:
    return {
        "hospital": hospital,
        "location_name": f"{hospital} Main",
        "source_url": f"https://example.org/{hospital}.json",
        "file_vintage": "2026-04-01",
        "code": code,
        "code_type": "CPT",
        "setting": "outpatient",
        "billing_class": "facility",
        "payer_name_raw": "Aetna",
        "plan_name_raw": plan,
        "product_class": "commercial",
        "rate_kind": "dollar",
        "rate_dollar": rate,
        "methodology": "fee schedule",
        "hospital_slug": hospital.casefold().replace(" ", "-"),
    }


@pytest.fixture
def lake(tmp_path: Path) -> ds.Dataset:
    """Two systems in one silver tree, partitioned the way publish writes it."""
    rows = [
        row("NYU Langone Health", "10021", 100.0),
        row("NYU Langone Health", "10021", 120.0),
        row("NYU Langone Health", "15271", 900.0),
        row("NYU Langone Health", "99213", 80.0),
        # Same codes at another system: the rows pruning must never pick up.
        row("Mount Sinai Health System", "10021", 5_000.0),
        row("Mount Sinai Health System", "15271", 7_000.0),
    ]
    ds.write_dataset(
        pa.Table.from_pylist(rows),
        base_dir=str(tmp_path / "silver"),
        format="parquet",
        partitioning=ds.partitioning(pa.schema([("hospital_slug", pa.string())]), flavor="hive"),
    )
    return ds.dataset(str(tmp_path / "silver"), format="parquet", partitioning="hive")


def rates(result: list[object]) -> list[tuple[object, ...]]:
    return sorted((r.hospital, r.code, r.plan, r.rate_dollar) for r in result)  # type: ignore[attr-defined]


class TestTheVariantsChangeNoRate:
    @pytest.mark.parametrize(
        ("slug", "readahead"),
        [("nyu-langone-health", True), (None, False), ("nyu-langone-health", False)],
    )
    def test_each_variant_returns_the_baseline_rates(self, lake, slug, readahead):
        baseline = hospital_shard(lake, "NYU Langone Health", CODE_TYPES, "1")

        variant = hospital_shard(
            lake, "NYU Langone Health", CODE_TYPES, "1", slug=slug, readahead=readahead
        )

        assert rates(variant) == rates(baseline)
        assert rates(baseline), "the shard is not empty, so equality means something"

    def test_pruning_never_admits_another_system(self, lake):
        got = hospital_shard(lake, "NYU Langone Health", CODE_TYPES, "", slug="nyu-langone-health")

        assert {r.hospital for r in got} <= {"NYU Langone Health Main", "NYU Langone Health"}
        assert all(r.rate_dollar < 1_000 for r in got)

    def test_a_mismatched_slug_returns_nothing_rather_than_the_wrong_system(self, lake):
        """The name and the slug both filter, so a wrong slug can only narrow."""
        got = hospital_shard(
            lake, "NYU Langone Health", CODE_TYPES, "", slug="mount-sinai-health-system"
        )

        assert got == []


class TestTheSteps:
    def test_every_step_is_reported_in_order(self, lake):
        seen: list[tuple[str, dict[str, object]]] = []

        hospital_shard(
            lake,
            "NYU Langone Health",
            CODE_TYPES,
            "1",
            on_step=lambda name, **fields: seen.append((name, fields)),
        )

        assert [name for name, _ in seen] == [
            "start",
            "scanned",
            "aggregated",
            "scan_released",
            "converted",
        ]
        scanned = dict(seen)["scanned"]
        assert scanned["rows"] == 3, "shard 1 at NYU: 10021 twice and 15271"
        assert dict(seen)["converted"]["rates"] == 2

    def test_an_empty_shard_stops_after_the_scan(self, lake):
        seen: list[str] = []

        got = hospital_shard(
            lake, "NYU Langone Health", CODE_TYPES, "7", on_step=lambda name, **_: seen.append(name)
        )

        assert got == []
        assert seen == ["start", "scanned"]
