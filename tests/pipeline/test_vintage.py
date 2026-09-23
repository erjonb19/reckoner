"""How far apart the two sides are, and what the report may claim about it.

CLAUDE.md calls vintage mismatch structural. The mart already refuses a pair
beyond the limit; what this adds is the distribution, because "some pairs are
refused for vintage" and "the median pair is nine months apart" are the same
sentence about very different projects.
"""

from __future__ import annotations

from typing import Any

from pipeline.vintage import MAX_VINTAGE_DAYS, alignment, gap_days, summarise


def pair(hospital: str = "2026-04-01", payer: str = "2026-04-15", **extra: str) -> dict[str, Any]:
    row: dict[str, Any] = {
        "hospital_slug": "mount-sinai",
        "system": "Mount Sinai",
        "carrier": "Aetna",
        "hospital_vintage": hospital,
        "payer_vintage": payer,
    }
    row.update(extra)
    return row


class TestTheGap:
    def test_it_is_the_absolute_difference(self):
        assert gap_days(pair("2026-04-01", "2026-04-15")) == 14
        assert gap_days(pair("2026-04-15", "2026-04-01")) == 14

    def test_a_missing_vintage_is_unknown_not_zero(self):
        """Zero would sort the least-aligned pair to the top of the table."""
        assert gap_days(pair(payer="")) is None
        assert gap_days(pair(hospital="")) is None

    def test_an_unparseable_vintage_is_unknown(self):
        assert gap_days(pair(payer="not a date")) is None

    def test_a_timestamp_is_read_as_its_date(self):
        """Silver publish times carry a time; the hospital side does not."""
        assert gap_days(pair("2026-04-01", "2026-04-11T03:50:29+00:00")) == 10


class TestAlignment:
    def test_one_row_per_hospital_and_carrier(self):
        rows = [pair(), pair(carrier="UHC"), pair(hospital_slug="nyp", system="NYP")]

        out = alignment(rows)

        assert len(out) == 3
        assert {r["carrier"] for r in out} == {"Aetna", "UHC"}

    def test_it_reports_the_spread_not_just_a_median(self):
        rows = [pair(payer=f"2026-04-{day:02d}") for day in (2, 5, 10, 20, 28)]

        out = alignment(rows)[0]

        assert out["min_gap_days"] == 1
        assert out["max_gap_days"] == 27
        assert out["median_gap_days"] == 9
        assert out["pairs"] == 5

    def test_unknown_vintages_are_counted_separately(self):
        """They are neither aligned nor misaligned; they are unmeasured."""
        out = alignment([pair(), pair(payer=""), pair(payer="")])[0]

        assert out["pairs"] == 3
        assert out["unknown_vintage_pairs"] == 2
        assert out["median_gap_days"] == 14, "the known pair still sets the median"

    def test_a_group_with_no_known_gap_reports_blank_not_zero(self):
        out = alignment([pair(payer=""), pair(payer="")])[0]

        assert out["median_gap_days"] == ""
        assert out["unknown_vintage_pairs"] == 2

    def test_beyond_the_limit_is_normally_zero_and_that_is_the_point(self):
        """The mart refuses those pairs, so a non-zero count means a leak."""
        out = alignment([pair(payer="2026-04-15")])[0]

        assert out["beyond_limit"] == 0
        assert out["max_vintage_days"] == MAX_VINTAGE_DAYS

    def test_a_pair_past_the_limit_is_flagged_if_one_ever_arrives(self):
        out = alignment([pair("2024-01-01", "2026-04-01")])[0]

        assert out["beyond_limit"] == 1


class TestTheSummary:
    def test_it_gives_the_headline(self):
        rows = [pair(payer=f"2026-04-{day:02d}") for day in (2, 5, 10, 20, 28)]

        got = summarise(rows)

        assert got["pairs"] == 5
        assert got["median_gap_days"] == 9
        assert got["max_gap_days"] == 27

    def test_an_empty_input_is_none_rather_than_zero(self):
        """Zero days apart is a claim; no pairs is the absence of one."""
        got = summarise([])

        assert got["pairs"] == 0
        assert got["median_gap_days"] is None
