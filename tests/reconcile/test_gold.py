"""Building the gold mart one shard at a time.

``mart_cli`` refuses ``--all-shards`` outside range mode because "a variance mart
carries cross-row state and its counts do not combine across shards". The state
is systematic-offset detection: it groups pairs by contract and asks whether one
constant ratio covers enough *distinct codes* to be one fact about two base rates
rather than hundreds of findings. Shards split on the code's first character, so
a contract is scattered across all of them.

That is not a small thing to lose. Collapsing offsets turned 2,943 unexplained
Mount Sinai pairs into 27, and chasing the four constants found a real join
defect. A sharded run that missed them would report hundreds of non-findings.

So the test that earns this module is ``test_sharding_changes_nothing``: the same
data, once whole and once split by leading character, must produce the same
residual, the same offsets and the same counts.
"""

from __future__ import annotations

import pytest

from reconcile.comparability import ComparableRate
from reconcile.gold import Reconciliation, reconcile_shard
from reconcile.variance import apply_systematic_offsets, cross_source_variance

SYSTEM = "Mount Sinai"
FACILITY = "The Mount Sinai Hospital"


def hospital(code: str, rate: float, facility: str = FACILITY) -> ComparableRate:
    return ComparableRate(
        source="hospital",
        hospital=facility,
        code=code,
        code_type="CPT",
        payer="Aetna",
        plan="Commercial PPO",
        product_class="commercial",
        billing_class="facility",
        rate_dollar=rate,
        vintage="2026-04-01",
    )


def payer(code: str, rate: float, facility: str = FACILITY) -> ComparableRate:
    return ComparableRate(
        source="payer",
        hospital=facility,
        code=code,
        code_type="CPT",
        payer="Aetna",
        plan="Commercial PPO",
        product_class="commercial",
        billing_class="facility",
        rate_dollar=rate,
        vintage="2026-04-01",
    )


def a_contract_with_a_constant_offset(
    ratio: float = 1.4, codes: int = 30
) -> tuple[list[ComparableRate], list[ComparableRate]]:
    """One contract where the payer pays a constant multiple across many codes.

    Codes are spread deliberately across leading characters 1-9, so an
    unsharded run sees one contract of 30 codes and a per-shard run would see
    nine contracts of three or four -- every one of them under ``min_codes``.
    """
    left, right = [], []
    for i in range(codes):
        code = f"{i % 9 + 1}{i:04d}"
        base = 100.0 + i
        left.append(hospital(code, base))
        right.append(payer(code, base * ratio))
    return left, right


def build_sharded(
    left: list[ComparableRate], right: list[ComparableRate], **kwargs: object
) -> Reconciliation:
    """Fold the same data in shard by shard, keyed on the code's first char."""
    run = Reconciliation(hospital=SYSTEM, system=SYSTEM, hospital_slug="mount-sinai")
    for shard in sorted({rate.code[:1] for rate in left} | {rate.code[:1] for rate in right}):
        shard_left = [r for r in left if r.code.startswith(shard)]
        shard_right = [r for r in right if r.code.startswith(shard)]
        if not shard_left or not shard_right:
            continue
        mart = reconcile_shard(shard_left, shard_right)
        run.add_shard(shard, mart, hospital_rates=len(shard_left), payer_rates=len(shard_right))
    run.close()
    return run


def build_whole(left: list[ComparableRate], right: list[ComparableRate]) -> Reconciliation:
    run = Reconciliation(hospital=SYSTEM, system=SYSTEM, hospital_slug="mount-sinai")
    run.add_shard(
        "", reconcile_shard(left, right), hospital_rates=len(left), payer_rates=len(right)
    )
    run.close()
    return run


class TestShardingIsInvisible:
    def test_sharding_changes_nothing(self):
        """The property the whole design rests on.

        Thirty codes at a constant 1.4x, spread across nine leading characters.
        Whole, that is one offset. Sharded, each shard sees three or four codes
        and the threshold is twenty -- so a naive per-shard run finds no offset
        and reports thirty findings that are one fact.
        """
        left, right = a_contract_with_a_constant_offset()

        whole = build_whole(left, right)
        sharded = build_sharded(left, right)

        assert len(sharded.offsets) == len(whole.offsets) == 1
        assert sharded.offsets[0].ratio == pytest.approx(whole.offsets[0].ratio)
        assert sharded.offsets[0].codes == whole.offsets[0].codes == 30
        assert len(sharded.residual) == len(whole.residual)
        assert sharded.explanation == whole.explanation
        assert sharded.pairs_formed == whole.pairs_formed

    def test_the_naive_per_shard_answer_would_have_been_wrong(self):
        """Proves the test above is testing something.

        Applying offsets per shard -- the obvious implementation -- finds
        nothing, because no shard holds twenty distinct codes of the contract.
        """
        left, right = a_contract_with_a_constant_offset()

        found_per_shard = 0
        for shard in sorted({r.code[:1] for r in left}):
            shard_left = [r for r in left if r.code.startswith(shard)]
            shard_right = [r for r in right if r.code.startswith(shard)]
            mart = cross_source_variance(shard_left, shard_right)
            found_per_shard += len(apply_systematic_offsets(mart))

        assert found_per_shard == 0, "if this ever finds one, the fixture stopped being a test"
        assert len(build_sharded(left, right).offsets) == 1

    def test_it_matches_the_unsharded_mart_the_cli_builds(self):
        """Against the existing code path, not just against itself."""
        left, right = a_contract_with_a_constant_offset()

        mart = cross_source_variance(left, right)
        offsets = apply_systematic_offsets(mart)
        sharded = build_sharded(left, right)

        assert len(sharded.offsets) == len(offsets)
        assert sharded.offsets[0].ratio == pytest.approx(offsets[0].ratio)
        assert len(sharded.residual) == len(mart.unexplained)


