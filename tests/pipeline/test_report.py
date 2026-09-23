"""Publishing gold as the summary dataset and the written report.

The stage is a format change, not a second calculation, so the tests are about
what could quietly go wrong in a format change: a number that stops matching
gold, a stale snapshot that renders as current, and a diff that churns on row
order rather than on content.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

import storage
from pipeline.report import (
    PROVENANCE_CAVEAT,
    Summary,
    as_csv,
    build,
    markdown,
    write_local,
)


def gold(tmp_path: Path) -> Path:
    """A minimal gold tree: the two tables the report leans on hardest."""
    for name, rows in {
        "coverage": [
            {
                "hospital_slug": "mount-sinai-health-system",
                "system": "Mount Sinai",
                "hospital_rates": 1348398,
                "payer_rates": 18799888,
                "candidates": 36250415,
                "pairs_formed": 3370446,
                "comparable_share": 0.093,
                "material": 3011270,
                "unexplained_and_material": 60182,
                "facilities": 7,
                "carriers": 2,
                "systematic_offsets": 461,
                "assumed_facility_when_unstated": True,
            }
        ],
        "refusals": [
            {
                "hospital_slug": "mount-sinai-health-system",
                "system": "Mount Sinai",
                "reason": "different_billing_class",
                "candidates": 31123000,
            }
        ],
    }.items():
        target = tmp_path / "gold" / name / "hospital_slug=mount-sinai-health-system"
        target.mkdir(parents=True)
        pq.write_table(pa.Table.from_pylist(rows), target / "part-0.parquet")
    return tmp_path


class TestItRepublishesRatherThanRecomputes:
    def test_the_figures_come_straight_from_gold(self, tmp_path):
        """A second calculation would give a second answer with no tiebreak."""
        summary = build(storage.local(gold(tmp_path)))

        row = summary.tables["coverage"][0]
        assert row["pairs_formed"] == 3370446
        assert row["unexplained_and_material"] == 60182
        assert row["systematic_offsets"] == 461

    def test_a_missing_table_is_empty_not_fatal(self, tmp_path):
        """Gold is written per system, so a partial tree is a real state."""
        summary = build(storage.local(gold(tmp_path)))

        assert summary.tables["magnitude"] == []
        assert summary.tables["exemplars"] == []


class TestProvenance:
    def test_the_phi_caveat_is_always_present(self, tmp_path):
        summary = build(storage.local(gold(tmp_path)))

        assert PROVENANCE_CAVEAT in summary.metadata["caveats"]
        assert "No PHI" in summary.metadata["caveats"][0]

    def test_extra_caveats_are_carried_and_not_replaced(self, tmp_path):
        summary = build(storage.local(gold(tmp_path)), caveats=("NYU Langone is local.",))

        assert summary.metadata["caveats"] == [PROVENANCE_CAVEAT, "NYU Langone is local."]

    def test_it_records_which_systems_arrived_against_which_were_expected(self, tmp_path):
        """One system present out of many is exactly the state worth seeing.

        Asserted against RECONCILABLE rather than a literal count: the set grows
        when hospital files arrive, and a test that had to be edited each time
        would be testing the edit rather than the behaviour.
        """
        from pipeline.mart import RECONCILABLE

        summary = build(storage.local(gold(tmp_path)))

        assert summary.metadata["systems"] == ["Mount Sinai"]
        assert len(summary.metadata["systems_expected"]) == len(RECONCILABLE)
        assert len(summary.metadata["systems"]) < len(summary.metadata["systems_expected"])

    def test_the_assumption_is_recorded_per_system(self, tmp_path):
        summary = build(storage.local(gold(tmp_path)))

        assert summary.metadata["assume_facility_when_unstated"] == ["mount-sinai-health-system"]

    def test_run_json_is_written_beside_the_tables(self, tmp_path):
        summary = build(storage.local(gold(tmp_path)))

        written = write_local(summary, tmp_path / "summary")

        names = {path.name for path in written}
        assert "run.json" in names
        assert "coverage.csv" in names
        stored = json.loads((tmp_path / "summary" / "run.json").read_text(encoding="utf-8"))
        assert stored["built_at"]
        assert stored["rows_per_table"]["coverage"] == 1


class TestTheCsv:
    def test_rows_are_sorted_so_the_diff_is_about_content(self, tmp_path):
        """Committed monthly. A diff that churns on row order is unreviewable."""
        records = [
            {"a": "3", "b": "z"},
            {"a": "1", "b": "y"},
            {"a": "2", "b": "x"},
        ]

        rows = list(csv.DictReader(io.StringIO(as_csv(records))))

        assert [r["a"] for r in rows] == ["1", "2", "3"]

    def test_columns_follow_gold_rather_than_a_hardcoded_list(self):
        """A column added upstream should reach the page without an edit here."""
        text = as_csv([{"one": 1, "two": 2, "surprise": 3}])

        assert text.splitlines()[0] == "one,two,surprise"

    def test_an_empty_table_is_empty_not_a_header_only_file(self):
        assert as_csv([]) == ""


class TestTheMarkdown:
    def test_it_leads_with_provenance_and_caveats(self, tmp_path):
        summary = build(storage.local(gold(tmp_path)), caveats=("NYU Langone is local.",))

        text = markdown(summary)

        assert text.index("## Caveats") < text.index("## Coverage")
        assert "No PHI" in text
        assert "NYU Langone is local." in text

    def test_it_tells_the_reader_to_read_the_share_not_the_count(self, tmp_path):
        """The pair counts are the most impressive and least informative number."""
        text = markdown(build(storage.local(gold(tmp_path))))

        assert "comparable share, not the pair count" in text
        assert "9.30%" in text

    def test_refusals_declare_their_grain(self, tmp_path):
        """The page cannot filter them by carrier, so the report must say so."""
        text = markdown(build(storage.local(gold(tmp_path))))

        assert "System grain" in text
        assert "different_billing_class" in text

    def test_it_lists_the_tables_it_published(self, tmp_path):
        summary = Summary(
            tables={
                "coverage": [
                    {
                        "system": "Mount Sinai",
                        "hospital_rates": 1,
                        "payer_rates": 1,
                        "pairs_formed": 1,
                        "comparable_share": 0.5,
                        "material": 1,
                        "unexplained_and_material": 0,
                        "systematic_offsets": 0,
                    }
                ]
            },
            metadata={
                "built_at": "now",
                "bronze_ingest_date": "",
                "silver_hospital_published_at": "",
                "silver_payer_published_at": "",
                "gold_published_at": "",
                "caveats": [PROVENANCE_CAVEAT],
            },
        )

        text = markdown(summary)

        assert "`summary/coverage.csv` — 1 rows" in text
        assert "`summary/run.json`" in text
