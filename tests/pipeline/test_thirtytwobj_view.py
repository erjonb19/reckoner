"""The 32BJ page's logic, held to what the page claims.

The promises that carry the most weight: a pasted code that could be two kinds of code
is flagged, never guessed; a cell with several rates shows its range, never one of
them; a revenue code paired with a CPT code is labelled for what the contract says,
because Anthem's file never prices a CPT code per revenue code; and no inpatient rate
is set beside a rate of the other DRG type.

The fixture ``tests/fixtures/thirtytwobj/facility_rates.parquet`` is a slice of real
rows from Anthem's public 2026-10-01 files (NY_HYBYMED32BJ and NY_HXRLMED0000_06_06),
written by ``scripts/32bj``; see that folder's README.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from openpyxl import load_workbook

from pipeline import thirtytwobj_view as bj

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "thirtytwobj"


# --- pasting ----------------------------------------------------------------------------


def test_paste_splits_lines_tabs_commas_and_spaces() -> None:
    p = bj.parse_paste("70450\n74177, 66984\n77067 43239\n")
    assert [e.code for e in p.entries] == ["70450", "74177", "66984", "77067", "43239"]
    assert p.pasted == 5


def test_paste_drops_headers_blanks_and_duplicates() -> None:
    p = bj.parse_paste("CPT Code\n\n70450\n 70450 \nDescription\n70450\n")
    assert [e.code for e in p.entries] == ["70450"]
    assert p.duplicates == 2


def test_two_columns_become_a_pair_only_when_the_first_looks_like_a_revenue_code() -> None:
    p = bj.parse_paste("0450\t99284\n320\t70450\n70450\tCT head\n")
    assert [(e.rc, e.code) for e in p.entries] == [
        ("0450", "99284"),
        ("0320", "70450"),
        (None, "70450"),
    ]


def test_a_drg_and_severity_split_by_a_space_is_not_split_into_two_codes() -> None:
    p = bj.parse_paste("139 4\n")
    assert [e.code for e in p.entries] == ["139-4"]
    assert p.split_note == ["139 4"]


# --- what a code could be ---------------------------------------------------------------


def test_candidates_restore_leading_zeros() -> None:
    assert ("MS-DRG", "039") in bj.candidates("39")
    assert ("CPT", "00100") in bj.candidates("100")
    assert ("RC", "0450") in bj.candidates("450")


def test_a_three_digit_code_offers_both_drg_families() -> None:
    c = bj.candidates("470")
    assert ("MS-DRG", "470") in c
    assert {("APR-DRG", f"470{s}") for s in "1234"} <= set(c)


def test_drg_with_severity_forms() -> None:
    for text in ("139-4", "139.4", "139 4"):
        assert bj.candidates(text) == [("APR-DRG", "1394")]


def test_hcpcs_and_category_iii_shapes() -> None:
    assert bj.candidates("g0300") == [("HCPCS", "G0300")]
    assert set(bj.candidates("0223U")) == {("CPT", "0223U"), ("HCPCS", "0223U")}


def test_450_is_flagged_when_it_is_both_an_ms_drg_and_a_revenue_code() -> None:
    available = {("MS-DRG", "450"), ("RC", "0450")}
    res = bj.resolve(bj.parse_paste("450"), available)
    assert set(res.found) == {("MS-DRG", "450"), ("RC", "0450")}
    assert res.ambiguous and res.ambiguous[0]["pasted"] == "450"


def test_a_drg_read_in_each_systems_own_type_is_not_ambiguous() -> None:
    available = {("MS-DRG", "470"), ("APR-DRG", "4701"), ("APR-DRG", "4702")}
    res = bj.resolve(bj.parse_paste("470"), available)
    assert not res.ambiguous
    assert set(res.found) == available


def test_not_found_and_picked_codes_combine() -> None:
    res = bj.resolve(
        bj.parse_paste("99999\n70450"), {("CPT", "70450"), ("CPT", "74177")}, [("CPT", "74177")]
    )
    assert res.not_found == ["99999"]
    assert res.found == [("CPT", "70450"), ("CPT", "74177")]


# --- cells ------------------------------------------------------------------------------


def test_a_cell_with_several_rates_shows_the_range_and_the_flag() -> None:
    row = {"min_rate": 100.0, "max_rate": 150.0, "n_rates": 3}
    assert bj.cell_text(row) == f"$100.00 – $150.00 {bj.FLAG} (3)"  # noqa: RUF001 - the page's range dash
    assert bj.cell_text(None) == "—"


def test_ratio_needs_a_single_rate_on_both_sides() -> None:
    one = {"min_rate": 91.7, "max_rate": 91.7, "n_rates": 1}
    std = {"min_rate": 100.0, "max_rate": 100.0, "n_rates": 1}
    assert bj.ratio(one, std) == pytest.approx(0.917)
    assert bj.ratio({**one, "n_rates": 2}, std) is None


def test_rc_label_follows_the_bundle_list() -> None:
    bundle = {"arrangement": "bundle", "bundled_rcs": ["0320", "0350"]}
    assert bj.rc_label(bundle, "0320") == bj.RC_LISTED
    assert bj.rc_label(bundle, "0450") == bj.RC_NOT_LISTED
    assert bj.rc_label({"arrangement": "ffs", "bundled_rcs": []}, "0320") == bj.RC_NOT_SPECIFIC
    assert bj.rc_label(bundle, None) == ""


# --- on real rows -----------------------------------------------------------------------


@pytest.fixture(scope="module")
def data() -> bj.Data:
    d = bj.load(FIXTURE)
    assert d is not None, f"fixture missing: {FIXTURE}"
    return d


def test_each_system_is_tied_to_one_drg_type(data: bj.Data) -> None:
    assert data.drg_type["montefiore"] == "APR-DRG"
    assert data.drg_type["mount_sinai"] == "MS-DRG"
    assert data.drg_type["nyu"] == "MS-DRG"


def test_drg_tables_never_mix_types(data: bj.Data) -> None:
    apr = bj.top_table(data, "inpatient_apr_drg", bj.Filters())
    ms = bj.top_table(data, "inpatient_ms_drg", bj.Filters())
    assert apr.long and ms.long
    assert {r["system"] for r in apr.long} == {"montefiore"}
    assert {r["code_type"] for r in apr.long} == {"APR-DRG"}
    assert {r["system"] for r in ms.long} <= {"mount_sinai", "nyu"}
    assert {r["code_type"] for r in ms.long} == {"MS-DRG"}


def test_mount_sinai_main_campus_is_below_standard_and_montefiore_is_not(data: bj.Data) -> None:
    disc = {d["campus"]: d for d in bj.discount_summary(data, bj.Filters())}
    assert disc["The Mount Sinai Hospital"]["median 32BJ ÷ standard"] == pytest.approx(
        0.917, abs=0.002
    )
    assert disc["Montefiore Medical Center (Bronx)"]["median 32BJ ÷ standard"] == pytest.approx(1.0)


def test_dollar_tables_exclude_per_diem_and_percentage(data: bj.Data) -> None:
    table = bj.top_table(data, "inpatient_ms_drg", bj.Filters())
    assert table.long
    assert not any("per diem" in r["basis"] or "percentage" in r["basis"] for r in table.long)
    assert bj.other_methods(data, bj.Filters(), "per diem"), (
        "per diem rows belong in their own table"
    )


def test_the_network_filter_drops_the_other_columns(data: bj.Data) -> None:
    table = bj.top_table(data, "inpatient_ms_drg", bj.Filters(network=bj.TIER))
    assert table.columns
    assert not any("standard" in c for c in table.columns)


def test_a_cell_shows_both_networks_and_their_ratio(data: bj.Data) -> None:
    table = bj.top_table(data, "inpatient_ms_drg", bj.Filters())
    row = next(r for r in table.rows if r.get("The Mount Sinai Hospital · 32BJ", "—") != "—")
    assert row["The Mount Sinai Hospital · standard"] != "—"
    assert float(row["The Mount Sinai Hospital · 32BJ ÷ standard"]) == pytest.approx(
        0.917, abs=0.003
    )


def test_findings_workbook_has_three_sheets(data: bj.Data) -> None:
    rows = bj.top_table(data, "inpatient_apr_drg", bj.Filters()).long
    wb = load_workbook(io.BytesIO(bj.findings_workbook(data, rows)))
    assert wb.sheetnames == ["Summary", "Definitions", "Data"]
    assert wb["Data"].max_row == len(rows) + 1


# --- outpatient under review --------------------------------------------------------------


def test_outpatient_table_is_not_offered_while_under_review() -> None:
    assert bj.OUTPATIENT_UNDER_REVIEW
    assert bj.visible_tables() == ["inpatient_apr_drg", "inpatient_ms_drg"]


def test_outpatient_rates_never_reach_a_table_while_under_review(data: bj.Data) -> None:
    present = data.con.sql("select count(*) from rates where setting = 'outpatient'").fetchone()
    assert present and present[0] > 0, (
        "the fixture holds outpatient rows, so the filter is what hides them"
    )
    codes = [("CPT", "70450"), ("CPT", "74177"), ("CPT", "66984")]
    assert bj.cell_rows(data, codes, bj.Filters(setting="outpatient")) == []
    assert not any(
        r["setting"] == "outpatient" for r in bj.other_methods(data, bj.Filters(), "per diem")
    )


def test_the_summary_says_outpatient_is_under_review(data: bj.Data) -> None:
    points = bj.summary_points(data)
    assert any(bj.OUTPATIENT_NOTE in p for p in points)


def test_the_summary_says_northwell_hospitals_are_not_in_the_tier(data: bj.Data) -> None:
    northwell = next(p for p in bj.summary_points(data) if p.startswith("Northwell"))
    assert "not in the 32BJ tier" in northwell
    assert "are in the 32BJ tier" in northwell  # the units that are
    # The files show the absence, not the reason; a contract they do not name is not claimed.
    assert "Northwell Direct" not in northwell
    assert "do not say how" in northwell


def few_codes_data(rows: str) -> bj.Data:
    import duckdb

    con = duckdb.connect()
    con.sql(f"""create view rates as select * from (values {rows})
                t(network, campus, acute_campus, setting, code_type, code, negotiated_type)""")
    return bj.Data(con=con, top25=[], crosscheck=None, drg_type={}, codes={})


def test_few_codes_because_the_tier_lacks_the_case_rates() -> None:
    """NYU Langone Long Island: case rates at standard Anthem, few in the 32BJ tier."""
    std = ", ".join(
        f"('{bj.STANDARD}', 'LI', true, 'inpatient', 'MS-DRG', '{c:03d}', 'derived')"
        for c in range(1, 11)
    )
    tier = f"('{bj.TIER}', 'LI', true, 'inpatient', 'MS-DRG', '001', 'negotiated')"

    reason = bj.few_codes_reason(few_codes_data(f"{std}, {tier}"), "LI", 1)

    assert "lists dollar inpatient rates for 1 DRGs there, against 10" in reason


def test_few_codes_because_the_cells_are_ranges() -> None:
    """Mount Sinai Brooklyn: both networks list every code, but under two NPIs."""
    rows = ", ".join(
        f"('{n}', 'B', true, 'inpatient', 'MS-DRG', '{c:03d}', 'derived')"
        for n in (bj.TIER, bj.STANDARD)
        for c in range(1, 11)
    )

    reason = bj.few_codes_reason(few_codes_data(rows), "B", 2)

    assert "more than one rate" in reason


def test_missing_data_is_none_not_an_error(tmp_path: Path) -> None:
    assert bj.load(tmp_path) is None


def test_excel_holds_single_rates_and_ratios_as_numbers() -> None:
    """Analysts sum and sort the download; a rate stored as "$1,234.50" text cannot be."""
    rows = [
        {"code": "0871", "a": "$11,624.37", "ratio": "0.917", "range": "$1.00 – $2.00 ⚑"},  # noqa: RUF001
    ]
    ws = load_workbook(io.BytesIO(bj.to_xlsx({"t": rows})))["t"]

    code, rate, ratio, rng = (c.value for c in ws[2])
    assert code == "0871"
    assert rate == 11624.37 and ws["B2"].number_format == '"$"#,##0.00'
    assert ratio == 0.917
    assert rng == "$1.00 – $2.00 ⚑"  # noqa: RUF001
