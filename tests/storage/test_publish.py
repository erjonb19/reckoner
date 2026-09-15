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

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq
import pytest

import storage
from storage import publish
from storage.publish import publish_hospital


def curated(
    root: Path, rows: list[tuple[str, str, float]], *, code_types: list[str] | None = None
) -> Path:
    """A minimal curated lake: hospital, the raw vintage string, a rate.

    ``code_type`` is present because the real lake always has it and it is a
    partition key; omitting it here once hid the fact that a missing partition
    column produces a shallower tree rather than an error.
    """
    target = root / "curated" / "hospital_rates"
    target.mkdir(parents=True, exist_ok=True)
    columns = {
        "hospital": pa.array([r[0] for r in rows], pa.string()),
        "file_vintage": pa.array([r[1] for r in rows], pa.string()),
        "rate_dollar": pa.array([r[2] for r in rows], pa.float64()),
    }
    if code_types is not None:
        columns["code_type"] = pa.array(code_types, pa.string())
    else:
        columns["code_type"] = pa.array(["CPT"] * len(rows), pa.string())
    pq.write_table(pa.table(columns), target / "part.parquet")
    return root


class TestPublishRepairsThePartition:
    def test_a_us_format_vintage_lands_as_a_clean_key(self, tmp_path):
        """The #13 defect: 4/1/2026 sliced to 4/1/202 and read as directories."""
        source = curated(tmp_path / "lake", [("Crouse Health", "4/1/2026", 10.0)])
        out = storage.local(tmp_path / "out")

        result = publish_hospital(source, "Crouse Health", out)

        assert result.verified
        keys = [
            p.name
            for p in (tmp_path / "out" / "hospital_slug=crouse-health" / "code_type=CPT").iterdir()
        ]
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
        assert (
            tmp_path / "out" / "hospital_slug=crouse-health" / "code_type=CPT" / "vintage=unknown"
        ).exists()


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


class TestTheCommandLine:
    """--all and --hospital are different code paths, and the dispatch is one of them.

    Written after --all fell through to the single-system branch and called
    publish_hospital(None): argparse was satisfied, the run failed on the far
    side with an error about a hospital nobody had named, and nothing had
    exercised the branch.
    """

    def test_all_publishes_every_system_and_writes_a_manifest(self, tmp_path, capsys):
        curated(
            tmp_path / "lake",
            [("A", "2026-01-01", 1.0), ("B", "2026-01-01", 2.0), ("B", "2026-01-01", 3.0)],
        )
        out = tmp_path / "out"

        code = publish.main(["--root", str(tmp_path / "lake"), "--all", "--to", str(out)])

        assert code == 0
        manifest = json.loads(
            (out / "_meta" / "silver" / "hospital_rates" / "upload_manifest.json").read_text()
        )
        assert manifest["rows"] == 3
        assert manifest["verified"] is True
        assert sorted(manifest["by_hospital_slug"]) == ["a", "b"]

    def test_all_reports_the_footprint_before_writing(self, tmp_path, capsys):
        """Before is a decision; after is a discovery."""
        curated(tmp_path / "lake", [("A", "2026-01-01", 1.0)])

        publish.main(["--root", str(tmp_path / "lake"), "--all", "--to", str(tmp_path / "out")])

        printed = capsys.readouterr().out
        assert "free-tier check (before the write)" in printed
        assert printed.index("free-tier check") < printed.index("verified")

    def test_naming_one_system_still_lands_under_the_silver_root(self, tmp_path):
        curated(tmp_path / "lake", [("A", "2026-01-01", 1.0)])
        out = tmp_path / "out"

        assert (
            publish.main(["--root", str(tmp_path / "lake"), "--hospital", "A", "--to", str(out)])
            == 0
        )
        assert (out / "silver" / "hospital_rates" / "hospital_slug=a").exists()

    def test_neither_flag_is_refused(self, tmp_path):
        with pytest.raises(SystemExit):
            publish.main(["--root", str(tmp_path)])


