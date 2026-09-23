"""The facility-grain gold tables the analyst app reads.

Each test is an invariant a reader of the page relies on without knowing it:
that the rankings' counts add up to the coverage headline, that every hospital
rate lands in exactly one pair, that code lookup shows a hospital's rate even
where nothing could be compared, and that none of it changes what was already
published.
"""

from __future__ import annotations

from dataclasses import replace

import pyarrow as pa
import pytest

import storage
from pipeline import mart
from pipeline.mart import gold_manifest
from reconcile.comparability import ComparableRate
from reconcile.gold import Reconciliation, stream_shard

QUEENS = "Mount Sinai Queens"
BROOKLYN = "Mount Sinai Brooklyn"


def hospital(code: str, rate: float, facility: str = QUEENS, **kw: object) -> ComparableRate:
    base = ComparableRate(
        source="hospital",
        hospital=facility,
        code=code,
        code_type="CPT",
        payer="Aetna",
        plan="Aetna PPO",
        product_class="commercial",
        billing_class="facility",
        setting="outpatient",
        rate_dollar=rate,
        vintage="2026-04-01",
    )
    return replace(base, **kw)  # type: ignore[arg-type]


def payer(code: str, rate: float, facility: str = QUEENS, **kw: object) -> ComparableRate:
    base = ComparableRate(
        source="payer",
        hospital=facility,
        code=code,
        code_type="CPT",
        payer="Aetna",
        plan="Ppo",
        product_class="commercial",
        billing_class="facility",
        setting="outpatient",
        rate_dollar=rate,
        vintage="2026-09-01",
    )
    return replace(base, **kw)  # type: ignore[arg-type]


def build(slices: list[tuple[list[ComparableRate], list[ComparableRate]]]) -> Reconciliation:
    """As the mart does it: one add_shard per facility slice, hooks attached."""
    run = Reconciliation(
        hospital="Mount Sinai Health System", system="Mount Sinai", hospital_slug="ms"
    )
    for i, (left, right) in enumerate(slices):
        m, rows = stream_shard(left, right, on_refusal=run.record_refusal)
        run.add_shard(
            str(i),
            m,
            hospital_rates=len(left),
            payer_rates=len(right),
            rows=rows,
            payer_carriers=frozenset(r.payer for r in right),
        )
    run.close()
    return run


def a_system() -> Reconciliation:
    queens = (
        [
            hospital("10021", 100.0),  # 3x below the payer: unexplained, material
            hospital("10022", 500.0),  # inside the payer's range
            hospital("10023", 300.0, product_class="medicare_advantage"),  # TiC-exempt
            hospital("10024", 250.0),  # counterpart only professional
            hospital("10025", 80.0, payer="Healthfirst"),  # carrier outside the corpus
        ],
        [
            payer("10021", 300.0),
            payer("10021", 320.0, plan="Epo"),
            payer("10022", 400.0),
            payer("10022", 600.0, plan="Epo"),
            payer("10024", 90.0, billing_class="professional"),
        ],
    )
    brooklyn = (
        [hospital("10021", 900.0, facility=BROOKLYN)],
        [payer("10021", 300.0, facility=BROOKLYN)],
    )
    return build([queens, brooklyn])


class TestPairs:
    def test_compared_sums_to_the_coverage_headline(self):
        run = a_system()

        assert sum(r["compared"] for r in run.pair_rows()) == run.coverage_row()["pairs_formed"]

    def test_every_hospital_rate_lands_in_exactly_one_pair(self):
        run = a_system()

        assert sum(r["hospital_rates"] for r in run.pair_rows()) == run.coverage_row()["candidates"]

    def test_a_pair_reports_its_own_figures(self):
        pairs = {(r["facility"], r["carrier"]): r for r in a_system().pair_rows()}
        queens = pairs[(QUEENS, "Aetna")]

        assert queens["compared"] == 2
        assert queens["hospital_rates"] == 4, "the Healthfirst rate is its own pair"
        assert queens["unexplained_material"] == 1
        assert queens["inside_range_share"] == pytest.approx(0.5)
        assert queens["like_class_share"] == pytest.approx(2 / 3), "4 rates less 1 other-class"

    def test_the_gap_is_signed_payer_over_hospital(self):
        pairs = {(r["facility"], r["carrier"]): r for r in a_system().pair_rows()}

        assert pairs[(BROOKLYN, "Aetna")]["median_signed_gap"] == pytest.approx(300 / 900 - 1)

    def test_outcomes_carry_the_facility_and_still_sum(self):
        run = a_system()
        rows = run.outcome_rows()

        assert {r["facility"] for r in rows} == {QUEENS, BROOKLYN}
        assert sum(r["pairs"] for r in rows) == run.pairs_formed

    def test_refusals_carry_the_facility(self):
        rows = a_system().refusal_rows()

        assert {r["facility"] for r in rows} == {QUEENS}
        assert {r["reason"] for r in rows} >= {"tic_exempt_product", "different_billing_class"}


