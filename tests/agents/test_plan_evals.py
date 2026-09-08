"""Tests for the plan-matching eval harness, and a gate on the shipped set.

The harness tests use small hand-built sets so the arithmetic is checkable. The
last class runs the real ``evals/plan_matching.jsonl``, which is what stops a
change to the matcher from silently losing matches.
"""

from pathlib import Path

import pytest

from agents.plan_evals import (
    PlanEvalSet,
    PlanLabel,
    append_result,
    gate,
    load_default,
    score_plan_matcher,
)

EVAL_SET = Path(__file__).parent.parent.parent / "evals" / "plan_matching.jsonl"


def label(plan: str, network: str, expected: str, reviewed: bool = False) -> PlanLabel:
    return PlanLabel(key=f"{plan} || {network}", expected=expected, reviewed=reviewed)


class TestScoring:
    def test_a_correct_match_is_a_true_positive(self):
        score, misses = score_plan_matcher(
            PlanEvalSet("t", [label("Cigna Localplus - Msq", "LocalPlus", "match")])
        )

        assert score.true_positives == 1
        assert score.precision == 1.0
        assert misses == []

    def test_a_wrong_match_is_a_false_positive(self):
        """The expensive error: it licenses a comparison that should not happen."""
        score, misses = score_plan_matcher(
            PlanEvalSet("t", [label("Cigna Ppo - Msq", "NationalPPO", "no_match")])
        )

        assert score.false_positives == 1
        assert score.precision == 0.0
        assert misses

    def test_abstaining_costs_recall_not_precision(self):
        """A declined pair stays unresolved, which is visible and honest."""
        score, _ = score_plan_matcher(
            PlanEvalSet("t", [label("SCREEN ACTORS GUILD 1220", "NationalPPO", "match")])
        )

        assert score.false_negatives == 1
        assert score.recall == 0.0
        assert score.false_positives == 0

    def test_confusing_two_non_matches_is_not_a_false_positive(self):
        """Wrong about which kind of non-match, but it still refuses to compare."""
        score, _ = score_plan_matcher(
            PlanEvalSet("t", [label("Oxford Indemnity - Msq", "ChoiceEPO", "unknown")])
        )

        assert score.false_positives == 0
        assert score.true_negatives == 1
        assert score.confused_non_matches == 1

    def test_f1_is_zero_when_nothing_matches(self):
        score, _ = score_plan_matcher(
            PlanEvalSet("t", [label("Oxford Commercial - Bi", "ChoicePlus", "unknown")])
        )

        assert score.f1 == 0.0
        assert score.accuracy == 1.0


class TestGate:
    def test_precision_is_the_gate_not_recall(self):
        score, _ = score_plan_matcher(
            PlanEvalSet(
                "t",
                [
                    label("Cigna Localplus - Msq", "LocalPlus", "match"),
                    label("SCREEN ACTORS GUILD 1220", "NationalPPO", "match"),
                ],
            )
        )

        # One found, one missed: recall 0.5, precision 1.0.
        assert score.recall == pytest.approx(0.5)
        assert gate(score)[0], "a miss must not fail the gate; a wrong match must"

    def test_a_false_match_fails_the_gate(self):
        score, _ = score_plan_matcher(
            PlanEvalSet("t", [label("Cigna Ppo - Msq", "NationalPPO", "no_match")])
        )

        ok, why = gate(score)
        assert not ok
        assert "differenced wrongly" in why

    def test_a_matcher_that_never_matches_cannot_pass(self):
        """Precision is undefined on no predictions; that is not a pass."""
        score, _ = score_plan_matcher(
            PlanEvalSet("t", [label("Oxford Commercial - Bi", "ChoicePlus", "unknown")])
        )

        ok, why = gate(score)
        assert not ok
        assert "never matched" in why


class TestPersistence:
    def test_results_are_appended_not_overwritten(self, tmp_path):
        score, _ = score_plan_matcher(
            PlanEvalSet("t", [label("Cigna Localplus - Msq", "LocalPlus", "match")])
        )
        path = tmp_path / "results.jsonl"
        append_result(score, path)
        append_result(score, path)

        assert len(path.read_text(encoding="utf-8").strip().splitlines()) == 2

    def test_a_set_round_trips(self, tmp_path):
        original = PlanEvalSet("t", [label("Cigna Ppo - Bi", "NationalPPO", "match", True)])
        path = tmp_path / "t.jsonl"
        original.save(path)

        assert PlanEvalSet.load(path).labels == original.labels


class TestShippedEvalSet:
    """The real set. These are the assertions that guard the matcher."""

    def test_the_set_exists_and_is_loadable(self):
        assert EVAL_SET.exists(), "the labelled set must be committed with the matcher"
        assert len(load_default(EVAL_SET.parent).labels) > 100

    def test_the_rule_based_matcher_passes_its_gate(self):
        score, _ = score_plan_matcher(load_default(EVAL_SET.parent))
        ok, why = gate(score)

        assert ok, why
        assert score.true_positives > 0

    def test_no_pair_is_matched_wrongly(self):
        """A false positive puts a variance between two contracts into the mart."""
        score, misses = score_plan_matcher(load_default(EVAL_SET.parent))

        assert score.false_positives == 0, [(m.plan_raw, m.network) for m, _ in misses]

    def test_every_label_has_been_reviewed(self):
        """The labels were proposed by a model and then signed off by a person.

        This assertion was the opposite until that review happened, so flipping
        it is the record of it. A label added later lands unreviewed and fails
        here until someone has actually looked at it, which is the point.
        """
        eval_set = load_default(EVAL_SET.parent)

        assert eval_set.reviewed_share == 1.0
        assert all("reviewed" in x.labelled_by for x in eval_set.labels)

    def test_every_label_carries_its_reasoning(self):
        assert all(x.note for x in load_default(EVAL_SET.parent).labels)