class TestTheResidual:
    def test_a_genuine_disagreement_survives_the_offset(self):
        """A straggler inside a contract that has an offset is still a finding."""
        left, right = a_contract_with_a_constant_offset()
        left.append(hospital("70001", 200.0))
        right.append(payer("70001", 900.0))

        run = build_sharded(left, right)

        assert len(run.residual) == 1
        assert run.residual[0].code == "70001"
        assert run.residual[0].payer_rate == 900.0

    def test_pairs_the_offset_explains_leave_the_residual(self):
        left, right = a_contract_with_a_constant_offset()

        run = build_sharded(left, right)

        assert run.residual == []
        assert run.explanation["systematic_offset"] == 30
        assert run.explanation.get("unexplained", 0) == 0

    def test_a_residual_row_carries_its_own_context(self):
        """It is read alone, in a report or a triage queue, with nothing joined."""
        left, right = a_contract_with_a_constant_offset()
        left.append(hospital("70001", 200.0))
        right.append(payer("70001", 900.0))

        row = build_sharded(left, right).residual[0]

        assert row.facility == FACILITY
        assert row.carrier == "Aetna"
        assert row.code_type == "CPT"
        assert row.hospital_rate == 200.0
        assert row.relative_difference == pytest.approx(3.5)
        assert row.hospital_vintage == "2026-04-01"
        assert row.contract == (FACILITY, "Aetna", "Commercial PPO", "Commercial PPO")


class TestTheAccounting:
    def test_the_breakdown_sums_to_the_pairs_formed(self):
        """A funnel whose parts do not add up is worse than no funnel."""
        left, right = a_contract_with_a_constant_offset()
        left.append(hospital("70001", 200.0))
        right.append(payer("70001", 900.0))

        run = build_sharded(left, right)

        counted = sum(v for k, v in run.explanation.items() if not k.startswith("_"))
        assert counted == run.pairs_formed

    def test_measures_are_long_format_and_include_the_funnel(self):
        left, right = a_contract_with_a_constant_offset()

        rows = build_sharded(left, right).measures()

        headline = {r["key"]: r["value"] for r in rows if r["measure"] == "headline"}
        assert headline["pairs_formed"] == 30
        assert headline["unexplained_and_material"] == 0
        assert headline["systematic_offsets"] == 1
        assert {r["measure"] for r in rows} >= {"headline", "explanation"}
        assert all(r["hospital_slug"] == "mount-sinai" for r in rows)

    def test_comparable_share_uses_the_full_denominator(self):
        left, right = a_contract_with_a_constant_offset(codes=20)
        # A pair the comparability layer refuses, so it is excluded not compared.
        left.append(hospital("80001", 50.0))
        right.append(
            ComparableRate(
                source="payer",
                hospital=FACILITY,
                code="80001",
                code_type="CPT",
                payer="Aetna",
                plan="Commercial PPO",
                product_class="commercial",
                billing_class="professional",
                rate_dollar=60.0,
                vintage="2026-04-01",
            )
        )

        run = build_sharded(left, right)

        assert sum(run.excluded.values()) >= 1
        assert run.comparable_share < 1.0
        assert run.comparable_share == pytest.approx(
            run.pairs_formed / (run.pairs_formed + sum(run.excluded.values()))
        )


