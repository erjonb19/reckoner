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


class TestImplausibleGaps:
    """A 193x difference is not a negotiation, and must not be counted as one.

    The pairwise mart had this bound from the start; the range comparison
    shipped without it, so a real run reported
    ``G0399 / Aetna: hospitals $270-$270, payer $52,078`` as a disagreement and
    measured its agreement share against a denominator holding that.
    """

    def test_a_wildly_high_payer_rate_is_implausible_not_above(self):
        row = comparison(52_078.0, {"a": 270.0, "b": 270.0})

        assert row.verdict is RangeVerdict.IMPLAUSIBLE
        assert row.gap > 100

    def test_a_wildly_low_payer_rate_is_implausible_not_below(self):
        row = comparison(14.0, {"a": 425.0, "b": 512.0})

        assert row.verdict is RangeVerdict.IMPLAUSIBLE

    def test_an_ordinary_disagreement_is_untouched(self):
        assert comparison(5_000.0).verdict is RangeVerdict.BELOW
        assert comparison(30_000.0).verdict is RangeVerdict.ABOVE

    def test_the_bound_is_where_it_says_it_is(self):
        """Ten times exactly is implausible; just under it is a disagreement."""
        assert comparison(1_000.0, {"a": 10_000.0}).verdict is RangeVerdict.IMPLAUSIBLE
        assert comparison(1_010.0, {"a": 10_000.0}).verdict is RangeVerdict.BELOW

    def test_the_bound_is_configurable_per_comparison(self):
        strict = RangeComparison(
            code="470",
            code_type="MS-DRG",
            payer="Cigna",
            facility_rates={"a": 10_000.0},
            payer_rate=5_000.0,
            implausible_ratio=1.5,
        )

        assert strict.verdict is RangeVerdict.IMPLAUSIBLE

    def test_implausible_pairs_are_kept_out_of_every_share(self):
        """Counted, then excluded -- they are not evidence either way."""
        rows = [
            comparison(14_000.0),  # inside
            comparison(5_000.0),  # below
            comparison(52_078.0, {"a": 270.0, "b": 270.0}),  # implausible
        ]
        out = summarise(rows)

        assert out["total"] == 3
        assert out["comparable"] == 2
        assert out["implausible_excluded"] == 1
        # One inside of two comparable, not one of three.
        assert out["inside_share"] == pytest.approx(0.5)

    def test_a_set_of_only_implausible_pairs_does_not_divide_by_zero(self):
        out = summarise([comparison(52_078.0, {"a": 270.0, "b": 270.0})])

        assert out["comparable"] == 0
        assert out["inside_share"] == 0.0
        assert out["median_range_width"] == 0.0
