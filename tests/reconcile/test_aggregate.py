"""Exact per-group medians, replacing a t-digest per group.

The replacement exists for memory, measured in the container. What these tests
hold is that it is a correct median: against the standard library on real-
shaped groups, with the cases a digest and a sort disagree about -- even
counts, nulls, a group of one, a group with nothing in it.
"""

from __future__ import annotations

import random
from statistics import median

import pyarrow as pa
import pytest

from reconcile.aggregate import list_medians, median_by_group


def medians_of(lists: list[list[float | None]]) -> list[float | None]:
    return list_medians(pa.array(lists, type=pa.list_(pa.float64()))).to_pylist()


class TestListMedians:
    def test_odd_and_even_counts(self):
        assert medians_of([[3.0, 1.0, 2.0], [4.0, 1.0, 3.0, 2.0]]) == [2.0, 2.5]

    def test_a_single_value_is_its_own_median(self):
        assert medians_of([[42.5]]) == [42.5]

    def test_nulls_are_ignored_as_the_digest_ignored_them(self):
        assert medians_of([[None, 10.0, 30.0]]) == [20.0]

    def test_a_group_with_no_values_is_null_not_zero(self):
        """Zero is a price; the converter skips a null and would keep a zero."""
        assert medians_of([[None, None], [], [5.0]]) == [None, None, 5.0]

    def test_it_agrees_with_the_standard_library(self):
        rng = random.Random(7)
        groups = [
            [round(rng.uniform(1, 5_000), 2) for _ in range(rng.randint(1, 40))] for _ in range(500)
        ]

        assert medians_of(groups) == pytest.approx([median(g) for g in groups])

    def test_chunked_input(self):
        chunked = pa.chunked_array(
            [
                pa.array([[1.0, 3.0]], pa.list_(pa.float64())),
                pa.array([[5.0]], pa.list_(pa.float64())),
            ]
        )

        assert list_medians(chunked).to_pylist() == [2.0, 5.0]


class TestMedianByGroup:
    def test_it_is_a_drop_in_for_the_old_aggregate(self):
        table = pa.table(
            {
                "code": ["10021", "10021", "10021", "15271"],
                "plan": ["PPO", "PPO", "HMO", "PPO"],
                "rate_dollar": [100.0, 140.0, 90.0, None],
                "methodology": ["fee schedule", "case rate", "fee schedule", "fee schedule"],
            }
        )

        got = median_by_group(table, ["code", "plan"], "rate_dollar", [("methodology", "min")])
        rows = {(r["code"], r["plan"]): r for r in got.to_pylist()}

        assert set(got.column_names) == {
            "code",
            "plan",
            "rate_dollar_median",
            "rate_dollar_count",
            "methodology_min",
        }
        assert rows[("10021", "PPO")]["rate_dollar_median"] == 120.0
        assert rows[("10021", "PPO")]["rate_dollar_count"] == 2
        assert rows[("10021", "PPO")]["methodology_min"] == "case rate"
        assert rows[("10021", "HMO")]["rate_dollar_median"] == 90.0
        assert rows[("15271", "PPO")]["rate_dollar_median"] is None
        assert rows[("15271", "PPO")]["rate_dollar_count"] == 0

    def test_group_order_does_not_matter(self):
        """Medians are matched to their group, not to a position."""
        rng = random.Random(3)
        rows = [
            {"k": f"g{rng.randint(0, 50)}", "v": round(rng.uniform(1, 100), 2)}
            for _ in range(2_000)
        ]
        expected: dict[str, list[float]] = {}
        for row in rows:
            expected.setdefault(row["k"], []).append(row["v"])

        got = median_by_group(pa.Table.from_pylist(rows), ["k"], "v")

        for row in got.to_pylist():
            assert row["v_median"] == pytest.approx(median(expected[row["k"]]))
