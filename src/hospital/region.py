"""Which market a hospital competes in.

Comparing a rate across hospitals only means something when the hospitals could
plausibly serve the same patient. Mount Sinai against NewYork-Presbyterian is a
question about two competitors; Mount Sinai against Mercy Hospital of Buffalo is
a question about two different economies, and the difference between them is
mostly wage index and payer mix rather than negotiation.

Region is assigned per facility rather than per system, because a health system
is not a market. Northwell publishes rates for Danbury, New Milford, Norwalk and
Sharon -- all in **Connecticut** -- alongside its New York hospitals, and folding
those into a New York peer group would compare across a state line without
saying so.

Two sources, in order of authority:

1. **The CMS facility file**, which gives a county for each hospital it lists.
   That is the real answer where a name matches.
2. **The system's own region**, where it does not. Roughly a third of the
   facility labels in the lake do not match a CMS name -- ``NYU Langone|Tisch
   Hospital`` against ``NYU LANGONE HOSPITALS``, ``Upstate University Hospital``
   against its SUNY name -- and resolving those properly is the same entity
   problem the payer matcher has. Falling back to the system is right far more
   often than it is wrong, and it is stated rather than hidden.

A facility whose region cannot be determined is ``UNKNOWN`` and is left out of
peer comparisons instead of being guessed into one.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

UNKNOWN = "Unknown"

#: County -> market. The New York City metro deliberately includes Nassau,
#: Suffolk and Westchester: those hospitals compete for the same patients and
#: negotiate against the same payers as the ones inside the city line, which is
#: what makes a rate comparison between them meaningful.
COUNTY_REGIONS: dict[str, str] = {
    "NEW YORK": "New York City metro",
    "KINGS": "New York City metro",
    "QUEENS": "New York City metro",
    "BRONX": "New York City metro",
    "RICHMOND": "New York City metro",
    "NASSAU": "New York City metro",
    "SUFFOLK": "New York City metro",
    "WESTCHESTER": "New York City metro",
    "ROCKLAND": "New York City metro",
    "ORANGE": "Hudson Valley",
    "DUTCHESS": "Hudson Valley",
    "PUTNAM": "Hudson Valley",
    "ULSTER": "Hudson Valley",
    "ERIE": "Western New York",
    "NIAGARA": "Western New York",
    "CHAUTAUQUA": "Western New York",
    "MONROE": "Finger Lakes",
    "ONTARIO": "Finger Lakes",
    "WAYNE": "Finger Lakes",
    "ONONDAGA": "Central New York",
    "OSWEGO": "Central New York",
    "MADISON": "Central New York",
    "ALBANY": "Capital Region",
    "SCHENECTADY": "Capital Region",
    "RENSSELAER": "Capital Region",
    "SARATOGA": "Capital Region",
    "BROOME": "Southern Tier",
    "ST. LAWRENCE": "North Country",
}

#: Where a system sits when a facility name cannot be matched. A fallback, not a
#: claim that the system has only one market.
SYSTEM_REGIONS: dict[str, str] = {
    "Mount Sinai Health System": "New York City metro",
    "NYU Langone Health": "New York City metro",
    "NewYork-Presbyterian": "New York City metro",
    "Northwell Health": "New York City metro",
    "NYC Health + Hospitals": "New York City metro",
    "Maimonides Medical Center": "New York City metro",
    "Catholic Health System (Buffalo)": "Western New York",
    "Rochester Regional Health": "Finger Lakes",
    "University of Rochester Medical Center": "Finger Lakes",
    "Upstate University Hospital": "Central New York",
    "Ellis Medicine": "Capital Region",
}

#: Facilities the system fallback would put in the wrong state or market. Keyed
#: on a distinctive fragment of the facility or file name, lowercased. Northwell
#: reaches into Connecticut and the Hudson Valley; treating those as New York
#: City would compare across a state line silently.
FACILITY_REGION_HINTS: tuple[tuple[str, str], ...] = (
    ("danbury", "Connecticut"),
    ("new milford", "Connecticut"),
    ("norwalk", "Connecticut"),
    ("sharon", "Connecticut"),
    ("vassar", "Hudson Valley"),
    ("northern dutchess", "Hudson Valley"),
    ("putnam", "Hudson Valley"),
    ("phelps", "New York City metro"),
    ("northern westchester", "New York City metro"),
)

_PUNCT = re.compile(r"[^a-z0-9]+")


def _key(name: str | None) -> str:
    return _PUNCT.sub("", (name or "").lower())


@dataclass(frozen=True)
class RegionIndex:
    """Facility name -> county, from the CMS file, with the fallbacks applied."""

    counties: dict[str, str]

    @classmethod
    def from_csv(cls, path: Path, state: str = "NY") -> RegionIndex:
        counties: dict[str, str] = {}
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                if state and row.get("State") != state:
                    continue
                county = (row.get("County/Parish") or "").strip().upper()
                if county:
                    counties[_key(row.get("Facility Name"))] = county
        return cls(counties)

    def county_of(self, facility: str | None) -> str:
        """The county CMS lists for this hospital, matched exactly or by prefix."""
        key = _key(facility)
        if not key:
            return ""
        found = self.counties.get(key)
        if found:
            return found
        # A prefix match catches "Mercy Hospital" against "MERCY HOSPITAL OF
        # BUFFALO", but only on a stem long enough not to collide.
        if len(key) >= 12:
            for name, county in self.counties.items():
                if name.startswith(key[:12]) or key.startswith(name[:12]):
                    return county
        return ""

    def region_of(self, facility: str | None, system: str | None = None) -> str:
        """The market this hospital competes in.

        A hint wins over CMS: the hints exist precisely for facilities whose
        system placement would be wrong, and they are checked against the
        facility's own name rather than inferred.
        """
        lowered = (facility or "").lower()
        for fragment, region in FACILITY_REGION_HINTS:
            if fragment in lowered:
                return region
        county = self.county_of(facility)
        if county:
            return COUNTY_REGIONS.get(county, UNKNOWN)
        return SYSTEM_REGIONS.get(system or "", UNKNOWN)


def peer_groups(
    facilities: Iterable[tuple[str, str]], index: RegionIndex, min_systems: int = 2
) -> dict[str, set[str]]:
    """Regions holding enough systems to compare, as region -> systems.

    ``facilities`` is ``(facility, system)`` pairs. A region with one system in
    it is not a peer group: there is nobody to compare against, and presenting
    it as one would imply a comparison the data cannot make.
    """
    found: dict[str, set[str]] = {}
    for facility, system in facilities:
        region = index.region_of(facility, system)
        if region != UNKNOWN:
            found.setdefault(region, set()).add(system)
    return {r: s for r, s in found.items() if len(s) >= min_systems}


__all__ = [
    "COUNTY_REGIONS",
    "FACILITY_REGION_HINTS",
    "SYSTEM_REGIONS",
    "UNKNOWN",
    "RegionIndex",
    "peer_groups",
]
