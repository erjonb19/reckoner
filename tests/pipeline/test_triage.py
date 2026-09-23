"""One test per rule, plus the properties the queue has to have.

These rules run on the residual, which the mart has already stripped of every
finding a deterministic rule could account for. So they are near-miss detectors
by construction, and the fixtures have to sit deliberately just the wrong side
of the mart's thresholds -- a rule that only fires on cases the mart would have
caught fires on nothing in production.
"""

from __future__ import annotations

from pipeline.triage import (
    MARGINAL_RELATIVE_DIFFERENCE,
    NEAR_OFFSET_MIN_CODES,
    RULES,
    WIDE_VINTAGE_GAP_DAYS,
    classify,
    queue,
    summarise,
    summarise_by_system,
    vintage_gap_days,
)


def finding(**overrides: object) -> dict[str, object]:
    """A residual finding that no rule should fire on unless asked."""
    base = {
        "system": "Mount Sinai",
        "facility": "The Mount Sinai Hospital",
        "carrier": "Aetna",
        "code": "99213",
        "code_type": "CPT",
        "hospital_plan": "Commercial PPO",
        "payer_plan": "Commercial PPO",
        "hospital_rate": 100.0,
        "payer_rate": 130.0,
        "ratio": 1.3,
        "relative_difference": 0.30,
        "is_implausible": False,
        "hospital_vintage": "2026-04-01",
        "payer_vintage": "2026-04-15",
    }
    base.update(overrides)
    return base


class TestEachRule:
    def test_a_baseline_finding_is_unexplained(self):
        """If this ever stops being true, every other test below is suspect."""
        assert classify(finding()).name == "unexplained"

    def test_implausible_fires_on_a_tenfold_gap(self):
        assert classify(finding(hospital_rate=100.0, payer_rate=1200.0)).name == "implausible"

    def test_implausible_honours_the_flag_gold_already_set(self):
        assert classify(finding(is_implausible=True)).name == "implausible"

    def test_a_wide_vintage_gap_fires(self):
        far = finding(hospital_vintage="2026-01-01", payer_vintage="2026-09-01")

        assert vintage_gap_days(far) >= WIDE_VINTAGE_GAP_DAYS
        assert classify(far).name == "vintage_artifact"

    def test_a_narrow_vintage_gap_does_not(self):
        assert classify(finding(payer_vintage="2026-04-10")).name == "unexplained"

    def test_a_missing_vintage_is_unknown_rather_than_zero(self):
        """Scoring it as same-day would make it look like the best-aligned row."""
        assert vintage_gap_days(finding(payer_vintage="")) is None
        assert classify(finding(payer_vintage="")).name == "unexplained"

    def test_granularity_fires_when_only_one_side_names_a_plan(self):
        assert classify(finding(payer_plan="")).name == "granularity_mismatch"

    def test_granularity_does_not_fire_when_neither_names_one(self):
        """Both absent is symmetric, and symmetric is not a mismatch."""
        assert classify(finding(hospital_plan="", payer_plan="")).name == "unexplained"

    def test_a_shared_ratio_across_several_codes_fires(self):
        """Below the 20 distinct codes an offset needs, above coincidence."""
        rows = [
            finding(code=f"9921{i}", ratio=1.3, relative_difference=0.30)
            for i in range(NEAR_OFFSET_MIN_CODES + 1)
        ]

        classified = queue(rows)

        assert {r["triage_rule"] for r in classified} == {"systematic_offset"}

    def test_a_ratio_shared_by_too_few_does_not_fire(self):
        rows = [finding(code=f"9921{i}", ratio=1.3) for i in range(2)]

        assert {r["triage_rule"] for r in queue(rows)} == {"unexplained"}

    def test_marginal_fires_just_above_the_materiality_line(self):
        marginal = finding(relative_difference=MARGINAL_RELATIVE_DIFFERENCE / 2)

        assert classify(marginal).name == "marginal"

    def test_marginal_does_not_swallow_a_real_gap(self):
        assert classify(finding(relative_difference=0.35)).name == "unexplained"


