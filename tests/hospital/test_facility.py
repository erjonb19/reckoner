"""Facility resolution tests, from the plan strings Mount Sinai actually publishes.

The bug this guards against is silent: a file naming one hospital while pricing
two merges their rate schedules, and every downstream comparison then measures
the gap between those schedules instead of anything about a contract.
"""

import pytest

from hospital.facility import (
    MOUNT_SINAI_SUFFIXES,
    ambiguous_locations,
    facility_from_source,
    is_multi_facility,
    resolve_facility,
    suffix_of,
)

MSHS = "Mount Sinai Health System"


class TestSuffix:
    @pytest.mark.parametrize(
        ("plan", "expected"),
        [
            ("Cigna Ppo - Msq", "msq"),
            ("Cigna Localplus - Brook", "brook"),
            # The spacing the files really carry.
            ("Cigna Hmo/Oap- Snch", "snch"),
            ("Aetna Whole Health-Tmsh", "tmsh"),
            ("Aetna Hmo/Ppo/Pos Commercial  - Nyeei", "nyeei"),
            # No suffix at all: NYU and NewYork-Presbyterian never carry one.
            ("AETNA INDEMNITY 1006", None),
            ("All Commercial Plans", None),
            (None, None),
        ],
    )
    def test_reads_the_real_spellings(self, plan, expected):
        assert suffix_of(plan) == expected

    def test_a_long_trailing_word_is_not_a_suffix(self):
        """Only a short abbreviation is a facility code."""
        assert suffix_of("Cigna PPO - Medicare Advantage") is None


class TestResolution:
    def test_the_plan_suffix_beats_the_file_location(self):
        """The whole point: the file says one hospital and prices another."""
        assert (
            resolve_facility(MSHS, "Mount Sinai Behavioral Health Center", "Cigna Ppo - Msq")
            == "Mount Sinai Queens"
        )
        assert (
            resolve_facility(
                MSHS, "Mount Sinai Behavioral Health Center", "Cigna Localplus - Brook"
            )
            == "Mount Sinai Brooklyn"
        )

    def test_two_hospitals_in_one_file_stay_apart(self):
        """Brooklyn and Queens shared a file and a label; they must not share a key."""
        rows = [
            ("Mount Sinai Behavioral Health Center", "Cigna Ppo - Msq"),
            ("Mount Sinai Behavioral Health Center", "Cigna Ppo - Brook"),
        ]
        assert is_multi_facility(MSHS, rows)
        assert len({resolve_facility(MSHS, loc, plan) for loc, plan in rows}) == 2

    def test_the_morningside_file_also_holds_two(self):
        rows = [
            ("Mount Sinai Morningside", "Cigna Ppo - Bi"),
            ("Mount Sinai Morningside", "Cigna Ppo - Slw"),
        ]
        assert is_multi_facility(MSHS, rows)

    def test_a_single_facility_file_is_unchanged(self):
        assert (
            resolve_facility(MSHS, "The Mount Sinai Hospital", "Cigna Ppo - Tmsh")
            == "The Mount Sinai Hospital"
        )
        assert not is_multi_facility(MSHS, [("The Mount Sinai Hospital", "Cigna Ppo - Tmsh")])

    def test_both_eye_and_ear_spellings_land_together(self):
        """``Nyee`` and ``Nyeei`` are one hospital written two ways."""
        assert resolve_facility(MSHS, "x", "Cigna Ppo - Nyee") == resolve_facility(
            MSHS, "x", "Cigna Ppo - Nyeei"
        )


