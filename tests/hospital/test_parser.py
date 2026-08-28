import json

from hospital.parser import MrfParser
from hospital.streaming import MrfStream

from .test_profile import TALL_CSV, WIDE_CSV


def parser_for(text: str) -> MrfParser:
    data = text.encode("utf-8")
    return MrfParser(MrfStream(data[i : i + 256] for i in range(0, len(data), 256)))


JSON_MRF = json.dumps(
    {
        "hospital_name": "Example Hospital",
        "last_updated_on": "2026-04-01",
        "version": "3.0.0",
        "location_name": ["Example Hospital Main"],
        "license_information": {"license_number": "1234H", "state": "NY"},
        "standard_charge_information": [
            {
                "description": "CT Scan",
                "code_information": [{"code": "70450", "type": "CPT"}],
                "standard_charges": [
                    {
                        "setting": "outpatient",
                        "billing_class": "facility",
                        "gross_charge": 2187.85,
                        "discounted_cash": 1969.06,
                        "payers_information": [
                            {
                                "payer_name": "Aetna",
                                "plan_name": "All Commercial Plans",
                                "standard_charge_dollar": 412.55,
                                "methodology": "fee schedule",
                            },
                            {
                                "payer_name": "Humana",
                                "plan_name": "Medicare Managed Care Plan",
                                "standard_charge_algorithm": "per contract",
                                "methodology": "other",
                            },
                        ],
                    }
                ],
            }
        ],
    }
)


class TestJson:
    def test_metadata_is_read_from_the_header(self):
        parser = parser_for(JSON_MRF)
        list(parser)

        assert parser.meta.hospital_name == "Example Hospital"
        assert parser.meta.last_updated_on == "2026-04-01"
        assert parser.meta.location_name == "Example Hospital Main"
        assert parser.meta.license_number == "1234H"
        assert parser.meta.has_vintage

    def test_one_row_per_payer(self):
        rows = list(parser_for(JSON_MRF))

        assert len(rows) == 2
        assert rows[0].payer_name == "Aetna"
        assert rows[0].code == "70450"
        assert rows[0].code_type == "CPT"
        assert rows[0].setting == "outpatient"
        assert rows[0].rate_dollar == "412.55"
        assert rows[0].gross_charge == "2187.85"
        assert rows[1].rate_algorithm == "per contract"

    def test_bom_prefixed_json_still_parses(self):
        rows = list(parser_for("﻿" + JSON_MRF))

        assert len(rows) == 2

    def test_ordinals_are_sequential(self):
        rows = list(parser_for(JSON_MRF))

        assert [r.ordinal for r in rows] == [1, 2]


class TestCsv:
    def test_tall_layout(self):
        parser = parser_for(TALL_CSV)
        rows = list(parser)

        assert parser.meta.layout == "csv-tall"
        assert parser.meta.hospital_name == "Example Hospital"
        assert parser.meta.last_updated_on == "2026-04-01"
        assert len(rows) == 4
        assert rows[0].payer_name == "Aetna"
        assert rows[0].rate_dollar == "412.55"
        assert rows[0].methodology == "fee schedule"
        assert rows[1].rate_algorithm == "per contract 12.4"

    def test_wide_layout_fans_one_row_into_several_rates(self):
        parser = parser_for(WIDE_CSV)
        rows = list(parser)

        assert parser.meta.layout == "csv-wide"
        # Row 1 has two populated payer columns, row 2 has two.
        assert len(rows) == 4
        assert {r.payer_name for r in rows} == {"Aetna", "Cigna"}
        assert any(r.plan_name == "Medicare Advantage" and r.rate_dollar == "388.00" for r in rows)
        assert any(r.rate_algorithm == "per contract" for r in rows)

    def test_blank_rows_are_skipped(self):
        rows = list(parser_for(TALL_CSV + "\n\n"))

        assert len(rows) == 4

    def test_file_without_a_header_yields_nothing(self):
        rows = list(parser_for("just,some,csv\n1,2,3\n"))

        assert rows == []


MULTI_CODE_MRF = json.dumps(
    {
        "hospital_name": "Example Hospital",
        "last_updated_on": "2026-04-01",
        "standard_charge_information": [
            {
                "description": "Pacemaker insertion",
                "code_information": [
                    {"code": "0000065", "type": "CDM"},
                    {"code": "0360", "type": "RC"},
                    {"code": "33206", "type": "CPT"},
                ],
                "standard_charges": [
                    {
                        "setting": "outpatient",
                        "payers_information": [
                            {
                                "payer_name": "Aetna",
                                "plan_name": "Commercial",
                                "standard_charge_dollar": 5000.0,
                            }
                        ],
                    }
                ],
            }
        ],
    }
)


def test_every_code_on_the_row_is_captured():
    """A service defined as "revenue code X with CPT Y" needs both to survive."""
    rows = list(parser_for(MULTI_CODE_MRF))

    assert len(rows) == 1
    assert rows[0].codes == (("0000065", "CDM"), ("0360", "RC"), ("33206", "CPT"))
    assert rows[0].code == "0000065"


def test_curate_breaks_codes_out_by_family():
    from hospital.curate import CurateContext, CuratedRate, curate
    from hospital.parser import FileMeta

    raw = next(iter(parser_for(MULTI_CODE_MRF)))
    meta = FileMeta(hospital_name="H", last_updated_on="2026-04-01")
    result = curate(raw, CurateContext("b", "u", "H", meta))

    assert isinstance(result, CuratedRate)
    assert result.revenue_code == "0360"
    assert result.procedure_code == "33206"
    assert result.drg_code is None
    assert '["0360","RC"]' in result.all_codes


def test_revenue_code_leading_zeros_are_preserved():
    """Revenue code 0470 and MS-DRG 470 must not collapse into each other."""
    from hospital.curate import CurateContext, CuratedRate, curate
    from hospital.parser import FileMeta

    raw = next(iter(parser_for(MULTI_CODE_MRF.replace('"0360"', '"0470"'))))
    result = curate(raw, CurateContext("b", "u", "H", FileMeta(last_updated_on="2026-04-01")))

    assert isinstance(result, CuratedRate)
    assert result.revenue_code == "0470"


def test_truncated_json_yields_what_it_parsed_and_flags_it():
    """A capped read ends mid-document; rows already parsed are still valid."""
    data = MULTI_CODE_MRF.encode("utf-8")
    parser = MrfParser(MrfStream((data[i : i + 128] for i in range(0, len(data), 128)), 400))
    rows = list(parser)

    assert parser.truncated
    assert isinstance(rows, list)


def test_complete_json_is_not_flagged_truncated():
    parser = parser_for(MULTI_CODE_MRF)
    list(parser)

    assert not parser.truncated
