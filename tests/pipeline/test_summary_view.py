"""What the published page does with the summary dataset.

The page is thin; this is where its decisions live, so this is where they can be
held down. The test that earns the module is
``test_a_filter_the_table_cannot_answer_is_reported``: a control that appears to
apply and does not is the failure mode this repository has catalogued eleven
times, and on a public page it would be the most visible instance yet.
"""

from __future__ import annotations

import json
from pathlib import Path

from pipeline.summary_view import (
    ALL,
    REFUSALS_GRAIN_NOTE,
    apply_filters,
    caveats,
    funnel,
    inapplicable_filters,
    load,
    options,
    outcomes_chart,
    staleness,
    widest,
)

COVERAGE = "hospital_slug,system,pairs_formed,comparable_share,unexplained_and_material\n"
OUTCOMES = "hospital_slug,system,carrier,code_type,explanation,pairs,material_pairs\n"
REFUSALS = "hospital_slug,system,reason,candidates\n"


def dataset(tmp_path: Path, *, with_run: bool = True) -> Path:
    directory = tmp_path / "summary"
    directory.mkdir()
    (directory / "coverage.csv").write_text(
        COVERAGE
        + "mount-sinai,Mount Sinai,3370446,0.093,60182\n"
        + "northwell,Northwell,4552693,0.0623,116504\n",
        encoding="utf-8",
    )
    (directory / "outcomes.csv").write_text(
        OUTCOMES
        + "mount-sinai,Mount Sinai,Aetna,CPT,unexplained,100,90\n"
        + "mount-sinai,Mount Sinai,Aetna,MS-DRG,systematic_offset,400,380\n"
        + "northwell,Northwell,UnitedHealthcare,CPT,unexplained,250,200\n",
        encoding="utf-8",
    )
    (directory / "refusals.csv").write_text(
        REFUSALS
        + "mount-sinai,Mount Sinai,different_billing_class,31123000\n"
        + "northwell,Northwell,billing_class_unstated,35334560\n",
        encoding="utf-8",
    )
    (directory / "magnitude.csv").write_text(
        "hospital_slug,system,carrier,code_type,residual_pairs,median_relative_difference\n"
        "mount-sinai,Mount Sinai,Aetna,CPT,60182,0.34\n",
        encoding="utf-8",
    )
    (directory / "exemplars.csv").write_text(
        "system,carrier,code,relative_difference,is_implausible\n"
        "Mount Sinai,Aetna,99213,3.5,False\n"
        "Mount Sinai,Aetna,70450,12.0,True\n",
        encoding="utf-8",
    )
    if with_run:
        (directory / "run.json").write_text(
            json.dumps(
                {
                    "built_at": "2026-09-18T02:29:58+00:00",
                    "bronze_ingest_date": "2026-09-13",
                    "silver_hospital_published_at": "2026-09-15T03:50:29+00:00",
                    "caveats": ["No PHI.", "NYU Langone is from a local run."],
                }
            ),
            encoding="utf-8",
        )
    return directory


class TestLoading:
    def test_numbers_arrive_as_numbers(self, tmp_path):
        """CSV has no types. A count left as text sorts 10 before 9."""
        data = load(dataset(tmp_path))

        row = data.table("coverage")[0]
        assert row["pairs_formed"] == 3370446
        assert isinstance(row["pairs_formed"], int)
        assert row["comparable_share"] == 0.093
        assert row["system"] == "Mount Sinai"

    def test_a_code_that_looks_numeric_stays_text(self, tmp_path):
        """`code` is an identifier; coercing it would let the page sum it."""
        data = load(dataset(tmp_path))

        assert data.table("exemplars")[0]["code"] == "99213"

    def test_a_missing_table_is_named_rather_than_fatal(self, tmp_path):
        directory = dataset(tmp_path)
        (directory / "magnitude.csv").unlink()

        data = load(directory)

        assert "magnitude" in data.missing
        assert data.table("magnitude") == []
        assert not data.is_empty, "one absent table must not blank the whole page"

    def test_a_missing_run_json_is_named(self, tmp_path):
        data = load(dataset(tmp_path, with_run=False))

        assert "run.json" in data.missing

    def test_an_absent_directory_reports_empty(self, tmp_path):
        data = load(tmp_path / "nothing")

        assert data.is_empty
        assert set(data.missing) >= {"coverage", "run.json"}


