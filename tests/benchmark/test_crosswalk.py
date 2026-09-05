"""Crosswalk tests against the real CMS facility file and real MRF location names.

The location strings here are the ones the landed corpus actually contains, not
invented examples. Several of them are the cases that broke an earlier scorer,
so they are kept as regressions: each one is a way to attach the wrong CCN to a
hospital, and a wrong CCN is a wage index that is plausible and wrong.
"""

from pathlib import Path

import pytest

from benchmark.crosswalk import (
    Crosswalk,
    load_cms_facilities,
    match_location,
    normalise_facility_name,
    resolve_all,
    score_names,
)

FACILITIES_CSV = (
    Path(__file__).parent.parent / "fixtures" / "cms" / ("hospital_general_information_sample.csv")
)


@pytest.fixture(scope="module")
def facilities():
    return load_cms_facilities(FACILITIES_CSV)


class TestLoading:
    def test_reads_real_facility_file(self, facilities):
        assert facilities
        assert all(f.ccn and f.name for f in facilities)

    def test_acute_only_by_default(self, facilities):
        # IPPS and OPPS price acute care. A psychiatric or critical access
        # hospital matched here would carry a benchmark that does not apply.
        assert all(f.is_acute for f in facilities)

    def test_keeps_out_of_state_facilities(self, facilities):
        # Northwell's file set includes Danbury, Connecticut, via the Nuvance
        # acquisition. A NY-only facility list would strand it forever.
        assert any(f.state == "CT" for f in facilities)


class TestScoring:
    def test_identical_names_score_one(self):
        assert score_names("Bellevue Hospital Center", "BELLEVUE HOSPITAL CENTER") == 1.0

    def test_noise_words_do_not_create_a_match(self):
        # "Hospital" and "Medical Center" are on almost every name; if they
        # counted, every pair would look related.
        assert score_names("Springfield Hospital", "Shelbyville Medical Center") == 0.0

    def test_closed_up_system_names_are_split(self):
        # Hospitals write "NewYork-Presbyterian"; CMS writes "NEW YORK-".
        assert "new york presbyterian" in normalise_facility_name("NewYork-Presbyterian")

    def test_dropping_the_distinguishing_token_is_penalised(self):
        # The regression that matters: "Morningside" is the entire difference
        # between two Mount Sinai hospitals. A containment-based score rates
        # this pair 1.0 and silently merges them.
        assert score_names("Mount Sinai Morningside", "MOUNT SINAI HOSPITAL") < 0.75


class TestMatching:
    def test_exact_facility_resolves_confidently(self, facilities):
        match = match_location("The Mount Sinai Hospital", facilities)

        assert match.ccn == "330024"
        assert match.is_confident
        assert match.routed_to == "accepted"

    def test_system_sibling_does_not_steal_the_match(self, facilities):
        # Regression: an earlier scorer resolved this to QUEENS HOSPITAL CENTER,
        # an unrelated public hospital, at high confidence -- because the system
        # name shared no token and only "Queens" was left to decide.
        match = match_location("NewYork-Presbyterian Queens", facilities)

        assert match.ccn == "330055"
        assert match.facility_name == "NEW YORK-PRESBYTERIAN/QUEENS"
        assert match.is_confident

    def test_absent_facility_is_not_forced_onto_a_sibling(self, facilities):
        # Mount Sinai Morningside is not in this facility list. The nearest
        # name is Mount Sinai Hospital, a different hospital with a different
        # wage index, so the only correct behaviour is to decline.
        match = match_location("Mount Sinai Morningside", facilities)

        assert not match.is_confident
        assert match.routed_to == "review"

    def test_unknown_name_returns_no_ccn(self, facilities):
        match = match_location("Cohen Children's Medical Center", facilities)

        assert match.ccn is None
        assert match.routed_to == "review"

    def test_out_of_state_facility_resolves(self, facilities):
        match = match_location("Danbury Hospital", facilities)

        assert match.ccn == "070033"
        assert match.is_confident

    def test_state_filter_can_strand_a_real_facility(self, facilities):
        # Documents why `states` is not defaulted to NY.
        match = match_location("Danbury Hospital", facilities, states=("NY",))

        assert match.ccn != "070033"
        assert not match.is_confident

    def test_ambiguous_pair_goes_to_review_even_when_scoring_well(self, facilities):
        # Two candidates within the ambiguity margin are a coin flip, and a coin
        # flip must not be reported as a resolution.
        match = match_location("Mount Sinai", facilities)

        assert not match.is_confident

    def test_empty_location_is_handled(self, facilities):
        assert match_location("", facilities).ccn is None
        assert match_location("   ", facilities).ccn is None


