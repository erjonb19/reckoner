"""Conforming payer bronze into silver.

Three claims are worth holding down.

**Compaction is a consequence of the partitioning, not a pass.** EmblemHealth
lands as 98 files averaging 210 KB, one per plan, because that is how the TiC
files arrive. They share a carrier and a vintage, so they are one partition and
``write_dataset`` writes one file. Nothing concatenates anything.

**The vintage key comes from ``last_updated_on``** — the column the payer reader
ignored for months in favour of a hardcoded date map covering 10% of the files
(ADR 0001). Deriving the partition from it is what makes it load-bearing.

**Silver is derived from bronze, not from the parser's output a second time.**
Bronze is the authoritative landing; building silver from the same upstream
twice would let the two drift with nothing to detect it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq
import pytest

import storage
from storage.publish import SILVER_PAYER_MANIFEST, publish_payer, write_manifest

DATE = "2026-09-13"


def bronze(tmp_path: Path, files: list[tuple[str, str, str, int]]) -> Path:
    """A bronze payer tree: (carrier, stem, last_updated_on, rows) per file."""
    root = tmp_path / "bronze" / "payer_tic" / f"ingest_date={DATE}"
    for carrier, stem, updated, rows in files:
        target = root / f"carrier={carrier}"
        target.mkdir(parents=True, exist_ok=True)
        pq.write_table(
            pa.table(
                {
                    "billing_code": pa.array([f"{i:05d}" for i in range(rows)], pa.string()),
                    "negotiated_rate": pa.array([float(i) for i in range(rows)], pa.float64()),
                    "billing_class": pa.array(["Professional"] * rows, pa.string()),
                    "last_updated_on": pa.array([updated] * rows, pa.string()),
                }
            ),
            target / f"{stem}.parquet",
        )
    return tmp_path


class TestCompaction:
    def test_ninety_eight_files_of_one_vintage_become_one(self, tmp_path):
        """The Emblem shape: many small files, one carrier, one vintage."""
        lake = bronze(
            tmp_path, [("Emblem", f"Emblem_plan{i:03d}", "2026-09-04", 5) for i in range(98)]
        )
        location = storage.local(lake)

        results, manifest = publish_payer(location, DATE)

        assert manifest["files"] == 1, "98 files in, one partition, one file out"
        assert manifest["rows"] == 490
        assert results[0].partitions == 1
        assert results[0].verified

    def test_two_vintages_of_one_carrier_stay_apart(self, tmp_path):
        """Compaction must not merge across the key that makes rates comparable."""
        lake = bronze(
            tmp_path,
            [("Aetna", "a", "2026-06-05", 3), ("Aetna", "b", "2026-08-01", 4)],
        )

        _, manifest = publish_payer(storage.local(lake), DATE)

        assert manifest["files"] == 2
        vintages = sorted(u["vintage"] for u in manifest["uploads"])
        assert vintages == ["2026-06", "2026-08"]


class TestConforming:
    def test_billing_class_is_lowercased(self, tmp_path):
        """Bronze keeps what the payer published; silver holds one spelling."""
        lake = bronze(tmp_path, [("UHC", "u", "2026-08-01", 4)])
        location = storage.local(lake)

        publish_payer(location, DATE)

        written = ds.dataset((lake / "silver" / "payer_rates"), partitioning="hive").to_table(
            columns=["billing_class"]
        )
        assert set(written.column(0).to_pylist()) == {"professional"}

    def test_the_vintage_key_is_derived_from_last_updated_on(self, tmp_path):
        lake = bronze(tmp_path, [("Cigna", "c", "2026-08-01", 2)])

        publish_payer(storage.local(lake), DATE)

        assert (lake / "silver" / "payer_rates" / "carrier=Cigna" / "vintage=2026-08").exists()

    def test_last_updated_on_itself_is_not_rewritten(self, tmp_path):
        """The key is derived; the published value must survive unchanged."""
        lake = bronze(tmp_path, [("Cigna", "c", "2026-08-01", 2)])

        publish_payer(storage.local(lake), DATE)

        table = ds.dataset((lake / "silver" / "payer_rates"), partitioning="hive").to_table(
            columns=["last_updated_on"]
        )
        assert set(table.column(0).to_pylist()) == {"2026-08-01"}

    def test_it_is_written_with_zstd(self, tmp_path):
        """Bronze is snappy as landed; silver is the codec the lake reads fastest."""
        lake = bronze(tmp_path, [("UHC", "u", "2026-08-01", 200)])

        publish_payer(storage.local(lake), DATE)

        one = next((lake / "silver" / "payer_rates").rglob("*.parquet"))
        group = pq.ParquetFile(one).metadata.row_group(0)
        assert {group.column(i).compression for i in range(group.num_columns)} == {"ZSTD"}


class TestVerification:
    def test_each_carrier_is_counted_alone_in_a_shared_tree(self, tmp_path):
        lake = bronze(tmp_path, [("Aetna", "a", "2026-06-05", 3), ("UHC", "u", "2026-08-01", 7)])

        results, manifest = publish_payer(storage.local(lake), DATE)

        by_carrier = {r.subject: r.rows_written for r in results}
        assert by_carrier == {"Aetna": 3, "UHC": 7}
        assert manifest["rows"] == manifest["rows_from_source"] == 10
        assert manifest["verified"] is True

    def test_the_manifest_groups_by_carrier(self, tmp_path):
        lake = bronze(tmp_path, [("Aetna", "a", "2026-06-05", 3), ("UHC", "u", "2026-08-01", 7)])
        location = storage.local(lake)

        _, manifest = publish_payer(location, DATE)
        where = write_manifest(location, manifest, path=SILVER_PAYER_MANIFEST)

        assert manifest["group_key"] == "carrier"
        assert manifest["by_carrier"] == {"Aetna": 1, "UHC": 1}
        stored = json.loads(Path(where).read_text(encoding="utf-8"))
        assert stored["layer"] == "silver/payer_rates"

    def test_an_absent_carrier_raises_rather_than_writing_nothing(self, tmp_path):
        lake = bronze(tmp_path, [("Aetna", "a", "2026-06-05", 3)])
        dataset = ds.dataset(
            lake / "bronze" / "payer_tic" / f"ingest_date={DATE}", partitioning="hive"
        )
        from storage.publish import _publish_one_carrier

        with pytest.raises(ValueError, match="nothing to publish"):
            _publish_one_carrier(dataset, "Nobody", storage.local(tmp_path / "out"))


class TestItReadsBackAsOneDataset:
    def test_the_silver_tree_queries_by_carrier_and_vintage(self, tmp_path):
        lake = bronze(
            tmp_path,
            [
                ("Aetna", "a", "2026-06-05", 3),
                ("UHC", "u1", "2026-08-01", 7),
                ("UHC", "u2", "2026-08-05", 5),
            ],
        )

        publish_payer(storage.local(lake), DATE)

        written = ds.dataset((lake / "silver" / "payer_rates"), partitioning="hive")
        assert written.count_rows(filter=ds.field("carrier") == "UHC") == 12
        assert written.count_rows() == 15

    def test_one_months_files_share_a_vintage_partition(self, tmp_path):
        """The key is month-grained, so 2026-08-01 and 2026-08-05 merge.

        That is the compaction working as intended and it loses nothing: the
        exact date stays in ``last_updated_on``, which is still queryable. The
        same rule keys hospital silver, so a vintage means one thing in both.
        """
        lake = bronze(tmp_path, [("UHC", "u1", "2026-08-01", 7), ("UHC", "u2", "2026-08-05", 5)])

        publish_payer(storage.local(lake), DATE)

        written = ds.dataset((lake / "silver" / "payer_rates"), partitioning="hive")
        assert written.count_rows(filter=ds.field("vintage") == "2026-08") == 12
        assert sorted(set(written.to_table(columns=["last_updated_on"]).column(0).to_pylist())) == [
            "2026-08-01",
            "2026-08-05",
        ]