class TestFilters:
    def test_options_come_from_the_data(self, tmp_path):
        """Hardcoded lists drift the moment a carrier is added."""
        choices = options(load(dataset(tmp_path)))

        assert choices["carrier"] == [ALL, "Aetna", "UnitedHealthcare"]
        assert choices["code_type"] == [ALL, "CPT", "MS-DRG"]
        assert choices["system"][0] == ALL

    def test_all_means_no_narrowing(self, tmp_path):
        rows = load(dataset(tmp_path)).table("outcomes")

        assert len(apply_filters(rows)) == 3

    def test_filters_combine(self, tmp_path):
        rows = load(dataset(tmp_path)).table("outcomes")

        got = apply_filters(rows, system="Mount Sinai", code_type="CPT")

        assert len(got) == 1
        assert got[0]["explanation"] == "unexplained"

    def test_a_filter_the_table_cannot_answer_is_reported(self, tmp_path):
        """The test this module exists for.

        Refusals have no carrier column. Selecting a carrier must be reported as
        inapplicable, not quietly skipped -- a control that looks applied and is
        not is worse than one that is absent.
        """
        rows = load(dataset(tmp_path)).table("refusals")

        assert inapplicable_filters(rows, system=ALL, carrier="Aetna", code_type=ALL) == ["carrier"]
        assert inapplicable_filters(rows, system=ALL, carrier="Aetna", code_type="CPT") == [
            "carrier",
            "code_type",
        ]

    def test_an_applicable_filter_is_not_reported(self, tmp_path):
        rows = load(dataset(tmp_path)).table("refusals")

        assert inapplicable_filters(rows, system="Mount Sinai", carrier=ALL, code_type=ALL) == []

    def test_refusals_still_answer_the_filter_they_can(self, tmp_path):
        rows = load(dataset(tmp_path)).table("refusals")

        got = apply_filters(rows, system="Northwell", carrier="Aetna")

        assert len(got) == 1, "system narrows; carrier cannot and must not drop everything"
        assert got[0]["reason"] == "billing_class_unstated"

    def test_the_note_names_the_reason_not_just_the_fact(self):
        assert "without keeping its carrier" in REFUSALS_GRAIN_NOTE


class TestViews:
    def test_the_funnel_filters_by_system(self, tmp_path):
        data = load(dataset(tmp_path))

        assert len(funnel(data)) == 2
        assert len(funnel(data, system="Northwell")) == 1

    def test_the_outcomes_chart_totals_by_explanation(self, tmp_path):
        rows = load(dataset(tmp_path)).table("outcomes")

        chart = outcomes_chart(rows)

        assert chart[0] == {"explanation": "systematic_offset", "pairs": 400}
        assert {c["explanation"] for c in chart} == {"systematic_offset", "unexplained"}
        assert sum(c["pairs"] for c in chart) == 750

    def test_widest_puts_the_largest_first_and_caps(self, tmp_path):
        rows = load(dataset(tmp_path)).table("exemplars")

        got = widest(rows, limit=1)

        assert len(got) == 1
        assert got[0]["code"] == "70450"


class TestProvenanceIsShown:
    def test_the_vintages_are_surfaced(self, tmp_path):
        """A stale snapshot looks exactly like a fresh one without these."""
        data = load(dataset(tmp_path))

        shown = staleness(data.metadata)

        assert shown["Built"].startswith("2026-09-18")
        assert shown["Bronze ingest"] == "2026-09-13"
        assert shown["Gold"] == "unknown", "absent is shown as unknown, not omitted"

    def test_caveats_survive_to_the_page(self, tmp_path):
        data = load(dataset(tmp_path))

        assert caveats(data.metadata) == ["No PHI.", "NYU Langone is from a local run."]

    def test_no_metadata_still_renders_labels(self):
        assert set(staleness({})) == {
            "Built",
            "Bronze ingest",
            "Hospital silver",
            "Payer silver",
            "Gold",
        }
        assert set(staleness({}).values()) == {"unknown"}


ROWS = [
    {"system": "NYU Langone", "reason": "zero_rate", "carrier": "", "candidates": "90"},
    {"system": "Mount Sinai", "reason": "zero_rate", "carrier": "Aetna", "candidates": "5"},
    {"system": "Mount Sinai", "reason": "zero_rate", "carrier": "", "candidates": "2"},
]


class TestUnattributedRefusals:
    """A carrier filter drops blank-carrier rows; the page must say whose."""

    def test_it_counts_what_a_carrier_filter_dropped_by_system(self):
        from pipeline.summary_view import unattributed_excluded

        got = unattributed_excluded(ROWS, system=ALL, carrier="Aetna", code_type=ALL)

        assert got == {"Mount Sinai": 2, "NYU Langone": 90}

    def test_no_filter_drops_nothing(self):
        from pipeline.summary_view import unattributed_excluded

        assert unattributed_excluded(ROWS, system=ALL, carrier=ALL, code_type=ALL) == {}

    def test_a_system_filter_alone_drops_nothing_unattributed(self):
        from pipeline.summary_view import unattributed_excluded

        assert unattributed_excluded(ROWS, system="NYU Langone", carrier=ALL, code_type=ALL) == {}

    def test_it_respects_the_system_filter(self):
        from pipeline.summary_view import unattributed_excluded

        got = unattributed_excluded(ROWS, system="Mount Sinai", carrier="Aetna", code_type=ALL)

        assert got == {"Mount Sinai": 2}