class TestTheSummaryGrain:
    """The grain every filter in the report acts on.

    A page that filters by carrier against a table counted per system answers
    with the system's numbers and looks like it filtered, which is the failure
    this grain exists to prevent.
    """

    def test_outcomes_are_keyed_by_carrier_code_type_and_explanation(self):
        left, right = a_contract_with_a_constant_offset()
        left.append(hospital("70001", 200.0))
        right.append(payer("70001", 900.0))

        rows = build_sharded(left, right).outcome_rows()

        assert {r["carrier"] for r in rows} == {"Aetna"}
        assert {r["code_type"] for r in rows} == {"CPT"}
        assert {r["explanation"] for r in rows} == {"systematic_offset", "unexplained"}
        assert sum(r["pairs"] for r in rows) == 31

    def test_outcome_counts_follow_the_offset_reclassification(self):
        """The counts must move when close() moves them, or the page disagrees
        with the funnel it sits next to."""
        left, right = a_contract_with_a_constant_offset()

        rows = {r["explanation"]: r["pairs"] for r in build_sharded(left, right).outcome_rows()}

        assert rows == {"systematic_offset": 30}

    def test_outcomes_sum_to_the_explanation_breakdown(self):
        left, right = a_contract_with_a_constant_offset()
        left.append(hospital("70001", 200.0))
        right.append(payer("70001", 900.0))
        run = build_sharded(left, right)

        by_explanation: dict[str, int] = {}
        for row in run.outcome_rows():
            by_explanation[row["explanation"]] = (
                by_explanation.get(row["explanation"], 0) + row["pairs"]
            )

        assert by_explanation == {
            k: v for k, v in run.explanation.items() if not k.startswith("_") and v
        }

    def test_an_immaterial_unexplained_pair_is_reclassified_at_grain_too(self):
        """It never reaches the residual, so its carrier is only remembered here."""
        left, right = a_contract_with_a_constant_offset()
        # Inside the 1.4x band, so the offset explains it, but under the 5%
        # materiality threshold relative to the offset it is not a finding.
        left.append(hospital("70002", 100.0))
        right.append(payer("70002", 140.7))

        run = build_sharded(left, right)
        rows = {r["explanation"]: r["pairs"] for r in run.outcome_rows()}

        assert run.residual == []
        assert rows.get("unexplained", 0) == 0
        assert rows["systematic_offset"] == 31


class TestTheSummaryTables:
    def test_coverage_is_one_row_with_the_whole_funnel(self):
        left, right = a_contract_with_a_constant_offset()
        left.append(hospital("70001", 200.0))
        right.append(payer("70001", 900.0))

        row = build_sharded(left, right).coverage_row()

        assert row["pairs_formed"] == 31
        assert row["unexplained_and_material"] == 1
        assert row["systematic_offsets"] == 1
        assert row["carriers"] == 1
        assert row["candidates"] >= row["pairs_formed"]

    def test_magnitude_sizes_the_residual_rather_than_counting_it(self):
        left, right = a_contract_with_a_constant_offset()
        left.append(hospital("70001", 200.0))
        right.append(payer("70001", 900.0))

        rows = build_sharded(left, right).magnitude_rows()

        assert len(rows) == 1
        assert rows[0]["residual_pairs"] == 1
        assert rows[0]["median_relative_difference"] == pytest.approx(3.5)
        assert rows[0]["median_abs_difference_usd"] == pytest.approx(700.0)

    def test_exemplars_are_capped_per_carrier(self):
        """Bounded on purpose: it is the one row-level table in the dataset."""
        left, right = a_contract_with_a_constant_offset()
        for i in range(40):
            left.append(hospital(f"9{i:04d}", 100.0))
            right.append(payer(f"9{i:04d}", 100.0 * (5 + i)))

        rows = build_sharded(left, right).exemplar_rows(per_carrier=25)

        assert len(rows) == 25
        widest = [r["relative_difference"] for r in rows]
        assert widest == sorted(widest, reverse=True), "the widest disagreements first"

    def test_refusals_are_reported_at_system_grain(self):
        left, right = a_contract_with_a_constant_offset(codes=20)
        left.append(hospital("80001", 50.0))
        right.append(
            ComparableRate(
                source="payer",
                hospital=FACILITY,
                code="80001",
                code_type="CPT",
                payer="Aetna",
                plan="Commercial PPO",
                product_class="commercial",
                billing_class="professional",
                rate_dollar=60.0,
                vintage="2026-04-01",
            )
        )

        rows = build_sharded(left, right).refusal_rows()

        assert rows, "a refused candidate must be reported, not dropped"
        assert set(rows[0]) == {"hospital_slug", "system", "reason", "candidates"}
        assert "carrier" not in rows[0], "system grain; the page must say the filter is inactive"

    def test_every_table_refuses_to_report_before_close(self):
        run = Reconciliation(hospital=SYSTEM, system=SYSTEM, hospital_slug="mount-sinai")
        left, right = a_contract_with_a_constant_offset()
        run.add_shard("1", reconcile_shard(left, right))

        for reader in (
            run.outcome_rows,
            run.magnitude_rows,
            run.exemplar_rows,
            run.coverage_row,
            run.refusal_rows,
        ):
            with pytest.raises(RuntimeError, match="close"):
                reader()


class TestTheGuards:
    def test_a_shard_cannot_be_added_after_close(self):
        """The offsets are fixed at close; a late shard would not be in them."""
        left, right = a_contract_with_a_constant_offset()
        run = build_sharded(left, right)

        with pytest.raises(RuntimeError, match="already fixed"):
            run.add_shard("Z", reconcile_shard(left, right))

    def test_measures_refuse_to_report_before_close(self):
        """Counts read before close would be missing every offset."""
        run = Reconciliation(hospital=SYSTEM, system=SYSTEM, hospital_slug="mount-sinai")
        left, right = a_contract_with_a_constant_offset()
        run.add_shard("1", reconcile_shard(left, right))

        with pytest.raises(RuntimeError, match="close"):
            run.measures()
