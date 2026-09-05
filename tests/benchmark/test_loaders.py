"""Loader tests against trimmed excerpts of the real CMS publications.

Per CLAUDE.md the fixtures are real files, not the spec: each one is the actual
FY2025 IPPS release with rows removed, keeping the multi-row titles, footnote
markers, non-breaking spaces and nested zip that the real files carry. Those are
the things that break a loader, so they are the things the fixtures preserve.
"""

from pathlib import Path

import pytest

from benchmark.loaders import (
    IppsStandardizedAmounts,
    SourceFormatError,
    geography_for,
    load_ipps_drg_weights,
    load_ipps_standardized_amounts,
    load_ipps_wage_index_by_cbsa,
    load_ipps_wage_index_by_ccn,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "cms"
TABLE_5 = FIXTURES / "ipps_table5_sample.zip"
TABLES_1 = FIXTURES / "ipps_tables1a_1e_sample.zip"
TABLES_2_3 = FIXTURES / "ipps_tables2_3_sample.zip"

YEAR = 2025


class TestDrgWeights:
    def test_reads_real_table_5(self):
        rates = load_ipps_drg_weights(TABLE_5, YEAR)

        assert rates
        assert all(r.schedule == "IPPS" and r.code_type == "MS-DRG" for r in rates)
        assert all(r.is_weighted for r in rates), "IPPS publishes a weight, never a price"

    def test_known_weight_is_exact(self):
        by_code = {r.code: r for r in load_ipps_drg_weights(TABLE_5, YEAR)}

        # DRG 470, the highest-volume joint replacement, as published in the
        # FY2025 correction notice -- the publication the loader prefers.
        assert by_code["470"].weight == pytest.approx(1.8855)
        assert "JOINT REPLACEMENT" in (by_code["470"].description or "")
        assert by_code["291"].weight == pytest.approx(1.3048)

    def test_ungroupable_drgs_are_dropped(self):
        codes = {r.code for r in load_ipps_drg_weights(TABLE_5, YEAR)}

        # 998 and 999 carry no weight; keeping them would price the unpriceable.
        assert "998" not in codes and "999" not in codes

    def test_capped_weight_is_the_default(self):
        capped = {r.code: r.weight for r in load_ipps_drg_weights(TABLE_5, YEAR)}
        uncapped = {
            r.code: r.weight for r in load_ipps_drg_weights(TABLE_5, YEAR, use_capped_weight=False)
        }

        # DRG 002 is the fixture's cap-affected row: the 10% cap lifts it well
        # above its uncapped weight, so the two columns must not be confused.
        assert uncapped["002"] == pytest.approx(9.4045)
        assert capped["002"] == pytest.approx(11.0197)

    def test_correction_notice_supersedes_the_final_rule(self):
        # The archive carries both publications and they disagree: DRG 002 is
        # 9.4038 in the final rule and 9.4045 in the correction notice. Which
        # one a loader picks must be a decision, not an accident of filename
        # ordering.
        corrected = {
            r.code: r.weight for r in load_ipps_drg_weights(TABLE_5, YEAR, use_capped_weight=False)
        }
        final_rule = {
            r.code: r.weight
            for r in load_ipps_drg_weights(
                TABLE_5, YEAR, use_capped_weight=False, prefer_correction=False
            )
        }

        assert corrected["002"] == pytest.approx(9.4045)
        assert final_rule["002"] == pytest.approx(9.4038)

    def test_source_records_which_publication_was_read(self):
        rates = load_ipps_drg_weights(TABLE_5, YEAR)

        # A number that cannot say which publication it came from cannot be
        # defended when someone else reads the other one.
        assert "Correction Notice" in (rates[0].source or "")

    def test_quoted_titles_containing_commas_survive(self):
        by_code = {r.code: r for r in load_ipps_drg_weights(TABLE_5, YEAR)}

        # Rows 003 and 004 carry titles quoted because they contain a comma,
        # and the correction notice's own header spans three physical lines
        # inside a quoted cell. Row 001 sits after all of that, so its weight
        # landing in the right column proves nothing shifted.
        assert by_code["001"].weight == pytest.approx(28.1683)

    def test_missing_file_is_a_stop_not_an_empty_index(self, tmp_path):
        empty = tmp_path / "empty.zip"
        import zipfile

        with zipfile.ZipFile(empty, "w") as archive:
            archive.writestr("Table 5.txt", "nothing useful here\n")

        with pytest.raises(SourceFormatError):
            load_ipps_drg_weights(empty, YEAR)


class TestStandardizedAmounts:
    def test_reads_both_labor_regimes(self):
        amounts = load_ipps_standardized_amounts(TABLES_1, YEAR)

        assert isinstance(amounts, IppsStandardizedAmounts)
        assert amounts.high_labor_related == pytest.approx(4465.41)
        assert amounts.high_nonlabor_related == pytest.approx(2140.23)
        assert amounts.low_labor_related == pytest.approx(4095.50)
        assert amounts.low_nonlabor_related == pytest.approx(2510.14)

    def test_labor_shares_match_the_published_captions(self):
        amounts = load_ipps_standardized_amounts(TABLES_1, YEAR)

        # Table 1A is captioned 67.6% labor, Table 1B 62%. Deriving the share
        # from the amounts and recovering the caption is what proves the two
        # tables were not transposed.
        assert amounts.for_wage_index(1.4).labor_share == pytest.approx(0.676, abs=1e-4)
        assert amounts.for_wage_index(0.9).labor_share == pytest.approx(0.620, abs=1e-4)

    def test_regime_switches_at_wage_index_one(self):
        amounts = load_ipps_standardized_amounts(TABLES_1, YEAR)

        # Exactly 1.0 takes the low-labour regime, per the Table 1B caption
        # "less than or equal to 1".
        assert amounts.for_wage_index(1.0).labor_share == pytest.approx(0.620, abs=1e-4)
        assert amounts.for_wage_index(1.0001).labor_share == pytest.approx(0.676, abs=1e-4)

    def test_regimes_carry_their_source(self):
        amounts = load_ipps_standardized_amounts(TABLES_1, YEAR)

        assert "FY2025" in amounts.for_wage_index(1.2).source


class TestWageIndex:
    def test_reads_hospitals_keyed_by_ccn(self):
        records = load_ipps_wage_index_by_ccn(TABLES_2_3)

        assert records
        assert all(ccn.isdigit() for ccn in records)
        assert all(r.ccn.startswith("330") for r in records.values()), "fixture is NY only"

    def test_wage_index_is_the_capped_payable_one(self):
        records = load_ipps_wage_index_by_ccn(TABLES_2_3)

        # NY sits on the rural floor: nearly every hospital is lifted to the
        # same index regardless of its own CBSA. Reading a different column
        # would produce plausible per-hospital variation that does not exist.
        indexes = [r.wage_index for r in records.values() if r.wage_index]
        assert indexes.count(pytest.approx(1.3056)) > len(indexes) // 2

    def test_payment_cbsa_wins_over_geographic(self):
        records = load_ipps_wage_index_by_ccn(TABLES_2_3)
        reclassified = [
            r
            for r in records.values()
            if r.payment_cbsa and r.geographic_cbsa and r.payment_cbsa != r.geographic_cbsa
        ]

        assert reclassified, "fixture should contain at least one reclassified hospital"
        # A reclassified hospital is paid at another area's index; reporting its
        # geographic CBSA would explain the wrong number.
        assert all(r.cbsa == r.payment_cbsa for r in reclassified)

    def test_geographic_cbsa_used_when_not_reclassified(self):
        records = load_ipps_wage_index_by_ccn(TABLES_2_3)
        plain = [r for r in records.values() if not r.payment_cbsa and r.geographic_cbsa]

        assert plain
        assert all(r.cbsa == r.geographic_cbsa for r in plain)

    def test_reads_areas_by_cbsa(self):
        areas = load_ipps_wage_index_by_cbsa(TABLES_2_3)

        assert areas
        # 35614 is the NYC metro area; 33 is the New York statewide rural code
        # that the rural floor pins most of the state to.
        assert areas["35614"] == pytest.approx(1.2961)
        assert areas["33"] == pytest.approx(1.3056)

    def test_areas_differ_between_publications(self):
        corrected = load_ipps_wage_index_by_cbsa(TABLES_2_3)
        final_rule = load_ipps_wage_index_by_cbsa(TABLES_2_3, prefer_correction=False)

        # The NYC metro index moved between the final rule and the correction
        # notice. Silently reading either one is how two runs of "the same"
        # pipeline produce two different answers.
        assert corrected["35614"] != final_rule["35614"]


class TestGeography:
    def test_builds_geography_for_a_known_hospital(self):
        records = load_ipps_wage_index_by_ccn(TABLES_2_3)
        ccn = next(iter(records))

        geography = geography_for("Example Hospital", ccn, records)

        assert geography is not None
        assert geography.hospital == "Example Hospital"
        assert geography.wage_index == records[ccn].wage_index

    def test_unknown_ccn_returns_none_not_a_default(self):
        records = load_ipps_wage_index_by_ccn(TABLES_2_3)

        # A fabricated wage index of 1.0 would produce a confidently wrong
        # percent-of-Medicare; None becomes a reason code instead.
        assert geography_for("Nowhere General", "999999", records) is None
        assert geography_for("Nowhere General", None, records) is None

    def test_falls_back_to_cbsa_when_the_hospital_row_has_no_index(self):
        from dataclasses import replace

        records = load_ipps_wage_index_by_ccn(TABLES_2_3)
        ccn = next(k for k, v in records.items() if v.cbsa == "35614")
        records[ccn] = replace(records[ccn], wage_index=None)
        areas = load_ipps_wage_index_by_cbsa(TABLES_2_3)

        geography = geography_for("Example", ccn, records, areas)

        assert geography is not None
        assert geography.wage_index == pytest.approx(1.2961)
