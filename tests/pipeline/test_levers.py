"""The refusal decomposition: attribution only, so the tests are about honesty.

Nothing here computes a new number. What could go wrong is attribution that
quietly misleads: a share of the wrong denominator, a lever that cannot move
the share left out rather than stated at zero, a refusal without a carrier
assigned to a guess.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pipeline.levers import (
    BY_REASON,
    NO_COUNTERPART,
    Decomposition,
    lever_for,
    main,
    markdown,
)

REPO = Path(__file__).resolve().parents[2]
CORPUS = frozenset({"Aetna", "UnitedHealthcare"})


def decomposition(refusals: list[dict[str, Any]], pairs: int = 100) -> Decomposition:
    refused = sum(r["candidates"] for r in refusals)
    return Decomposition(
        coverage=[{"system": "S", "candidates": pairs + refused, "pairs_formed": pairs}],
        refusals=[{"system": "S", **r} for r in refusals],
        outcomes=[
            {"system": "S", "carrier": "Aetna", "explanation": "plan_unresolved", "pairs": 60},
            {"system": "S", "carrier": "Aetna", "explanation": "unexplained", "pairs": 40},
        ],
    )


class TestLevers:
    def test_a_tic_exempt_refusal_is_correct_and_stays_one(self):
        """Medicare Advantage and Medicaid exist only hospital-side, by rule."""
        lever = lever_for("tic_exempt_product", "Aetna", CORPUS)

        assert lever.name == "none"
        assert lever.feasibility == "none"

    def test_no_counterpart_splits_on_who_the_hospital_named(self):
        assert lever_for(NO_COUNTERPART, "Aetna", CORPUS).feasibility == "low"
        assert lever_for(NO_COUNTERPART, "Healthfirst", CORPUS).name == "none"
        assert lever_for(NO_COUNTERPART, "connecticare", CORPUS).name == "payer/plan name matching"

    def test_a_refusal_without_a_carrier_is_unattributed_not_guessed(self):
        assert lever_for(NO_COUNTERPART, None, CORPUS).name == "unattributed"
        assert lever_for(NO_COUNTERPART, "", CORPUS).name == "unattributed"

    def test_an_unknown_reason_is_unclassified_not_dropped(self):
        assert lever_for("something new", "Aetna", CORPUS).name == "unclassified"


class TestArithmetic:
    def test_lift_is_a_share_of_all_candidates(self):
        d = decomposition(
            [{"reason": "mixed_rate_kind", "carrier": "Aetna", "candidates": 100}], pairs=300
        )

        row = d.by_reason()[0]

        assert row["lift_if_resolved"] == pytest.approx(0.25)
        assert row["weighted_lift"] == pytest.approx(0.25 * 0.5)

    def test_levers_rank_by_lift_times_feasibility_not_lift(self):
        """A large refusal nobody can fix must not outrank a smaller fixable one."""
        d = decomposition(
            [
                {"reason": "tic_exempt_product", "carrier": "Aetna", "candidates": 900},
                {"reason": "mixed_rate_kind", "carrier": "Aetna", "candidates": 100},
            ]
        )

        ranked = [r["lever"] for r in d.by_lever()]

        assert ranked.index("methodology normalization") < ranked.index("none")

    def test_levers_that_cannot_move_the_share_are_stated_at_zero(self):
        """Absent from the table would read as 'not considered'."""
        d = decomposition([{"reason": "zero_rate", "carrier": "Aetna", "candidates": 10}])

        levers = {r["lever"]: r for r in d.by_lever()}

        assert levers["plan matching"]["lift_if_resolved"] == 0.0
        assert levers["vintage tolerance"]["lift_if_resolved"] == 0.0

    def test_expected_weights_refusals_by_feasibility(self):
        d = decomposition(
            [
                {"reason": "tic_exempt_product", "carrier": "Aetna", "candidates": 100},
                {"reason": "mixed_rate_kind", "carrier": "Aetna", "candidates": 100},
            ],
            pairs=100,
        )

        row = d.per_system()[0]

        assert row["share_now"] == pytest.approx(1 / 3)
        assert row["expected"] == pytest.approx((100 + 100 * 0.5) / 300)

    def test_keying_on_billing_class_changes_the_denominator_not_the_pairs(self):
        """A definition, reported beside the share and never as a lift."""
        d = decomposition(
            [{"reason": "different_billing_class", "carrier": "Aetna", "candidates": 700}],
            pairs=100,
        )

        row = d.per_system()[0]

        assert row["share_now"] == pytest.approx(0.125)
        assert row["billing_class_share"] == pytest.approx(0.875)
        assert row["billing_class_keyed"] == pytest.approx(1.0)

    def test_plan_unresolved_is_a_share_of_pairs(self):
        d = decomposition([])

        assert d.plan_unresolved_share() == pytest.approx(0.6)

    def test_a_document_is_refused_if_the_arithmetic_does_not_close(self, tmp_path):
        summary = tmp_path / "summary"
        summary.mkdir()
        (summary / "coverage.csv").write_text(
            "system,candidates,pairs_formed\nS,100,10\n", encoding="utf-8"
        )
        (summary / "refusals.csv").write_text(
            "system,reason,carrier,candidates\nS,zero_rate,Aetna,50\n", encoding="utf-8"
        )
        (summary / "outcomes.csv").write_text(
            "system,carrier,explanation,pairs\nS,Aetna,unexplained,10\n", encoding="utf-8"
        )

        code = main(["--summary", str(summary), "--out", str(tmp_path / "out.md")])

        assert code == 1
        assert not (tmp_path / "out.md").exists()


class TestAgainstThePublishedSummary:
    def test_candidates_are_pairs_plus_refusals_for_every_system(self):
        """Otherwise every share in the document is a share of something else."""
        d = Decomposition.load(REPO / "summary")

        for system, (candidates, accounted) in d.arithmetic_holds().items():
            assert candidates == accounted, system

    def test_every_published_reason_has_a_lever(self):
        d = Decomposition.load(REPO / "summary")
        known = set(BY_REASON) | {NO_COUNTERPART}

        assert {r["reason"] for r in d.refusals} <= known

    def test_the_document_renders(self):
        text = markdown(Decomposition.load(REPO / "summary"))

        assert "lift x feasibility" in text
        assert "Plan matching lifts comparable share by zero" in text
