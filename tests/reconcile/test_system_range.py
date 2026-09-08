"""Range-comparison tests.

The failure this replaces was silent and large: attributing one system-level
payer rate to each facility in turn and differencing produced 1,458 pairs at a
constant 1.298x and 1,458 more at 1.410x, which were two hospitals measured
against the same figure rather than any disagreement about price.
"""

import pytest

from reconcile.system_range import (
    RangeComparison,
    RangeVerdict,
    compare_to_system_range,
    summarise,
)

KEY = ("470", "MS-DRG", "Cigna")
FACILITIES = {"Queens": 10_000.0, "Brooklyn": 14_000.0, "Tisch": 20_000.0}


def comparison(payer_rate: float, rates: dict[str, float] | None = None) -> RangeComparison:
    return RangeComparison(
        code="470",
        code_type="MS-DRG",
        payer="Cigna",
        facility_rates=dict(rates or FACILITIES),
        payer_rate=payer_rate,
    )


class TestVerdict:
    def test_inside_the_range(self):
        row = comparison(14_000.0)

        assert row.verdict is RangeVerdict.INSIDE
        assert row.gap == 1.0

    def test_at_an_endpoint_counts_as_inside(self):
        assert comparison(10_000.0).verdict is RangeVerdict.INSIDE
        assert comparison(20_000.0).verdict is RangeVerdict.INSIDE

    def test_below_every_hospital(self):
        row = comparison(5_000.0)

        assert row.verdict is RangeVerdict.BELOW
        assert row.gap == pytest.approx(2.0)

    def test_above_every_hospital(self):
        row = comparison(30_000.0)

        assert row.verdict is RangeVerdict.ABOVE
        assert row.gap == pytest.approx(1.5)


class TestReadingTheVerdict:
    def test_width_is_reported_because_inside_can_be_cheap(self):
        """A system spanning 8k to 30k is easy to land inside."""
        wide = comparison(14_000.0, {"a": 8_000.0, "b": 30_000.0})
        narrow = comparison(14_000.0, {"a": 13_800.0, "b": 14_200.0})

        assert wide.verdict is narrow.verdict is RangeVerdict.INSIDE
        assert wide.width > narrow.width
        assert narrow.width == pytest.approx(1.029, abs=0.001)

    def test_vs_median_does_not_depend_on_the_range_width(self):
        """So it stays meaningful where the verdict is flattered by a wide spread."""
        row = comparison(28_000.0, {"a": 8_000.0, "b": 14_000.0, "c": 30_000.0})

        assert row.verdict is RangeVerdict.INSIDE
        assert row.vs_median == pytest.approx(2.0)

    def test_a_point_is_not_a_range(self):
        """One hospital cannot support an inside-or-outside claim."""
        assert compare_to_system_range({KEY: {"Queens": 10_000.0}}, {KEY: [12_000.0]}) == []

    def test_two_hospitals_are_enough(self):
        assert len(compare_to_system_range({KEY: FACILITIES}, {KEY: [12_000.0]})) == 1


class TestPairing:
    def test_only_services_both_sides_publish_are_compared(self):
        hospital = {KEY: FACILITIES, ("871", "MS-DRG", "Cigna"): FACILITIES}
        rows = compare_to_system_range(hospital, {KEY: [12_000.0]})

        assert [r.code for r in rows] == ["470"]

    def test_a_carrier_mismatch_is_not_a_pair(self):
        assert compare_to_system_range({KEY: FACILITIES}, {("470", "MS-DRG", "Aetna"): [1.0]}) == []

    def test_the_payer_median_is_used_not_a_single_row(self):
        """A payer publishes many rows per service; one outlier must not decide."""
        rows = compare_to_system_range({KEY: FACILITIES}, {KEY: [13_000.0, 14_000.0, 99_000.0]})

        assert rows[0].payer_rate == pytest.approx(14_000.0)
        assert rows[0].payer_rows == 3
        assert rows[0].verdict is RangeVerdict.INSIDE

    def test_one_comparison_per_service_not_one_per_facility(self):
        """The whole point: seven hospitals make one comparison, not seven."""
        rows = compare_to_system_range(
            {KEY: {"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0, "e": 5.0, "f": 6.0, "g": 7.0}},
            {KEY: [4.0]},
        )

        assert len(rows) == 1
        assert len(rows[0].facility_rates) == 7


class TestSummary:
    def test_headline_counts(self):
        rows = [comparison(14_000.0), comparison(5_000.0), comparison(30_000.0)]
        out = summarise(rows)

        assert out["total"] == 3
        assert out["inside"] == out["below"] == out["above"] == 1
        assert out["inside_share"] == pytest.approx(1 / 3)

    def test_an_empty_set_does_not_divide_by_zero(self):
        assert summarise([]) == {"total": 0}