class TestThePartitionLayout:
    def test_all_three_keys_land_in_the_path(self, tmp_path):
        """hospital_slug / code_type / vintage, in that order."""
        source = curated(
            tmp_path / "lake",
            [("A", "2026-01-01", 1.0), ("A", "2026-01-01", 2.0)],
            code_types=["CPT", "MS-DRG"],
        )

        publish_hospital(source, "A", storage.local(tmp_path / "out"))

        landed = sorted(
            str(p.relative_to(tmp_path / "out")).replace("\\", "/")
            for p in (tmp_path / "out").rglob("*.parquet")
        )
        assert landed == [
            "hospital_slug=a/code_type=CPT/vintage=2026-01/part-0.parquet",
            "hospital_slug=a/code_type=MS-DRG/vintage=2026-01/part-0.parquet",
        ]

    def test_it_is_written_with_the_codec_the_source_uses(self, tmp_path):
        """Snappy is write_dataset's default and it cost 2.7 GB the first time.

        The curated lake is zstd; publishing with the default re-encoded the
        same 156M rows at 6.2 GB instead of 3.5 GB. Nothing about the layout
        changed -- only the codec -- so this is pinned rather than commented.
        """
        source = curated(tmp_path / "lake", [("A", "2026-01-01", float(i)) for i in range(2000)])

        publish_hospital(source, "A", storage.local(tmp_path / "out"))

        one = next((tmp_path / "out").rglob("*.parquet"))
        group = pq.ParquetFile(one).metadata.row_group(0)
        codecs = {group.column(i).compression for i in range(group.num_columns)}
        assert codecs == {"ZSTD"}

    def test_a_missing_partition_column_raises_rather_than_flattening(self, tmp_path):
        """pyarrow's own behaviour here is to write a shallower tree and succeed.

        A silver copy keyed on two of three columns is not a smaller mistake
        than a failed write -- it is the same wrong layout with nothing to say
        so. Found when a fixture without code_type published happily.
        """
        target = tmp_path / "lake" / "curated" / "hospital_rates"
        target.mkdir(parents=True)
        pq.write_table(
            pa.table(
                {
                    "hospital": pa.array(["A"], pa.string()),
                    "file_vintage": pa.array(["2026-01-01"], pa.string()),
                }
            ),
            target / "part.parquet",
        )

        with pytest.raises(ValueError, match="shallower tree"):
            publish_hospital(tmp_path / "lake", "A", storage.local(tmp_path / "out"))

    def test_the_partition_column_is_not_duplicated_in_the_payload(self, tmp_path):
        """write_dataset lifts it into the path; hive partitioning puts it back."""
        source = curated(tmp_path / "lake", [("A", "2026-01-01", 1.0)], code_types=["CPT"])
        out = storage.local(tmp_path / "out")

        publish_hospital(source, "A", out)

        one = next((tmp_path / "out").rglob("*.parquet"))
        assert "code_type" not in pq.read_schema(one).names, "stored in the path, not the file"
        read_back = ds.dataset(out.root, filesystem=out.filesystem, partitioning="hive")
        assert read_back.to_table(columns=["code_type"]).column(0).to_pylist() == ["CPT"]


class TestSilverConformsCase:
    def test_billing_class_is_lowercased(self, tmp_path):
        """The lake holds both 'Facility' and 'facility'; silver holds one."""
        target = tmp_path / "lake" / "curated" / "hospital_rates"
        target.mkdir(parents=True)
        pq.write_table(
            pa.table(
                {
                    "hospital": pa.array(["A", "A"], pa.string()),
                    "file_vintage": pa.array(["2026-01-01", "2026-01-01"], pa.string()),
                    "billing_class": pa.array(["Facility", "facility"], pa.string()),
                    "code_type": pa.array(["CPT", "CPT"], pa.string()),
                }
            ),
            target / "part.parquet",
        )
        out = storage.local(tmp_path / "out")

        publish_hospital(tmp_path / "lake", "A", out)

        written = ds.dataset(out.root, filesystem=out.filesystem, partitioning="hive")
        assert set(written.to_table(columns=["billing_class"]).column(0).to_pylist()) == {
            "facility"
        }

    def test_an_empty_billing_class_becomes_null_not_blank(self, tmp_path):
        """Blank and null are the same absence; two spellings of it is one too many."""
        target = tmp_path / "lake" / "curated" / "hospital_rates"
        target.mkdir(parents=True)
        pq.write_table(
            pa.table(
                {
                    "hospital": pa.array(["A"], pa.string()),
                    "file_vintage": pa.array(["2026-01-01"], pa.string()),
                    "billing_class": pa.array(["   "], pa.string()),
                    "code_type": pa.array(["CPT"], pa.string()),
                }
            ),
            target / "part.parquet",
        )
        out = storage.local(tmp_path / "out")

        publish_hospital(tmp_path / "lake", "A", out)

        written = ds.dataset(out.root, filesystem=out.filesystem, partitioning="hive")
        assert written.to_table(columns=["billing_class"]).column(0).to_pylist() == [None]