class TestPrecedence:
    def test_implausible_beats_a_wide_vintage_gap(self):
        """A tenfold gap is a data question; answering timing first buries it."""
        both = finding(
            hospital_rate=100.0,
            payer_rate=1500.0,
            hospital_vintage="2026-01-01",
            payer_vintage="2026-09-01",
        )

        assert classify(both).name == "implausible"

    def test_every_finding_gets_exactly_one_rule(self):
        """A queue where some rows carry two verdicts is one someone re-derives."""
        rows = [
            finding(),
            finding(payer_rate=2000.0),
            finding(payer_plan=""),
            finding(relative_difference=0.01),
        ]

        classified = queue(rows)

        assert len(classified) == len(rows)
        assert all(isinstance(r["triage_rule"], str) for r in classified)

    def test_unexplained_is_last_and_always_matches(self):
        assert RULES[-1].name == "unexplained"
        assert RULES[-1].fires({}, {}) is True


class TestTheQueue:
    def test_it_is_ordered_by_priority_then_size(self):
        rows = [
            finding(relative_difference=0.10),
            finding(hospital_rate=100.0, payer_rate=5000.0, relative_difference=49.0),
            finding(relative_difference=0.90),
        ]

        classified = queue(rows)

        assert classified[0]["triage_rule"] == "implausible"
        assert classified[-1]["triage_rule"] == "unexplained"

    def test_it_keeps_every_original_column(self):
        """The queue is read on its own; dropping context would break that."""
        classified = queue([finding()])[0]

        assert classified["facility"] == "The Mount Sinai Hospital"
        assert classified["code"] == "99213"
        assert classified["triage_why"]

    def test_the_vintage_gap_is_published_not_just_used(self):
        classified = queue([finding(payer_vintage="2026-06-01")])[0]

        assert classified["vintage_gap_days"] == 61

    def test_an_empty_residual_is_an_empty_queue(self):
        assert queue([]) == []
        assert summarise([]) == []


class TestTheSummary:
    def test_it_counts_and_shares_by_rule(self):
        rows = queue([finding(), finding(), finding(payer_rate=3000.0)])

        counted = summarise(rows)

        by_rule = {r["triage_rule"]: r for r in counted}
        assert by_rule["unexplained"]["findings"] == 2
        assert by_rule["implausible"]["findings"] == 1
        assert abs(sum(r["share"] for r in counted) - 1.0) < 1e-6

    def test_it_is_ordered_by_priority(self):
        rows = queue([finding(), finding(payer_rate=3000.0)])

        assert [r["triage_rule"] for r in summarise(rows)] == ["implausible", "unexplained"]

    def test_it_carries_the_reason_so_the_number_can_be_read(self):
        counted = summarise(queue([finding()]))

        assert "A1 exists for" in counted[0]["why"]


class TestTheQueueBecomesArrow:
    """Written after the stage crashed on the first rows with no payer vintage.

    Every earlier row had both vintages, so the gap column was always an int
    and a blank-string fallback never met Arrow. The distribution grain
    produced the first unknown gap, and the write failed.
    """

    def test_a_queue_mixing_known_and_unknown_gaps_converts(self):
        import pyarrow as pa

        rows = queue([finding(), finding(code="99214", payer_vintage="")])

        table = pa.Table.from_pylist(rows)

        gaps = table.column("vintage_gap_days").to_pylist()
        assert None in gaps
        assert any(isinstance(g, int) for g in gaps)


class TestTheSummaryIsPerSystem:
    """Written after the summary landed outside every gold partition."""

    def test_every_row_names_its_system(self):
        rows = queue(
            [
                finding(hospital_slug="mount-sinai-health-system", system="Mount Sinai"),
                finding(code="99214", hospital_slug="white-plains-hospital", system="White Plains"),
            ]
        )

        got = summarise_by_system(rows)

        assert {r["hospital_slug"] for r in got} == {
            "mount-sinai-health-system",
            "white-plains-hospital",
        }
        assert all(r["system"] for r in got)

    def test_per_system_counts_sum_to_the_pooled_ones(self):
        rows = queue(
            [
                finding(hospital_slug="a", system="A"),
                finding(code="2", hospital_slug="b", system="B"),
                finding(code="3", hospital_slug="b", system="B"),
            ]
        )

        pooled = {r["triage_rule"]: r["findings"] for r in summarise(rows)}
        split: dict[str, int] = {}
        for r in summarise_by_system(rows):
            split[r["triage_rule"]] = split.get(r["triage_rule"], 0) + r["findings"]

        assert split == pooled
