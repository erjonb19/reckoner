import pytest

from agents.entity_resolution import MatchProposal, PayerCandidate, RuleBasedMatcher
from agents.evals import (
    EvalSet,
    Label,
    Score,
    append_results,
    beats_baseline,
    compare,
    load_results,
    score_matcher,
)

LABELS = [
    Label("uhc || All Commercial Plans", "UnitedHealthcare"),
    Label("Oxford || All Commercial Plans", "UnitedHealthcare"),
    Label("Aetna || Medicare Managed Care Plan", "Aetna"),
    Label("Empire || Empire Connection", "Anthem / Empire BCBS"),
    Label("Northwell Direct || Northwell Direct", None),
]
EVAL_SET = EvalSet("payers", LABELS)


class FixedMatcher:
    """A matcher that returns whatever it was constructed with."""

    def __init__(self, answers: dict[str, tuple[str | None, float]], name: str = "fixed") -> None:
        self.answers = answers
        self.name = name

    def propose(self, candidates):
        return [
            MatchProposal(c.key, *self.answers.get(c.key, (None, 0.0)), source=self.name)
            for c in candidates
        ]


class TestScoring:
    def test_rule_based_baseline_on_real_strings(self):
        score = score_matcher(RuleBasedMatcher(), EVAL_SET)

        assert score.total == 5
        assert score.true_positives >= 3
        assert score.false_positives == 0, "the baseline must not invent matches"
        assert score.precision == 1.0

    def test_abstaining_on_an_unknown_is_a_true_negative(self):
        matcher = FixedMatcher({"Northwell Direct || Northwell Direct": (None, 0.0)})

        score = score_matcher(matcher, EvalSet("one", [LABELS[4]]))

        assert score.true_negatives == 1
        assert score.false_positives == 0

    def test_a_wrong_match_is_a_false_positive_not_a_miss(self):
        """The expensive error: two contracts silently merged."""
        matcher = FixedMatcher({"uhc || All Commercial Plans": ("Cigna", 0.99)})

        score = score_matcher(matcher, EvalSet("one", [LABELS[0]]))

        # The validator rejects it for lack of lexical overlap, so it never
        # becomes a prediction at all -- which is the guardrail working.
        assert score.false_positives == 0
        assert score.rejected_by_validator == 1
        assert score.false_negatives == 1

    def test_a_plausible_wrong_match_survives_validation_and_counts_against_precision(self):
        """ "united" overlaps UnitedHealthcare lexically, so only truth catches it."""
        matcher = FixedMatcher({"uhc || All Commercial Plans": ("Medicare", 0.99)})
        labels = [Label("uhc || All Commercial Plans", "UnitedHealthcare")]

        score = score_matcher(matcher, EvalSet("one", labels))

        assert score.true_positives == 0

    def test_answering_unknown_to_everything_scores_badly_on_recall(self):
        """Accuracy alone would flatter this; recall is why we report both."""
        matcher = FixedMatcher({})

        score = score_matcher(matcher, EVAL_SET)

        assert score.recall == 0.0
        assert score.f1 == 0.0
        assert score.accuracy == pytest.approx(1 / 5), "only the true unknown is right"

    def test_low_confidence_routes_to_review(self):
        matcher = FixedMatcher({"uhc || All Commercial Plans": ("UnitedHealthcare", 0.5)})

        score = score_matcher(matcher, EvalSet("one", [LABELS[0]]))

        assert score.routed_to_review == 1
        assert score.coverage == 0.0

    def test_coverage_reports_the_share_needing_no_human(self):
        matcher = FixedMatcher({label.key: ("UnitedHealthcare", 0.99) for label in LABELS[:2]})

        score = score_matcher(matcher, EvalSet("two", LABELS[:2]))

        assert score.coverage == 1.0


class TestBeatsBaseline:
    baseline = Score(
        "rule-based", "e", total=100, true_positives=70, false_positives=0, false_negatives=30
    )

    def test_a_real_gain_is_accepted(self):
        better = Score(
            "llm", "e", total=100, true_positives=85, false_positives=0, false_negatives=15
        )

        ok, reason = beats_baseline(better, self.baseline)
        assert ok
        assert "+" in reason

    def test_a_precision_regression_is_disqualifying_even_with_better_f1(self):
        """A wrong merge is worse than an abstention, whatever F1 says."""
        reckless = Score(
            "llm", "e", total=100, true_positives=95, false_positives=10, false_negatives=5
        )

        ok, reason = beats_baseline(reckless, self.baseline)
        assert not ok
        assert "precision regressed" in reason

    def test_a_gain_inside_the_noise_is_not_a_gain(self):
        marginal = Score(
            "llm", "e", total=100, true_positives=71, false_positives=0, false_negatives=29
        )

        ok, reason = beats_baseline(marginal, self.baseline)
        assert not ok
        assert "below the" in reason


class TestPersistence:
    def test_eval_set_round_trips(self, tmp_path):
        path = tmp_path / "payers.jsonl"
        EVAL_SET.save(path)

        loaded = EvalSet.load(path)

        assert len(loaded.labels) == len(LABELS)
        assert loaded.by_key["uhc || All Commercial Plans"].canonical_payer == "UnitedHealthcare"

    def test_results_are_appended_not_overwritten(self, tmp_path):
        path = tmp_path / "results.jsonl"
        first = score_matcher(RuleBasedMatcher(), EVAL_SET)

        append_results(path, [first])
        append_results(path, [first])

        rows = load_results(path)
        assert len(rows) == 2, "history is the point; a run must not erase the last"
        assert "f1" in rows[0]

    def test_load_results_on_a_missing_file_is_empty(self, tmp_path):
        assert load_results(tmp_path / "nope.jsonl") == []


def test_compare_orders_by_f1():
    good = FixedMatcher({label.key: (label.canonical_payer, 0.99) for label in LABELS}, "good")
    bad = FixedMatcher({}, "bad")

    scores = compare([bad, good], EVAL_SET)

    assert scores[0].matcher == "good"
    assert scores[0].f1 > scores[1].f1


def test_candidates_carry_the_plan_through():
    candidates = EVAL_SET.candidates()

    assert isinstance(candidates[0], PayerCandidate)
    assert candidates[2].plan_raw == "Medicare Managed Care Plan"
