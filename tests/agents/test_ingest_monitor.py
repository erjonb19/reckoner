"""A4 tests.

The diffs are built by moving a real payer file, so what is classified is what
the manifest actually emits rather than a hand-written approximation of it.

Two things get pinned harder than the rest. Every verdict has to be a claim
about consequence -- a cosmetic verdict is a promise that a published figure is
still correct, so the one rule that issues it is tested from both directions.
And an unrecognised combination has to reach the review queue rather than a
guess, because that queue is the eval set the agent will eventually need and a
guess would poison it.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from agents.ingest_monitor import (
    Assessment,
    ChangeKind,
    Materiality,
    RuleBasedAssessor,
    monitor,
    validate_assessment,
)
from payer.manifest import ManifestDiff, build, diff

REAL = Path(__file__).parent.parent / "fixtures" / "payer_parquet" / "Emblem_HIPHOSH00687.parquet"
STEM = "Emblem_HIPHOSH00687"


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    pq.write_table(pq.read_table(REAL), tmp_path / f"{STEM}.parquet")
    return tmp_path


def changed(fields: list[str], stem: str = STEM) -> ManifestDiff:
    return ManifestDiff(changed={stem: fields})


def only(watch) -> Assessment:
    assert len(watch.assessments) == 1, watch.summary()
    return watch.assessments[0]


class TestAgainstRealDiffs:
    def test_an_unchanged_boundary_is_quiet(self, lake):
        watch = monitor(diff(build(lake), build(lake)))

        assert watch.quiet
        assert watch.assessments == []

    def test_a_new_file_is_material(self, lake):
        before = build(lake)
        pq.write_table(pq.read_table(REAL), lake / "Emblem_NEW0001.parquet")

        watch = monitor(diff(before, build(lake)))

        assert only(watch).kind is ChangeKind.FILE_ADDED
        assert only(watch).materiality is Materiality.MATERIAL
        assert not watch.quiet

    def test_a_vanished_file_is_material(self, lake):
        before = build(lake)
        (lake / f"{STEM}.parquet").unlink()

        assert only(monitor(diff(before, build(lake)))).kind is ChangeKind.FILE_REMOVED

    def test_a_real_vintage_move_is_material(self, lake):
        """The change most likely to go unnoticed: same file, newer reporting month."""
        before = build(lake)
        table = pq.read_table(REAL)
        index = table.schema.get_field_index("last_updated_on")
        pq.write_table(
            table.set_column(
                index, "last_updated_on", pa.array(["2026-10-01"] * table.num_rows, pa.string())
            ),
            lake / f"{STEM}.parquet",
        )

        assessment = only(monitor(diff(before, build(lake))))

        assert assessment.kind is ChangeKind.VINTAGE_CHANGED
        assert assessment.materiality is Materiality.MATERIAL

    def test_a_real_schema_change_is_material(self, lake):
        before = build(lake)
        pq.write_table(pq.read_table(REAL).drop_columns(["description"]), lake / f"{STEM}.parquet")

        assert only(monitor(diff(before, build(lake)))).kind is ChangeKind.SCHEMA_CHANGED


class TestTheOneCosmeticVerdict:
    def test_a_rewrite_at_the_same_vintage_and_row_count_is_cosmetic(self):
        assessment = only(monitor(changed(["bytes: 100 -> 120", "row_groups: 1 -> 2"])))

        assert assessment.kind is ChangeKind.REWRITTEN
        assert assessment.materiality is Materiality.COSMETIC

    def test_the_cosmetic_verdict_states_what_it_rests_on(self):
        """It promises a published figure is still right, so it must say why."""
        assessment = only(monitor(changed(["bytes: 100 -> 120"])))

        assert "--hash" in assessment.reason

    def test_size_moving_alongside_rows_is_not_cosmetic(self):
        """Same-size-different-rows is the case a lazy rule would wave through."""
        assessment = only(monitor(changed(["bytes: 100 -> 120", "rows: 20 -> 19"])))

        assert assessment.materiality is Materiality.MATERIAL
        assert assessment.kind is ChangeKind.ROWS_CHANGED

    def test_content_moving_under_an_unchanged_footer_is_material(self):
        """Only visible with --hash, and precisely the silent case."""
        assessment = only(monitor(changed(["sha256: aaa -> bbb"])))

        assert assessment.kind is ChangeKind.CONTENT_CHANGED
        assert assessment.materiality is Materiality.MATERIAL


class TestSeverityOrdering:
    def test_the_most_consequential_field_decides(self):
        """A file that moved vintage and rows is a vintage change, not a row change."""
        assessment = only(monitor(changed(["rows: 20 -> 40", "vintage: 2026-09-04 -> 2026-10-01"])))

        assert assessment.kind is ChangeKind.VINTAGE_CHANGED

    def test_state_outranks_everything(self):
        assessment = only(
            monitor(changed(["state: read -> unreadable", "vintage: a -> b", "rows: 1 -> 2"]))
        )

        assert assessment.kind is ChangeKind.STATE_CHANGED


class TestTheReviewQueue:
    def test_an_unmodelled_field_goes_to_review_not_to_a_guess(self):
        """That queue is the eval set the agent will need; a guess would poison it."""
        watch = monitor(changed(["carrier: Emblem -> Empire"]))

        assert only(watch).materiality is Materiality.UNKNOWN
        assert watch.review_queue == watch.assessments
        assert not watch.quiet, "an unreviewed unknown must never read as all clear"

    def test_review_entries_still_say_what_they_saw(self):
        assessment = only(monitor(changed(["carrier: Emblem -> Empire"])))

        assert "carrier" in assessment.reason


class TestValidationGuardsTheAgentThatDoesNotExistYet:
    def test_an_assessment_about_a_file_not_in_the_diff_is_rejected(self):
        ok, why = validate_assessment(
            Assessment("Invented_File", ChangeKind.ROWS_CHANGED, Materiality.MATERIAL, "because"),
            changed(["rows: 1 -> 2"]),
        )

        assert not ok
        assert "not in this diff" in why

    def test_an_assessment_citing_fields_the_diff_never_reported_is_rejected(self):
        ok, why = validate_assessment(
            Assessment(
                STEM, ChangeKind.ROWS_CHANGED, Materiality.MATERIAL, "because", ("rows", "vintage")
            ),
            changed(["rows: 1 -> 2"]),
        )

        assert not ok
        assert "vintage" in why

    def test_an_assessment_with_no_reason_is_rejected(self):
        ok, why = validate_assessment(
            Assessment(STEM, ChangeKind.ROWS_CHANGED, Materiality.MATERIAL, "   "),
            changed(["rows: 1 -> 2"]),
        )

        assert not ok
        assert "reviewable" in why

    def test_an_invalid_assessment_is_surfaced_not_dropped(self):
        """A monitor that discards what it cannot verify is worse than one that says so."""

        class Liar:
            def assess(self, _diff: ManifestDiff) -> list[Assessment]:
                return [Assessment("Nowhere", ChangeKind.ROWS_CHANGED, Materiality.COSMETIC, "hi")]

        watch = monitor(changed(["rows: 1 -> 2"]), assessor=Liar())

        assert watch.assessments == []
        assert len(watch.invalid) == 1
        assert not watch.quiet, "an unverifiable assessment must not read as all clear"

    def test_the_rules_pass_their_own_validation(self):
        d = ManifestDiff(added=["A_x"], removed=["B_y"], changed={STEM: ["rows: 1 -> 2"]})

        for assessment in RuleBasedAssessor().assess(d):
            ok, why = validate_assessment(assessment, d)
            assert ok, why