class TestCrosswalkPersistence:
    def test_confirmed_decision_outranks_the_scorer(self, facilities):
        crosswalk = Crosswalk()
        crosswalk.confirm("Mount Sinai Morningside", "330046")

        match = crosswalk.resolve("Mount Sinai Morningside", facilities)

        assert match.ccn == "330046"
        assert match.method == "confirmed"
        assert match.is_confident

    def test_rejection_stops_a_location_returning_to_the_queue(self, facilities):
        crosswalk = Crosswalk()
        crosswalk.reject("Cohen Children's Medical Center")

        match = crosswalk.resolve("Cohen Children's Medical Center", facilities)

        assert match.ccn is None
        assert match.method == "confirmed unmatchable"

    def test_confirm_clears_a_prior_rejection(self, facilities):
        crosswalk = Crosswalk()
        crosswalk.reject("Glen Cove Hospital")
        crosswalk.confirm("Glen Cove Hospital", "330181")

        assert crosswalk.resolve("Glen Cove Hospital", facilities).ccn == "330181"

    def test_round_trips_through_a_file(self, tmp_path, facilities):
        crosswalk = Crosswalk()
        crosswalk.confirm("Mount Sinai Morningside", "330046")
        crosswalk.reject("Cohen Children's Medical Center")
        path = tmp_path / "crosswalk.jsonl"
        crosswalk.save(path)

        reloaded = Crosswalk.load(path)

        assert reloaded.confirmed == {"Mount Sinai Morningside": "330046"}
        assert reloaded.rejected == {"Cohen Children's Medical Center"}

    def test_loading_a_missing_file_is_empty_not_an_error(self, tmp_path):
        assert Crosswalk.load(tmp_path / "nope.jsonl").confirmed == {}


LOCATIONS = [
    "The Mount Sinai Hospital",
    "NewYork-Presbyterian Queens",
    "Danbury Hospital",
    "Mount Sinai Morningside",
    "Cohen Children's Medical Center",
]


class TestResolveAll:
    def test_every_accepted_match_is_correct(self, facilities):
        report = resolve_all(LOCATIONS, facilities)

        expected = {
            "The Mount Sinai Hospital": "330024",
            "NewYork-Presbyterian Queens": "330055",
            "Danbury Hospital": "070033",
        }
        for match in report.accepted:
            assert expected[match.location] == match.ccn

    def test_uncertain_matches_are_routed_not_dropped(self, facilities):
        report = resolve_all(LOCATIONS, facilities)

        assert len(report.accepted) + len(report.review) == len(LOCATIONS)
        assert {m.location for m in report.review} == {
            "Mount Sinai Morningside",
            "Cohen Children's Medical Center",
        }

    def test_duplicate_locations_are_resolved_once(self, facilities):
        report = resolve_all(["Danbury Hospital"] * 4, facilities)

        assert len(report.matches) == 1

    def test_report_writes_the_review_queue(self, tmp_path, facilities):
        report = resolve_all(LOCATIONS, facilities)
        path = tmp_path / "crosswalk_review.jsonl"
        report.to_jsonl(path)

        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert len(lines) == len(LOCATIONS)
        assert '"routed_to"' in lines[0]