class TestResidual:
    def test_it_is_capped_per_pair_and_ordered_by_dollar_gap(self):
        left = [hospital(f"1{i:04d}", 100.0 + i) for i in range(30)]
        # Ratios from 2x to 4.9x: material, below the 10x implausibility line, and
        # too varied to collapse into one systematic offset.
        right = [payer(f"1{i:04d}", (100.0 + i) * (2 + 0.1 * i)) for i in range(30)]
        run = build([(left, right)])

        rows = run.residual_rows(per_pair=5)

        assert len(rows) == 5
        gaps = [abs(r["difference"]) for r in rows]
        assert gaps == sorted(gaps, reverse=True)

    def test_the_exemplars_are_unchanged_beside_it(self):
        """A1's queue is built from exemplars and is being labelled."""
        run = a_system()

        assert [r["code"] for r in run.exemplar_rows()] == ["10021", "10021"]


class TestRates:
    def rows(self, run: Reconciliation) -> dict[tuple[str, str], dict[str, object]]:
        return {(r["facility"], r["code"]): r for r in run.rate_table().to_pylist()}

    def test_one_row_per_facility_carrier_code(self):
        run = a_system()
        table = run.rate_table()

        keys = list(
            zip(
                *(table.column(c).to_pylist() for c in ("facility", "carrier", "code")), strict=True
            )
        )
        assert len(keys) == len(set(keys))

    def test_a_compared_code_carries_the_payers_distribution(self):
        row = self.rows(a_system())[(QUEENS, "10022")]

        assert (row["hospital_rate"], row["payer_min"], row["payer_median"], row["payer_max"]) == (
            500.0,
            400.0,
            500.0,
            600.0,
        )
        assert row["inside_share"] == 1.0
        assert row["explanation_before_offsets"] == "within_payer_range"
        assert row["refusal"] == ""

    def test_a_refused_code_still_shows_the_hospitals_rate_and_why(self):
        rows = self.rows(a_system())

        exempt = rows[(QUEENS, "10023")]
        assert exempt["hospital_rate"] == 300.0
        assert exempt["compared"] == 0
        assert exempt["refusal"] == "tic_exempt_product"
        assert exempt["payer_median"] is None
        assert rows[(QUEENS, "10024")]["refusal"] == "different_billing_class"

    def test_only_carriers_the_payer_side_holds(self):
        """Healthfirst has no payer file; there is nothing to look up against."""
        carriers = set(a_system().rate_table().column("carrier").to_pylist())

        assert carriers == {"Aetna"}


class TestCodes:
    def test_the_most_common_description_wins_and_only_three_are_kept(self):
        run = Reconciliation(hospital="x", system="x", hospital_slug="x")
        run.add_descriptions(
            pa.table(
                {
                    "code_type": ["CPT"] * 5,
                    "code": ["70450"] * 5,
                    "description": [
                        "CT HEAD W/O",
                        "CT HEAD WO CONTRAST",
                        "ct head",
                        "CT BRAIN",
                        None,
                    ],
                    "rows": [40, 12, 3, 1, 99],
                }
            )
        )
        run.close()

        (row,) = run.code_rows()
        assert row["description"] == "CT HEAD W/O"
        assert len(run.descriptions[("CPT", "70450")]) == 3


class TestWriting:
    def test_rates_write_as_arrow_and_verify(self, tmp_path):
        run = a_system()
        built = mart.tables([run])

        written = mart.write(storage.local(tmp_path), built, systems={"ms"})
        manifest = gold_manifest(storage.local(tmp_path), written, {"ms"})

        assert written["rates"] == run.rate_table().num_rows > 0
        assert manifest["verified"] is True
