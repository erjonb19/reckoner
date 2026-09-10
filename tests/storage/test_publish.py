"""Publishing a curated slice through the seam.

The property worth most here is that publishing *repairs* the partition rather
than copying it. The local lake still carries paths written before #13, where a
US-format date was sliced mid-field and its slashes became directory
separators -- Rochester Regional has both ``vintage=2026-04`` and
``vintage=4/1/202`` for the same data. Copying bytes would carry that into the
authoritative store; recomputing the key from ``file_vintage`` fixes it on the
way out, and the real run collapsed those two into one clean partition with all
841,244 rows intact.

Everything here runs against a local ``Location``. That is the point of ADR
0002: the cloud path is the same code with a different filesystem, so the whole
thing can be proven before a storage account exists.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq
import pytest

import storage
from storage.publish import publish_hospital


def curated(root: Path, rows: list[tuple[str, str, float]]) -> Path:
    """A minimal curated lake: hospital, the raw vintage string, a rate."""
    target = root / "curated" / "hospital_rates"
    target.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "hospital": pa.array([r[0] for r in rows], pa.string()),
            "file_vintage": pa.array([r[1] for r in rows], pa.string()),
            "rate_dollar": pa.array([r[2] for r in rows], pa.float64()),
        }
    )
    pq.write_table(table, target / "part.parquet")
    return root


class TestPublishRepairsThePartition:
    def test_a_us_format_vintage_lands_as_a_clean_key(self, tmp_path):
        """The #13 defect: 4/1/2026 sliced to 4/1/202 and read as directories."""
        source = curated(tmp_path / "lake", [("Crouse Health", "4/1/2026", 10.0)])
        out = storage.local(tmp_path / "out")

        result = publish_hospital(source, "Crouse Health", out)

        assert result.verified
        keys = [p.name for p in (tmp_path / "out" / "hospital_slug=crouse-health").iterdir()]
        assert keys == ["vintage=2026-04"]

    def test_two_local_partitions_of_one_month_merge(self, tmp_path):
        """Rochester Regional really has both forms for the same month."""
        source = curated(
            tmp_path / "lake",
            [("RRH", "4/1/2026", 10.0), ("RRH", "2026-04-01", 20.0)],
        )
        out = storage.local(tmp_path / "out")

        result = publish_hospital(source, "RRH", out)

        assert result.rows_written == 2
        assert result.partitions == 1

    def test_a_missing_vintage_is_keyed_rather_than_dropped(self, tmp_path):
        source = curated(tmp_path / "lake", [("Crouse Health", "", 10.0)])

        result = publish_hospital(source, "Crouse Health", storage.local(tmp_path / "out"))

        assert result.verified
        assert (tmp_path / "out" / "hospital_slug=crouse-health" / "vintage=unknown").exists()


class TestVerification:
    def test_the_row_count_is_read_back_not_assumed(self, tmp_path):
        source = curated(tmp_path / "lake", [("A", "2026-01-01", float(i)) for i in range(500)])

        result = publish_hospital(source, "A", storage.local(tmp_path / "out"))

        assert result.rows_read == result.rows_written == 500
        assert result.verified

    def test_each_system_is_counted_alone_in_a_shared_tree(self, tmp_path):
        """An unfiltered count grows with every system and always looks verified."""
        source = curated(
            tmp_path / "lake",
            [("A", "2026-01-01", 1.0)] * 3 + [("B", "2026-01-01", 2.0)] * 7,
        )
        out = storage.local(tmp_path / "out")

        first = publish_hospital(source, "A", out)
        second = publish_hospital(source, "B", out)

        assert first.rows_written == 3
        assert second.rows_written == 7, "B must not be credited with A's rows"

    def test_publishing_an_unknown_system_raises_rather_than_writing_nothing(self, tmp_path):
        source = curated(tmp_path / "lake", [("A", "2026-01-01", 1.0)])

        with pytest.raises(ValueError, match="nothing to publish"):
            publish_hospital(source, "Nobody", storage.local(tmp_path / "out"))


class TestThePublishedCopyIsUsable:
    def test_it_reads_back_as_a_partitioned_dataset(self, tmp_path):
        source = curated(
            tmp_path / "lake",
            [("A", "1/15/2026", 1.0), ("A", "3/2/2026", 2.0)],
        )
        out = storage.local(tmp_path / "out")

        publish_hospital(source, "A", out)

        written = ds.dataset(out.root, filesystem=out.filesystem, partitioning="hive")
        assert set(written.schema.names) >= {"hospital_slug", "vintage", "rate_dollar"}
        assert written.count_rows(filter=ds.field("vintage") == "2026-01") == 1
        assert written.count_rows(filter=ds.field("vintage") == "2026-03") == 1

    def test_the_raw_vintage_survives_unchanged(self, tmp_path):
        """The key is derived; the published value must not be rewritten."""
        source = curated(tmp_path / "lake", [("A", "1/15/2026", 1.0)])
        out = storage.local(tmp_path / "out")

        publish_hospital(source, "A", out)

        written = ds.dataset(out.root, filesystem=out.filesystem, partitioning="hive")
        assert written.to_table(columns=["file_vintage"]).column(0).to_pylist() == ["1/15/2026"]
