"""Choosing which systems an execution reconciles.

One execution per system is how the mart fits in a container: each is a fresh
process, so nothing inherits the previous system's retained pages. Measured,
Mount Sinai alone peaks at 4,748 MiB; by the time Northwell has run after it in
the same process the figure is 6,746.

The selector is fed from an environment variable, so it accepts any of the three
correct spellings of a system. Being strict about which one a shell used would
only produce a run that reconciles nothing -- and reconciling nothing is a
number this pipeline can legitimately produce, which is entry 9.
"""

from __future__ import annotations

import pytest

from pipeline.mart import RECONCILABLE, select


class TestSelect:
    def test_no_selection_means_every_system(self):
        assert select(None) == RECONCILABLE
        assert select("") == RECONCILABLE
        assert select("   ") == RECONCILABLE

    @pytest.mark.parametrize(
        "spelling",
        ["mount-sinai-health-system", "Mount Sinai", "Mount Sinai Health System"],
    )
    def test_any_of_the_three_names_selects_it(self, spelling):
        """The slug, the payer's short label, and the hospital's legal name."""
        chosen = select(spelling)

        assert len(chosen) == 1
        assert chosen[0].slug == "mount-sinai-health-system"

    def test_matching_ignores_case_and_padding(self):
        assert select("  NORTHWELL  ")[0].slug == "northwell-health"

    def test_an_unknown_system_raises_rather_than_reconciling_nothing(self):
        """Silence here would look exactly like a system with no comparable rows."""
        with pytest.raises(ValueError, match="no reconcilable system matches"):
            select("Mont Sinai")

    def test_the_error_names_what_would_have_worked(self):
        with pytest.raises(ValueError, match="northwell-health"):
            select("nope")

    def test_every_system_is_reachable_by_its_slug(self):
        for spec in RECONCILABLE:
            assert select(spec.slug) == (spec,)