class TestScoping:
    """A suffix is not universally a facility."""

    def test_northwells_suffixes_are_products_and_are_left_alone(self):
        """``CHP`` is Child Health Plus. Reading it as a hospital invents one."""
        assert (
            resolve_facility("Northwell Health", "Glen Cove Hospital", "Empire - CHP")
            == "Glen Cove Hospital"
        )

    def test_a_system_with_no_map_keeps_its_file_location(self):
        assert (
            resolve_facility(
                "NYU Langone Health", "NYU Langone|Tisch Hospital", "AETNA INDEMNITY 1006"
            )
            == "NYU Langone|Tisch Hospital"
        )

    def test_an_unmapped_suffix_falls_back_rather_than_inventing(self):
        """Splitting one hospital across two labels is the failure to avoid."""
        assert (
            resolve_facility(MSHS, "The Mount Sinai Hospital", "Cigna Ppo - Zzz")
            == "The Mount Sinai Hospital"
        )

    def test_no_location_falls_back_to_the_system(self):
        assert resolve_facility(MSHS, None, "Some Plan") == MSHS

    def test_the_map_covers_every_suffix_seen_in_the_lake(self):
        seen = {"tmsh", "msq", "brook", "bi", "slw", "snch", "nyeei", "nyee"}
        assert seen <= set(MOUNT_SINAI_SUFFIXES)


class TestFilenameFallback:
    """A location label that cannot distinguish its own files is not a facility.

    Northwell publishes Danbury and New Milford under "Danbury Hospital", and
    Catholic Health publishes four Buffalo hospitals under one name. In both the
    hospital's name is in the file's name, following the CMS convention.
    """

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            (
                "https://x/16-0762843_Kenmore-Mercy-Hospital_StandardCharges.csv",
                "Kenmore Mercy Hospital",
            ),
            (
                "https://x/060646597_new-milford-hospital_standardcharges.zip",
                "New Milford Hospital",
            ),
            # A doubled word is really in the filename; keep it rather than guess.
            (
                "https://x/Zucker_Hillside_Hospital_Hospital_StandardCharges.zip",
                "Zucker Hillside Hospital Hospital",
            ),
            # No tax-id prefix at all.
            ("https://x/Glen_Cove_Hospital_StandardCharges.zip", "Glen Cove Hospital"),
            # Query strings and paths must not confuse it.
            (
                "https://x/y/Phelps_Hospital_StandardCharges.zip?u=1&download=true",
                "Phelps Hospital",
            ),
        ],
    )
    def test_reads_the_cms_filename_convention(self, url, expected):
        assert facility_from_source(url) == expected

    def test_minor_words_stay_lowercase(self):
        assert (
            facility_from_source(
                "https://x/16-0743187_Sisters-of-Charity-Hospital_StandardCharges.csv"
            )
            == "Sisters of Charity Hospital"
        )

    def test_a_filename_that_does_not_follow_the_convention_gives_nothing(self):
        assert facility_from_source("https://x/rates.json") == ""
        assert facility_from_source(None) == ""

    def test_ambiguity_is_measured_from_the_files_present(self):
        """Not hardcoded, so a system that starts or stops colliding is handled."""
        found = ambiguous_locations(
            [
                ("Danbury Hospital", "a.zip"),
                ("Danbury Hospital", "b.zip"),
                ("Glen Cove Hospital", "c.zip"),
            ]
        )

        assert found == {"Danbury Hospital"}

    def test_a_merged_label_is_replaced_by_the_filename(self):
        assert (
            resolve_facility(
                "Northwell Health",
                "Danbury Hospital",
                None,
                source_url="https://x/060646597_new-milford-hospital_standardcharges.zip",
                ambiguous=frozenset({"Danbury Hospital"}),
            )
            == "New Milford Hospital"
        )

    def test_an_unambiguous_label_is_left_alone(self):
        """The name inside the file beats the name on it, when it distinguishes."""
        assert (
            resolve_facility(
                "Northwell Health",
                "Glen Cove Hospital",
                None,
                source_url="https://x/Glen_Cove_Hospital_StandardCharges.zip",
                ambiguous=frozenset(),
            )
            == "Glen Cove Hospital"
        )

    def test_a_suffix_map_still_wins(self):
        """Mount Sinai's suffixes are more specific than any filename."""
        assert (
            resolve_facility(
                MSHS,
                "Mount Sinai Behavioral Health Center",
                "Cigna Ppo - Msq",
                source_url="https://x/135564934_mount-sinai-behavioral-health-center_standardcharges.json",
                ambiguous=frozenset({"Mount Sinai Behavioral Health Center"}),
            )
            == "Mount Sinai Queens"
        )
