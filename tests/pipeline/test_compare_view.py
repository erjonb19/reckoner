"""The Compare page's logic, on rows copied from the 2026-10-01 rate release.

Every facility string and rate below is as the release holds it, mojibake
included, so the label and matching rules are tested against what the files
actually publish rather than against tidy invented names.
"""

from __future__ import annotations

import pyarrow as pa
import pytest

from pipeline import compare_view as compare

BRONX = (
    "Montefiore Medical Center|Montefiore Hospital - Moses DIV|Montefiore Medical Center - "
    "Weiler (Einstein Campus)|Montefiore Medical Center - Wakefield|Montefiore Medical Center"
    " - Hutchinson Campus|Montefiore Medical Center - Westchester Square|Montefiore Medical "
    "Center â€“ Childrenâ€™s Hospital at Montefiore Einstein"
)
NYACK = (
    "Nyack Hospital|Nyack Hospital - Hematology Oncology|Nyack Hospital - Rheumatology and "
    "Specialty Care MNH"
)
CORNWALL = (
    "St. Luke's Cornwall Hospital|St. Luke's Cornwall Hospital - Cornwall Woundcare|St. Luke's"
    " Cornwall Hospital - Hospital PT|St. Luke's Cornwall Hospital - Hospital Extension Clinic"
    "|St. Luke's Cornwall Hospital - Cornwall Campus"
)
VERNON = "Montefiore Mount Vernon Hospital"
WHITE_PLAINS = "White Plains Hospital"
TISCH = "NYU Langone|Tisch Hospital"
WMC = (
    "Westchester Medical Center | Westchester Medical Center | Westchester Medical Center | "
    "Westchester Medical Center"
)

SYSTEM = {
    BRONX: "Montefiore",
    NYACK: "Montefiore",
    CORNWALL: "Montefiore",
    VERNON: "Montefiore",
    WHITE_PLAINS: "White Plains",
    TISCH: "NYU Langone",
}

# (facility, carrier, code, code_type, rates behind it, rate)
ROWS = [
    (BRONX, "Aetna", "70450", "CPT", 3, 543.09),
    (VERNON, "Aetna", "70450", "CPT", 2, 346.77),
    (NYACK, "Aetna", "70450", "CPT", 3, 113.02),
    (CORNWALL, "Aetna", "70450", "CPT", 3, 129.2),
    (WHITE_PLAINS, "Aetna", "70450", "CPT", 3, 703.08),
    (TISCH, "Aetna", "70450", "CPT", 25, 388.17),
    (BRONX, "Aetna", "99285", "CPT", 3, 2625.0),
    (VERNON, "Aetna", "99285", "CPT", 2, 3337.0),
    (NYACK, "Aetna", "99285", "CPT", 2, 896.48),
    (CORNWALL, "Aetna", "99285", "CPT", 3, 1260.0),
    (WHITE_PLAINS, "Aetna", "99285", "CPT", 3, 6582.0),
    (BRONX, "Aetna", "470", "MS-DRG", 1, 14919.88),
    (VERNON, "Aetna", "470", "MS-DRG", 2, 57556.77),
    (NYACK, "Aetna", "470", "MS-DRG", 3, 32370.26),
    (CORNWALL, "Aetna", "470", "MS-DRG", 3, 69716.36),
    (WHITE_PLAINS, "Aetna", "470", "MS-DRG", 3, 73798.47),
    # Nyack lists 80161 under both labels; the row with more rates should win.
    (NYACK, "Aetna", "80161", "CPT", 1, 10.5),
    (NYACK, "Aetna", "80161", "HCPCS", 2, 32.06),
    (BRONX, "Aetna", "80161", "CPT", 2, 120.41),
    (BRONX, "Cigna", "70450", "CPT", 2, 701.42),
    (NYACK, "Cigna", "70450", "CPT", 2, 1766.355),
    (CORNWALL, "Cigna", "70450", "CPT", 6, 1747.32),
]


@pytest.fixture
def rates() -> pa.Table:
    return pa.table(
        {
            "system": [SYSTEM[r[0]] for r in ROWS],
            "facility": [r[0] for r in ROWS],
            "carrier": [r[1] for r in ROWS],
            "code": [r[2] for r in ROWS],
            "code_type": [r[3] for r in ROWS],
            "hospital_rates": pa.array([r[4] for r in ROWS], pa.int32()),
            "hospital_rate": [r[5] for r in ROWS],
        }
    )


@pytest.fixture
def family(rates: pa.Table) -> list[compare.Hospital]:
    by = {h.label: h for h in compare.hospitals(rates)}
    return [by[label] for label in compare.PRESETS["Montefiore family"]]


