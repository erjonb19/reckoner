import pytest

from hospital.services import (
    ServiceSheet,
    expand_range,
    normalise_revenue,
    parse_rule,
)

PACEMAKER = (
    "0360-0361, 0369, 0481, 0490, 0499, 0750, 0790; CPT Codes: 33206-33208, "
    "33212-33214, 33221, 33224-33225, 33227-33229"
)
RADIATION = "0330, 0339, 0333 (excluding CPT codes 61796-61800, 77371, 77372, 77373)"
DRUG_STENT = "0360-0361, 0369, 0481, 0490, 0499, 0750, 0790 with HCPCS codes of C9600, C9602"
SLEEP = "0740, 0920, 0929 (with CPT codes 95805-95811)"


class TestNormalisation:
    @pytest.mark.parametrize(
        ("given", "expected"), [("172", "0172"), ("0172", "0172"), ("762", "0762"), ("64", "0064")]
    )
    def test_revenue_codes_are_padded_to_four_digits(self, given, expected):
        assert normalise_revenue(given) == expected

    def test_padding_never_strips(self):
        """Revenue 0470 must not collapse onto MS-DRG 470."""
        assert normalise_revenue("0470") == "0470"


class TestRanges:
    def test_inclusive_expansion_at_endpoint_width(self):
        codes, warnings = expand_range("0360", "0361")

        assert codes == ["0360", "0361"]
        assert not warnings

    def test_five_digit_cpt_range(self):
        codes, _ = expand_range("33206", "33208")

        assert codes == ["33206", "33207", "33208"]

    def test_lettered_hcpcs_range(self):
        codes, _ = expand_range("G0398", "G0400")

        assert codes == ["G0398", "G0399", "G0400"]

    def test_mismatched_width_is_a_warning_not_a_guess(self):
        """The sheet contains "6320-63621"; inventing codes would be worse."""
        codes, warnings = expand_range("6320", "63621")

        assert codes == []
        assert warnings and "width" in warnings[0]

    def test_descending_range_is_rejected(self):
        codes, warnings = expand_range("0361", "0360")

        assert codes == [] and warnings


class TestRuleParsing:
    def test_revenue_with_cpt_intersection(self):
        rule = parse_rule("Hospital Outpatient Surgery", "Pacemaker", PACEMAKER)

        assert "0360" in rule.revenue_codes
        assert "0790" in rule.revenue_codes
        assert "33206" in rule.procedure_codes
        assert "33228" in rule.procedure_codes
        assert not rule.excluded_procedure_codes
        assert not rule.warnings

    def test_exclusion_clause(self):
        rule = parse_rule("Priority 2", "Radiation Therapy", RADIATION)

        assert rule.revenue_codes == {"0330", "0339", "0333"}
        assert "61798" in rule.excluded_procedure_codes
        assert "77372" in rule.excluded_procedure_codes
        assert not rule.procedure_codes

    def test_hcpcs_clause(self):
        rule = parse_rule("HOS", "Drug-eluting stent", DRUG_STENT)

        assert "C9600" in rule.procedure_codes
        assert "0360" in rule.revenue_codes

    def test_parenthesised_with_clause(self):
        rule = parse_rule("Priority 4", "Sleep Studies", SLEEP)

        assert rule.revenue_codes == {"0740", "0920", "0929"}
        assert "95808" in rule.procedure_codes

    def test_bare_revenue_list(self):
        rule = parse_rule("Therapy", "Physical Therapy", "0420-0424, 0429")

        assert rule.revenue_codes == {"0420", "0421", "0422", "0423", "0424", "0429"}
        assert not rule.procedure_codes


class TestMatching:
    RULE = parse_rule("HOS", "Pacemaker", PACEMAKER)

    def test_both_codes_on_the_same_row_matches(self):
        assert self.RULE.matches({"revenue": {"0360"}, "procedure": {"33206"}})

    def test_revenue_alone_does_not_match(self):
        assert not self.RULE.matches({"revenue": {"0360"}, "procedure": set()})

    def test_procedure_alone_does_not_match(self):
        assert not self.RULE.matches({"revenue": set(), "procedure": {"33206"}})

    def test_wrong_revenue_code_does_not_match(self):
        assert not self.RULE.matches({"revenue": {"0999"}, "procedure": {"33206"}})

    def test_exclusion_blocks_an_otherwise_matching_row(self):
        rule = parse_rule("Priority 2", "Radiation Therapy", RADIATION)

        assert rule.matches({"revenue": {"0330"}, "procedure": {"77401"}})
        assert not rule.matches({"revenue": {"0330"}, "procedure": {"77372"}})

    def test_a_row_can_satisfy_more_than_one_service(self):
        sheet = ServiceSheet(
            [
                parse_rule("A", "Pacemaker", PACEMAKER),
                parse_rule("B", "Any cardiac revenue", "0360-0361"),
            ]
        )

        hits = sheet.classify({"revenue": {"0360"}, "procedure": {"33206"}})

        assert {r.name for r in hits} == {"Pacemaker", "Any cardiac revenue"}


class TestSheetLoading:
    def test_headings_without_codes_become_categories(self, tmp_path):
        openpyxl = pytest.importorskip("openpyxl")
        path = tmp_path / "sheet.xlsx"
        book = openpyxl.Workbook()
        ws = book.active
        ws.append(["Category / Level", "CPT / Rev Codes"])
        ws.append(["Therapy Series", None])
        ws.append(["Physical Therapy", "0420-0424, 0429"])
        book.save(path)

        sheet = ServiceSheet.from_xlsx(str(path))

        assert len(sheet.rules) == 1
        assert sheet.rules[0].category == "Therapy Series"
        assert sheet.rules[0].name == "Physical Therapy"


class TestCorrections:
    def test_correction_repairs_a_malformed_range(self, tmp_path):
        from hospital.services import Correction

        openpyxl = pytest.importorskip("openpyxl")
        path = tmp_path / "sheet.xlsx"
        book = openpyxl.Workbook()
        ws = book.active
        ws.append(["Category / Level", "CPT / Rev Codes"])
        ws.append(["Priority 2", None])
        ws.append(["Radiation Therapy", "0330, 0339, 0333 (excluding CPT codes 6320-63621)"])
        book.save(path)

        correction = Correction("Radiation Therapy", "6320-63621", "63620-63621")
        sheet = ServiceSheet.from_xlsx(str(path), corrections=[correction])

        rule = sheet.rules[0]
        assert rule.excluded_procedure_codes == {"63620", "63621"}
        assert not rule.warnings
        assert sheet.applied_corrections == ["Radiation Therapy: 6320-63621 -> 63620-63621"]

    def test_correction_does_not_touch_other_services(self):
        from hospital.services import Correction

        correction = Correction("Radiation Therapy", "6320-63621", "63620-63621")

        spec, applied = correction.apply("Chemotherapy Administration", "6320-63621")

        assert not applied
        assert spec == "6320-63621"

    def test_corrections_load_from_yaml(self):
        from pathlib import Path

        from hospital.services import load_corrections

        corrections = load_corrections(Path("config/sheet_corrections.yml"))

        assert any(c.find == "6320-63621" and c.replace == "63620-63621" for c in corrections)
        assert all(c.reason for c in corrections), "every correction needs a stated reason"
