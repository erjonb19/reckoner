"""A3 tests: which kind of nothing a file produced.

The gross-and-cash document below is copied in shape from the real Mount Sinai
Brooklyn file -- 82 MB, CMS template 3.0.0, 217,957 charge items, and not one
``payers_information`` entry in any of them. Working that out by hand took a full
download and six probes, and the whole point of this module is that it should
have taken a line in the audit.

The distinction under test is the one that matters: a file the parser could not
read needs an adapter, and a file with nothing in it to read needs nothing at
all. Both look identical from outside -- same status, same message, same zero.
"""

from __future__ import annotations

import json

import pytest

from hospital.conformance import Conformance, Observation, diagnose
from hospital.parser import MrfParser
from hospital.streaming import MrfStream

# Shape copied from the real file: code_information, standard_charges carrying
# gross_charge and discounted_cash, and no payers_information anywhere.
GROSS_AND_CASH_ONLY = {
    "hospital_name": "Mount Sinai Brooklyn",
    "last_updated_on": "2026-04-01",
    "version": "3.0.0",
    "standard_charge_information": [
        {
            "description": "PLATE IMPLANT RECONSTRUCTION 3.5MM 11HOLE 130MM L",
            "code_information": [{"code": "C1713", "type": "HCPCS"}],
            "standard_charges": [
                {
                    "gross_charge": "2187.85",
                    "discounted_cash": "1969.06",
                    "setting": "both",
                    "billing_class": "facility",
                    "additional_generic_notes": "Gross Charge Type: FS 870",
                }
            ],
        }
    ],
}

WITH_A_NEGOTIATED_RATE = {
    "hospital_name": "Somewhere General",
    "version": "2.0.0",
    "standard_charge_information": [
        {
            "description": "A service",
            "code_information": [{"code": "99213", "type": "CPT"}],
            "standard_charges": [
                {
                    "setting": "outpatient",
                    "billing_class": "professional",
                    "payers_information": [
                        {
                            "payer_name": "Aetna",
                            "plan_name": "PPO",
                            "standard_charge_dollar": "123.45",
                        }
                    ],
                }
            ],
        }
    ],
}

# Same data, but the array is under a name the parser does not look for. This is
# the adapter case, and it has never been observed in the real corpus.
UNRECOGNISED_CONTAINER = {
    "hospital_name": "Somewhere General",
    "charges_by_service": WITH_A_NEGOTIATED_RATE["standard_charge_information"],
}


def parse(document: dict) -> tuple[MrfParser, int]:
    """Parse a document fully, returning the parser and the rates it yielded."""
    data = json.dumps(document).encode()
    parser = MrfParser(MrfStream(data[i : i + 256] for i in range(0, len(data), 256)))
    return parser, sum(1 for _ in parser)


def observe(
    parser: MrfParser, yielded: int, rows_kept: int = 0, bytes_read: int = 0
) -> Observation:
    return Observation(
        layout=parser.meta.layout,
        structure_found=parser.structure_found,
        items_seen=parser.items_seen,
        rates_yielded=yielded,
        rows_kept=rows_kept,
        bytes_read=bytes_read,
    )


class TestTheDistinctionThatMatters:
    def test_gross_and_cash_only_is_not_a_parser_failure(self):
        """Mount Sinai Brooklyn: complete file, no negotiated rates, nothing to fix."""
        parser, yielded = parse(GROSS_AND_CASH_ONLY)

        assert parser.structure_found, "the container was found and walked"
        assert parser.items_seen == 1
        assert yielded == 0

        d = diagnose(observe(parser, yielded))

        assert d.conformance is Conformance.NO_NEGOTIATED_RATES
        assert not d.actionable
        assert "gross and cash" in d.reason

    def test_an_unrecognised_container_is_a_parser_failure(self):
        """Identical symptom from outside -- zero rows -- opposite response."""
        parser, yielded = parse(UNRECOGNISED_CONTAINER)

        assert not parser.structure_found
        assert parser.items_seen == 0

        d = diagnose(observe(parser, yielded, bytes_read=4096))

        assert d.conformance is Conformance.STRUCTURE_NOT_FOUND
        assert d.actionable
        assert "adapter" in d.reason or "does not recognise" in d.reason

    def test_the_two_are_indistinguishable_without_the_item_count(self):
        """Why the parser had to start counting items.

        Both files yield zero rates. Only ``items_seen`` separates them, which is
        the observation the ingest was not collecting.
        """
        empty, empty_yield = parse(GROSS_AND_CASH_ONLY)
        broken, broken_yield = parse(UNRECOGNISED_CONTAINER)

        assert empty_yield == broken_yield == 0
        assert empty.items_seen != broken.items_seen

    def test_a_file_with_rates_conforms(self):
        parser, yielded = parse(WITH_A_NEGOTIATED_RATE)

        assert yielded == 1
        assert diagnose(observe(parser, yielded, rows_kept=1)).conformance is Conformance.CONFORMS


class TestOrdering:
    def test_truncation_outranks_everything(self):
        """Any other verdict would be a claim about a fragment."""
        d = diagnose(
            Observation(
                layout="json",
                structure_found=False,
                items_seen=0,
                rates_yielded=0,
                rows_kept=0,
                truncated=True,
            )
        )

        assert d.conformance is Conformance.TRUNCATED

    def test_kept_rows_end_the_enquiry(self):
        d = diagnose(
            Observation(
                layout="json",
                structure_found=True,
                items_seen=5,
                rates_yielded=5,
                rows_kept=5,
                truncated=True,
            )
        )

        assert d.conformance is Conformance.CONFORMS

    def test_an_unknown_layout_needs_an_adapter(self):
        d = diagnose(
            Observation(
                layout="xml", structure_found=True, items_seen=0, rates_yielded=0, rows_kept=0
            )
        )

        assert d.conformance is Conformance.STRUCTURE_NOT_FOUND
        assert d.actionable


class TestRatesFoundButNoneKept:
    def test_everything_rejected_is_a_data_question_not_a_layout_one(self):
        d = diagnose(
            Observation(
                layout="csv-tall",
                structure_found=True,
                items_seen=0,
                rates_yielded=900,
                rows_kept=0,
                rows_rejected=900,
            )
        )

        assert d.conformance is Conformance.ALL_ROWS_REJECTED
        assert "reject reasons" in d.reason

    def test_rates_that_neither_landed_nor_were_rejected_go_to_review(self):
        """Filtered-out-of-scope only. No rule covers it, so a human sees it."""
        d = diagnose(
            Observation(
                layout="csv-tall",
                structure_found=True,
                items_seen=0,
                rates_yielded=900,
                rows_kept=0,
                rows_rejected=0,
            )
        )

        assert d.conformance is Conformance.UNKNOWN
        assert d.needs_review


@pytest.mark.parametrize("layout", ["json", "csv-tall", "csv-wide"])
def test_every_known_layout_can_reach_a_non_actionable_verdict(layout):
    """A conforming file must never be reported as needing work."""
    d = diagnose(
        Observation(layout=layout, structure_found=True, items_seen=1, rates_yielded=1, rows_kept=1)
    )

    assert not d.actionable