class TestLabels:
    @pytest.mark.parametrize(
        ("facility", "label"),
        [
            (BRONX, compare.MONTEFIORE_BRONX),
            (NYACK, "Nyack Hospital"),
            (CORNWALL, "St. Luke's Cornwall Hospital"),
            (TISCH, "NYU Langone Tisch Hospital"),
            (WMC, "Westchester Medical Center"),
            ("Montefiore Health System", "Montefiore (location not stated)"),
            ("Zucker Hillside Hospital Hospital", "Zucker Hillside Hospital"),
            ("Mount Sinai Morningside/West", "Mount Sinai Morningside/West"),
        ],
    )
    def test_each_published_shape_gets_a_readable_name(self, facility, label):
        assert compare.facility_label(facility) == label

    def test_the_bronx_entry_lists_its_seven_campuses_with_the_encoding_fixed(self):
        covers = compare.locations(BRONX)

        assert len(covers) == 7
        assert (
            covers[-1]
            == "Montefiore Medical Center \u2013 Children\u2019s Hospital at Montefiore Einstein"
        )

    def test_unrelated_names_in_one_entry_say_how_many_more(self):
        assert compare.facility_label("Alpha Hospital|Beta Clinic") == "Alpha Hospital (+1 more)"

    def test_two_entries_that_would_share_a_label_are_told_apart(self):
        table = pa.table({"system": ["A", "B"], "facility": ["Good Samaritan Hospital"] * 2})

        labels = sorted(h.label for h in compare.hospitals(table))

        assert labels == ["Good Samaritan Hospital (A)", "Good Samaritan Hospital (B)"]

    def test_every_preset_hospital_exists_in_the_data(self, rates):
        labels = {h.label for h in compare.hospitals(rates)}

        assert set(compare.PRESETS["Montefiore family"]) <= labels


class TestHeadToHead:
    def test_ct_head_aetna_ranks_the_montefiore_family_cheapest_first(self, rates, family):
        rows = compare.head_to_head(rates, family, "Aetna", "70450")

        assert [r["hospital"] for r in rows] == [
            "Nyack Hospital",
            "St. Luke's Cornwall Hospital",
            "Montefiore Mount Vernon Hospital",
            compare.MONTEFIORE_BRONX,
            "White Plains Hospital",
        ]
        assert rows[0]["lowest"] and rows[0]["vs_lowest"] == 0
        assert rows[-1]["vs_lowest"] == pytest.approx(703.08 / 113.02 - 1)
        assert rows[2]["vs_middle"] == 0, "Mount Vernon is the middle of five"

    def test_a_rate_under_a_third_of_the_group_is_flagged(self, rates, family):
        rows = {r["hospital"]: r for r in compare.head_to_head(rates, family, "Aetna", "70450")}

        assert rows["Nyack Hospital"]["check"].endswith("check before quoting")
        assert rows["White Plains Hospital"]["check"] == ""

    def test_a_hospital_that_does_not_publish_stays_on_the_list_last(self, rates, family):
        rows = compare.head_to_head(rates, family, "Cigna", "70450")

        assert [r["published"] for r in rows] == [True, True, True, False, False]
        assert rows[-1]["rate"] is None and rows[-1]["vs_lowest"] is None

    def test_the_insurer_changes_the_order(self, rates, family):
        aetna = compare.head_to_head(rates, family, "Aetna", "70450")
        cigna = compare.head_to_head(rates, family, "Cigna", "70450")

        assert aetna[0]["hospital"] == "Nyack Hospital"
        assert cigna[0]["hospital"] == compare.MONTEFIORE_BRONX

    def test_the_row_backed_by_more_rates_wins_when_a_code_has_two_labels(self, rates, family):
        rows = {r["hospital"]: r for r in compare.head_to_head(rates, family, "Aetna", "80161")}

        assert rows["Nyack Hospital"]["rate"] == 32.06
        assert rows["Nyack Hospital"]["rates_behind_it"] == 2


class TestScorecard:
    def test_the_index_puts_each_hospital_on_one_scale(self, rates, family):
        cells, summary = compare.scorecard(rates, family, "Aetna", ["70450", "99285", "470"])

        by = {r["hospital"]: r for r in summary}
        assert by["White Plains Hospital"]["index"] > 1.5
        assert by["Nyack Hospital"]["index"] < 0.5
        assert by["Nyack Hospital"]["lowest_on"] == 2, "CT head and the ER visit"
        assert by[compare.MONTEFIORE_BRONX]["lowest_on"] == 1, "the DRG 470 outlier"
        assert summary[0]["hospital"] == "Nyack Hospital", "sorted cheapest first"
        assert len(cells) == 3 * len(family)

    def test_a_code_fewer_than_two_hospitals_publish_is_left_out(self, rates, family):
        cells, summary = compare.scorecard(rates, family, "Cigna", ["70450", "99999"])

        assert {c["code"] for c in cells} == {"70450"}
        assert all(r["compared"] == 1 for r in summary)


def test_one_hospital_lists_every_insurer_highest_first(rates, family):
    nyack = next(h for h in family if h.label == "Nyack Hospital")

    rows = compare.by_insurer(rates, nyack, ["Aetna", "Cigna", "Emblem"], "70450")

    assert [(r["carrier"], r["rate"]) for r in rows] == [
        ("Cigna", 1766.355),
        ("Aetna", 113.02),
        ("Emblem", None),
    ]


def test_every_preset_service_has_a_plain_name():
    assert all(compare.SERVICES[code] for code in compare.DEFAULT_BASKET)
    assert compare.service_name("12345", "HC SOMETHING") == "HC SOMETHING"


@pytest.mark.parametrize(("value", "shown"), [(None, ""), (38.65, "$38.65"), (14919.88, "$14,920")])
def test_money_reads_like_a_price(value, shown):
    assert compare.money(value) == shown


@pytest.mark.parametrize(("value", "shown"), [(None, ""), (0.0, "same"), (0.566, "+57%")])
def test_percent_reads_like_a_difference(value, shown):
    assert compare.percent(value) == shown
