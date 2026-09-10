"""Runner tests, centred on the one property sharding has to have.

Sharding exists because ``aggregate_rates`` materialises the whole filtered
payer table and then takes a distinct over every column, so its peak memory
scales with rows entering it. NYU Langone sends 15.1M rows into that call and
reached 58 GB of virtual memory on a 15.6 GB machine, taking the terminal with
it. Splitting the run by the code's leading character bounds that.

A memory fix that changes the answer is not a fix, so what is tested here is
that it does not: a sweep of shards must produce exactly what one pass produces.
That holds because every key is ``(code, code_type, carrier)`` and a shard is
defined by the code's first character, so no key spans two shards -- but "it
holds because of an argument" is what this file replaces.
"""

from __future__ import annotations

import pytest

from reconcile.comparability import ComparableRate
from reconcile.mart_cli import SHARDS, range_rows, summarise_range

CODES = [
    ("470", "MS-DRG"),
    ("477", "MS-DRG"),
    ("99213", "CPT"),
    ("J9225", "HCPCS"),
    ("J0130", "HCPCS"),
    ("Q5124", "HCPCS"),
    ("A0428", "HCPCS"),
]
FACILITIES = ("Tisch", "Brooklyn", "Queens")


def hospital_rates() -> list[ComparableRate]:
    """Three facilities per code, at spread-out prices so a range exists."""
    out = []
    for index, (code, code_type) in enumerate(CODES):
        for step, facility in enumerate(FACILITIES):
            out.append(
                ComparableRate(
                    source="hospital",
                    hospital=facility,
                    code=code,
                    code_type=code_type,
                    payer="Cigna",
                    rate_dollar=1_000.0 * (index + 1) * (1 + step * 0.4),
                )
            )
    return out


def payer_rates() -> list[ComparableRate]:
    """One payer rate per code, some inside the hospital range and some not."""
    return [
        ComparableRate(
            source="payer",
            hospital="",
            code=code,
            code_type=code_type,
            billing_class="facility",
            payer="Cigna",
            rate_dollar=1_000.0 * (index + 1) * (1.1 if index % 2 else 2.9),
        )
        for index, (code, code_type) in enumerate(CODES)
    ]


def shard_of(rate: ComparableRate) -> str:
    return rate.code[0]


class TestShardingIsExact:
    def test_a_sweep_of_shards_equals_one_pass(self):
        """The property the memory fix rests on."""
        hospital, payer = hospital_rates(), payer_rates()

        one_pass = range_rows(hospital, payer)
        swept = []
        for shard in SHARDS:
            swept.extend(
                range_rows(
                    [r for r in hospital if shard_of(r) == shard],
                    [r for r in payer if shard_of(r) == shard],
                )
            )

        assert len(swept) == len(one_pass)
        assert {(c.code, c.code_type, c.payer) for c in swept} == {
            (c.code, c.code_type, c.payer) for c in one_pass
        }
        assert summarise_range(swept) == summarise_range(one_pass)

    def test_the_fixture_actually_spans_several_shards(self):
        """Otherwise the test above passes by covering one shard and proves nothing."""
        assert len({code[0] for code, _ in CODES}) >= 3

    def test_the_fixture_produces_both_verdicts(self):
        """A sweep that agrees only because everything is INSIDE proves little."""
        summary = summarise_range(range_rows(hospital_rates(), payer_rates()))

        assert summary["inside"] > 0
        assert summary["above"] > 0

    def test_no_comparison_is_counted_twice(self):
        """A key landing in two shards would inflate every count downstream."""
        swept = [
            (c.code, c.code_type, c.payer)
            for shard in SHARDS
            for c in range_rows(
                [r for r in hospital_rates() if shard_of(r) == shard],
                [r for r in payer_rates() if shard_of(r) == shard],
            )
        ]

        assert len(swept) == len(set(swept))


class TestSummarise:
    def test_widest_is_re_sorted_across_shards(self):
        """Each shard arrives sorted within itself; concatenated shards are not."""
        rows = range_rows(hospital_rates(), payer_rates())
        shuffled = list(reversed(rows))

        assert summarise_range(shuffled)["widest"] == summarise_range(rows)["widest"]

    def test_empty_input_summarises_rather_than_raising(self):
        assert summarise_range([])["total"] == 0


class TestArgumentGuards:
    def test_all_shards_refuses_pairs_mode(self):
        """A variance mart carries cross-row state and does not combine."""
        from reconcile.mart_cli import main

        with pytest.raises(SystemExit):
            main(
                [
                    "--payer-root",
                    ".",
                    "--hospital",
                    "X",
                    "--mode",
                    "pairs",
                    "--all-shards",
                ]
            )

    def test_shard_and_all_shards_are_mutually_exclusive(self):
        from reconcile.mart_cli import main

        with pytest.raises(SystemExit):
            main(["--payer-root", ".", "--hospital", "X", "--shard", "J", "--all-shards"])
