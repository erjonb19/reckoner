"""Gold's manifest when two stages write into the same partitions.

Written after three cloud reruns wrote correct gold and exited 1. The mart
writes five tables per system and the triage stage writes two more into the
same ``hospital_slug=`` partitions. The mart's verification counted triage's
rows as its own, found more rows than it wrote, and reported failure -- for
exactly the systems triage had run on, and for none of the others.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

import storage
from pipeline.mart import gold_manifest


def table(root: Path, name: str, slug: str, rows: int) -> None:
    target = root / "gold" / name / f"hospital_slug={slug}"
    target.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({"n": pa.array(range(rows), pa.int64())}), target / "part-0.parquet")


def lake(tmp_path: Path) -> Path:
    for slug in ("mount-sinai-health-system", "white-plains-hospital"):
        table(tmp_path, "coverage", slug, 1)
        table(tmp_path, "outcomes", slug, 30)
    # Triage has run for Mount Sinai only, as in the lake that broke.
    table(tmp_path, "triage_queue", "mount-sinai-health-system", 50)
    table(tmp_path, "triage_summary", "mount-sinai-health-system", 3)
    return tmp_path


MART_WROTE = {"coverage": 1, "outcomes": 30}


class TestTheMartVerifiesItsOwnTables:
    def test_triage_rows_in_the_same_partition_do_not_fail_a_correct_mart_run(self, tmp_path):
        manifest = gold_manifest(
            storage.local(lake(tmp_path)), MART_WROTE, {"mount-sinai-health-system"}
        )

        assert manifest["verified"] is True
        assert manifest["rows_checked"] == 31

    def test_the_manifest_still_describes_every_table(self, tmp_path):
        """Narrowing the check must not narrow what stage 1 compares against."""
        manifest = gold_manifest(
            storage.local(lake(tmp_path)), MART_WROTE, {"mount-sinai-health-system"}
        )

        assert manifest["rows"] == 1 + 30 + 1 + 30 + 50 + 3
        destinations = {
            u["destination"].split("/gold/")[1].split("/")[0] for u in manifest["uploads"]
        }
        assert {"triage_queue", "triage_summary"} <= destinations

    def test_a_system_triage_never_touched_passed_before_and_still_does(self, tmp_path):
        manifest = gold_manifest(
            storage.local(lake(tmp_path)), MART_WROTE, {"white-plains-hospital"}
        )

        assert manifest["verified"] is True

    def test_a_real_shortfall_in_its_own_tables_still_fails(self, tmp_path):
        """Scoping the check must not blunt it."""
        manifest = gold_manifest(
            storage.local(lake(tmp_path)),
            {"coverage": 1, "outcomes": 31},
            {"mount-sinai-health-system"},
        )

        assert manifest["verified"] is False

    def test_a_stale_partition_of_an_owned_table_fails(self, tmp_path):
        """An empty write leaves the old partition, so an owned zero is checked."""
        manifest = gold_manifest(
            storage.local(lake(tmp_path)),
            {"coverage": 1, "outcomes": 0},
            {"mount-sinai-health-system"},
        )

        assert manifest["verified"] is False


class TestTriageVerifiesItsOwnTables:
    def test_the_triage_stage_checks_only_what_it_wrote(self, tmp_path):
        manifest = gold_manifest(
            storage.local(lake(tmp_path)),
            {"triage_queue": 50, "triage_summary": 3},
            {"mount-sinai-health-system"},
        )

        assert manifest["verified"] is True
        assert manifest["rows_checked"] == 53
        assert manifest["tables_written"] == ["triage_queue", "triage_summary"]


class TestTheRealWriteFeedsTheCheck:
    """Through ``mart.write``, not a hand-written dict.

    #75's tests passed the mart's tables in by hand. The real ``write`` also
    reported a zero for every table it was not given, so in the cloud the mart
    claimed triage's tables and failed verification on the three systems that
    had triage partitions -- while the hand-written test stayed green.
    """

    def test_write_reports_only_the_tables_it_was_given(self, tmp_path):
        from pipeline import mart

        written = mart.write(
            storage.local(tmp_path),
            {"coverage": [{"hospital_slug": "white-plains-hospital", "n": 1}]},
        )

        assert set(written) == {"coverage"}

    def test_a_mart_write_verifies_beside_triage_partitions(self, tmp_path):
        from pipeline import mart

        root = lake(tmp_path)
        slug = "mount-sinai-health-system"
        written = mart.write(
            storage.local(root),
            {
                "coverage": [{"hospital_slug": slug, "n": 1}],
                "outcomes": [{"hospital_slug": slug, "n": i} for i in range(30)],
            },
        )

        manifest = gold_manifest(storage.local(root), written, {slug})

        assert manifest["verified"] is True
