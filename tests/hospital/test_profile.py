import json

import pytest

from hospital.profile import HOSPITAL_ONLY_CLASSES, classify_product, profile_stream
from hospital.streaming import MrfStream


def stream_of(text: str, max_bytes: int | None = None) -> MrfStream:
    data = text.encode("utf-8")
    return MrfStream((data[i : i + 128] for i in range(0, len(data), 128)), max_bytes)


class TestClassification:
    @pytest.mark.parametrize(
        ("payer", "plan", "expected"),
        [
            ("Aetna", "Medicare Managed Care Plan", "medicare_advantage"),
            ("ANTHEM", "MEDICARE ADVANTAGE", "medicare_advantage"),
            ("VNSNY", "Medicare SNP", "medicare_advantage"),
            ("Healthfirst", "Managed Medicaid", "medicaid_managed"),
            ("Fidelis", "CHP", "medicaid_managed"),
            ("Healthfirst", "HARP", "medicaid_managed"),
            ("Fidelis", "Essential Plan 1-4_200-250", "essential_plan"),
            ("Elderplan", "MAP_Medicare Advantage_MLTC", "dual_or_ltc"),
            ("Centerlight", "PACE", "dual_or_ltc"),
            ("MetroPlus", "EXCHANGE", "exchange_individual"),
            ("Cigna", "All Commercial Plans", "commercial_aggregate"),
            ("ANTHEM", "Blue Access Large Group", "commercial"),
            ("VACCN", "All Products", "ambiguous_all_products"),
            ("Local 1199", "", "no_plan_detail"),
            ("Aetna", "Aetna", "no_plan_detail"),
            ("Northwell Direct", "Northwell Direct", "no_plan_detail"),
            ("Fidelis", "Fidelis - Essential 1&amp;2", "essential_plan"),
            ("Affinity", "Affinity Health Plan - MCD", "medicaid_managed"),
            ("WellCare", "WellCare MCR", "medicare_advantage"),
            ("HIGHMARK BCBS [5143]", "HIGHMARK BCBS [514301]", "no_plan_detail"),
            ("Self-Pay", "Self-Pay", "non_payer"),
            ("Non-Contracted", "Non-Contracted", "non_payer"),
            ("ANTHEM", "INDEMNITY", "commercial"),
        ],
    )
    def test_real_payer_plan_strings(self, payer, plan, expected):
        """Every case here is a string observed in a real NY hospital MRF."""
        assert classify_product(payer, plan) == expected

    def test_dual_products_outrank_plain_medicare(self):
        """A narrower rule must win: MAP/MLTC is not plain Medicare Advantage."""
        assert classify_product("Elderplan", "MAP_Medicare Advantage_MLTC") != "medicare_advantage"

    def test_tic_exempt_products_are_flagged_hospital_only(self):
        for plan in ("Medicare Advantage", "Managed Medicaid", "PACE", "Essential Plan 1-2"):
            assert classify_product("X", plan) in HOSPITAL_ONLY_CLASSES

    def test_html_entities_are_unescaped_before_matching(self):
        """Publishers round-trip names through web pages: "Essential 1&amp;2"."""
        from hospital.profile import normalise_name

        assert normalise_name("Fidelis - Essential 1&amp;2") == "Fidelis Essential 1 2"

    def test_internal_payer_codes_are_stripped(self):
        from hospital.profile import normalise_name

        assert normalise_name("AETNA [2700]").strip() == "AETNA"

    def test_commercial_is_not_flagged_hospital_only(self):
        assert classify_product("ANTHEM", "Blue Access Large Group") not in HOSPITAL_ONLY_CLASSES


TALL_CSV = """\
hospital_name,last_updated_on,version
Example Hospital,2026-04-01,3.0.0
description,code|1,payer_name,plan_name,standard_charge|negotiated_dollar,standard_charge|negotiated_algorithm,standard_charge|negotiated_percentage,standard_charge|methodology
CT Scan,70450,Aetna,All Commercial Plans,412.55,,,fee schedule
CT Scan,70450,Aetna,Medicare Managed Care Plan,,per contract 12.4,,other
CT Scan,70450,Healthfirst,Managed Medicaid,101.10,,,fee schedule
MRI,70551,Cigna,All Commercial Plans,,,45,percent of charges
"""

_WIDE_HEADER = ",".join(
    [
        "description",
        "code|1",
        "standard_charge|Aetna|All Commercial Plans|negotiated_dollar",
        "standard_charge|Aetna|Medicare Advantage|negotiated_dollar",
        "standard_charge|Cigna|All Commercial Plans|negotiated_algorithm",
    ]
)

WIDE_CSV = "\n".join(
    [
        "hospital_name,last_updated_on",
        "Example Hospital,2026-04-01",
        _WIDE_HEADER,
        "CT Scan,70450,412.55,388.00,",
        "MRI,70551,900.00,,per contract",
        "",
    ]
)

JSON_MRF = json.dumps(
    {
        "hospital_name": "Example Hospital",
        "version": "3.0.0",
        "standard_charge_information": [
            {
                "description": "CT Scan",
                "standard_charges": [
                    {
                        "setting": "outpatient",
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


class TestProfiling:
    def test_tall_csv_layout(self):
        profile = profile_stream(stream_of(TALL_CSV))

        assert profile.layout == "csv-tall"
        assert profile.rate_lines == 4
        assert profile.pairs["Aetna || All Commercial Plans"] == 1
        assert profile.value_kind == {"dollar": 2, "algorithm": 1, "percentage": 1}
        assert profile.methodology["fee schedule"] == 2
        assert profile.product_class["medicare_advantage"] == 1
        assert profile.hospital_only_lines == 2

    def test_wide_csv_layout_reads_payer_from_column_names(self):
        profile = profile_stream(stream_of(WIDE_CSV))

        assert profile.layout == "csv-wide"
        assert profile.pairs["Aetna || Medicare Advantage"] == 1
        assert profile.value_kind["dollar"] == 3
        assert profile.value_kind["algorithm"] == 1

    def test_bom_prefixed_json_is_not_mistaken_for_csv(self):
        """Observed on all 24 Northwell files: a UTF-8 BOM ahead of the '{'."""
        profile = profile_stream(stream_of("﻿" + JSON_MRF))

        assert profile.layout == "json"
        assert profile.rate_lines == 2
        assert profile.error is None

    def test_json_layout(self):
        profile = profile_stream(stream_of(JSON_MRF))

        assert profile.layout == "json"
        assert profile.rate_lines == 2
        assert profile.product_class["medicare_advantage"] == 1
        assert profile.value_kind == {"dollar": 1, "algorithm": 1}

    def test_truncated_json_yields_a_partial_profile_not_an_error(self):
        """A capped pass over a huge file is a sample, not a failure."""
        profile = profile_stream(stream_of(JSON_MRF, max_bytes=200))

        assert profile.truncated
        assert profile.error is None

    def test_file_without_a_template_header_is_reported(self):
        profile = profile_stream(stream_of("just,some,csv\n1,2,3\n"))

        assert profile.error is not None
        assert profile.rate_lines == 0

    def test_blank_payer_and_plan_rows_are_skipped(self):
        csv_text = TALL_CSV + "Orphan,70450,,,,,,\n"
        profile = profile_stream(stream_of(csv_text))

        assert profile.rate_lines == 4
