"""Region tests.

The comparison a region enables is "could these hospitals serve the same
patient". So the tests are about the boundaries that would make a comparison
meaningless: a state line, a different economy, or a region with nobody in it to
compare against.
"""

from pathlib import Path

import pytest

from hospital.region import (
    UNKNOWN,
    RegionIndex,
    peer_groups,
)

CMS = (
    Path(__file__).parent.parent.parent
    / "data"
    / "reference"
    / "cms_hospital_general_information.csv"
)


@pytest.fixture(scope="module")
def index() -> RegionIndex:
    if not CMS.exists():
        pytest.skip("CMS facility reference not present")
    return RegionIndex.from_csv(CMS)


class TestCountyLookup:
    def test_the_reference_loads(self, index):
        assert len(index.counties) > 150

    def test_an_unknown_hospital_has_no_county(self, index):
        assert index.county_of("Not A Real Hospital Anywhere") == ""
        assert index.county_of(None) == ""

    def test_a_short_name_does_not_prefix_match(self, index):
        """A two-letter stem would collide with half the file."""
        assert index.county_of("St") == ""


class TestRegion:
    def test_new_york_city_metro_includes_the_suburbs_it_competes_with(self, index):
        """Nassau and Westchester hospitals bid against Manhattan ones."""
        from hospital.region import COUNTY_REGIONS

        for county in ("NEW YORK", "KINGS", "NASSAU", "SUFFOLK", "WESTCHESTER"):
            assert COUNTY_REGIONS[county] == "New York City metro"

    def test_connecticut_facilities_leave_the_new_york_group(self, index):
        """Northwell publishes four Connecticut hospitals alongside its NY ones."""
        for name in ("Danbury Hospital", "New Milford Hospital", "Norwalk Hospital"):
            assert index.region_of(name, "Northwell Health") == "Connecticut"

    def test_hudson_valley_facilities_leave_it_too(self, index):
        assert (
            index.region_of("Vassar Brothers Medical Center", "Northwell Health") == "Hudson Valley"
        )

    def test_a_hint_beats_the_system_fallback(self, index):
        """The hints exist for exactly the facilities the system would misplace."""
        assert index.region_of("Putnam Hospital", "Northwell Health") == "Hudson Valley"

    def test_an_unmatched_name_falls_back_to_its_system(self, index):
        """A third of the lake's labels do not match a CMS name."""
        assert (
            index.region_of("NYU Langone|Tisch Hospital", "NYU Langone Health")
            == "New York City metro"
        )

    def test_an_unplaceable_facility_is_unknown_rather_than_guessed(self, index):
        assert index.region_of("Somewhere Unlisted", None) == UNKNOWN


class TestPeerGroups:
    def test_a_region_with_one_system_is_not_a_peer_group(self, index):
        """Nobody to compare against; offering it would imply a comparison."""
        groups = peer_groups(
            [("Mercy Hospital of Buffalo", "Catholic Health System (Buffalo)")], index
        )

        assert groups == {}

    def test_two_systems_in_one_region_are(self, index):
        groups = peer_groups(
            [
                ("Rochester General Hospital", "Rochester Regional Health"),
                ("Strong Memorial Hospital", "University of Rochester Medical Center"),
            ],
            index,
        )

        assert len(groups) == 1
        assert next(iter(groups.values())) == {
            "Rochester Regional Health",
            "University of Rochester Medical Center",
        }

    def test_the_same_system_twice_is_still_one_system(self, index):
        """Two hospitals of one system do not make a peer group."""
        groups = peer_groups(
            [
                ("The Mount Sinai Hospital", "Mount Sinai Health System"),
                ("Mount Sinai Queens", "Mount Sinai Health System"),
            ],
            index,
        )

        assert groups == {}

    def test_unknown_regions_are_excluded(self, index):
        groups = peer_groups([("Somewhere Unlisted", "A"), ("Somewhere Else Unlisted", "B")], index)

        assert groups == {}
