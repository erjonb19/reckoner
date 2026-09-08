"""Facility resolution tests, from the plan strings Mount Sinai actually publishes.

The bug this guards against is silent: a file naming one hospital while pricing
two merges their rate schedules, and every downstream comparison then measures
the gap between those schedules instead of anything about a contract.
"""

import pytest

from hospital.facility import (
    MOUNT_SINAI_SUFFIXES,
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
