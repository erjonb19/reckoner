"""Which hospital a published rate actually belongs to.

The CMS template has one ``location_name`` per file, and a health system that
publishes one file per hospital needs nothing more. Mount Sinai does not: two of
its five files each carry rates for **two** hospitals, and the file-level name
covers only one of them.

    "Behavioral Health Center" file  ->  Brooklyn 355,219 + Queens 251,068
    "Morningside" file               ->  Beth Israel 296,619 + St Luke's/West 284,759
    "The Mount Sinai Hospital" file  ->  itself, 332,055

The facility is instead encoded as a suffix on the plan name -- ``Cigna Ppo -
Msq`` is the Queens rate. Trusting the file-level name merges two hospitals'
rate schedules under one label, and every downstream comparison then measures
the gap between those schedules rather than anything about a contract. It did:
one Cigna rate, which resolves no finer than the health system, differenced
against Queens and Brooklyn base rates under a single name produced 1,458 pairs
at a constant 1.298x and 1,458 more at 1.410x, all reading as findings.

**A suffix is not universally a facility, so this is scoped per system.**
Northwell uses the same position for a product -- ``CHP`` is Child Health Plus,
``MCD`` Medicaid -- and reading those as hospitals would invent facilities that
do not exist. Only systems with a known map are re-attributed; everyone else
keeps the name their file gave, which is right for NYU Langone and
NewYork-Presbyterian, whose plan names carry no suffix at all.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

#: Mount Sinai's plan-name suffixes, from the abbreviations in its own files.
#:
#: ``Slw`` is St Luke's/West. The former St Luke's-Roosevelt is now two
#: hospitals -- Mount Sinai Morningside and Mount Sinai West -- and the files do
#: not say which, or whether the rate covers both. It is mapped to a name that
#: says so rather than to a guess: what the join needs is that it is reliably
#: *distinct* from Beth Israel, which shares its file, and that is certain.
MOUNT_SINAI_SUFFIXES: dict[str, str] = {
    "tmsh": "The Mount Sinai Hospital",
    "msq": "Mount Sinai Queens",
    "brook": "Mount Sinai Brooklyn",
    "bi": "Mount Sinai Beth Israel",
    "slw": "Mount Sinai Morningside/West",
    "snch": "Mount Sinai South Nassau",
    "nyeei": "New York Eye and Ear Infirmary of Mount Sinai",
    "nyee": "New York Eye and Ear Infirmary of Mount Sinai",
}

#: Health system -> its suffix map. Absent from here means the file-level
#: location is trusted, which is the correct default.
FACILITY_SUFFIX_MAPS: dict[str, dict[str, str]] = {
    "Mount Sinai Health System": MOUNT_SINAI_SUFFIXES,
}

#: The trailing ``- Msq`` on a plan name. Tolerant of the spacing the files
#: actually carry: ``Cigna Hmo/Oap- Snch`` omits a space and
#: ``Aetna Whole Health-Tmsh`` omits both.
# A plain hyphen: no plan name in the corpus uses an en dash (checked across
# all 891 distinct plan names), so allowing one would be speculative.
_SUFFIX = re.compile(r"-\s*([A-Za-z]{2,6})\s*$")


def suffix_of(plan_name: str | None) -> str | None:
    """The trailing abbreviation on a plan name, if there is one."""
    if not plan_name:
        return None
    found = _SUFFIX.search(plan_name.strip())
    return found.group(1).casefold() if found else None


def resolve_facility(
    hospital: str | None,
    location_name: str | None,
    plan_name: str | None,
    *,
    maps: Mapping[str, dict[str, str]] | None = None,
) -> str:
    """The facility a rate belongs to, preferring the plan suffix where it is known.

    Falls back to the file-level location, then the system name. A suffix the map
    does not recognise is ignored rather than guessed at: an unmapped code is
    more likely a product than a hospital, and inventing a facility from it would
    split one hospital's rates across two labels.
    """
    table = (maps if maps is not None else FACILITY_SUFFIX_MAPS).get(hospital or "")
    if table:
        found = table.get(suffix_of(plan_name) or "")
        if found:
            return found
    return (location_name or hospital or "").strip()


def is_multi_facility(hospital: str | None, rows: list[tuple[str | None, str | None]]) -> bool:
    """Whether one file's rows resolve to more than one facility.

    ``rows`` is ``(location_name, plan_name)`` pairs from a single file. Used to
    report the mismatch rather than silently correct it, because a file naming
    one hospital and pricing two is a fact about the disclosure.
    """
    return len({resolve_facility(hospital, loc, plan) for loc, plan in rows}) > 1


__all__ = [
    "FACILITY_SUFFIX_MAPS",
    "MOUNT_SINAI_SUFFIXES",
    "is_multi_facility",
    "resolve_facility",
    "suffix_of",
]
