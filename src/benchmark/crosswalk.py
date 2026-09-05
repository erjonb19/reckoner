"""Resolve an MRF location to the CMS facility that Medicare prices it as.

A hospital MRF names its location in the hospital's own words -- ``NYU
Langone|Tisch Hospital``, ``NewYork-Presbyterian Columbia University Irving
Medical Center`` -- while every CMS benchmark is keyed on a CCN. Nothing in
either file joins them, so the link has to be resolved.

Two facts make this harder than a lookup, and both were measured on the corpus
rather than assumed:

* **A health system is not a facility.** ``Mount Sinai Health System`` is five
  CCNs; NYC Health + Hospitals publishes twelve MRFs, one per hospital. Matching
  at the system level and attaching one wage index to all of it would price
  Bellevue at Elmhurst's geography.
* **The corpus crosses state lines.** Northwell's file set includes Danbury
  Hospital, which is in Connecticut, because Northwell acquired Nuvance.
  Restricting the search to NY would silently drop it.

The same discipline as the A2 matcher applies here: a deterministic scorer
proposes, a threshold routes anything uncertain to a human, and accepted
decisions are persisted so they are made once. An unresolved facility is
returned as unresolved -- never as a nearest guess, because a wrong CCN produces
a wage index that is plausible and wrong.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

#: Corporate and legal noise that carries no identifying signal.
_NOISE = re.compile(
    r"\b(hospital|hospitals|medical|center|centre|health|healthcare|system|systems"
    r"|the|of|at|and|inc|llc|corp|campus|division|dba)\b",
    re.I,
)

#: MRFs pack a system and a site into one cell with a pipe or dash.
_SEPARATORS = re.compile(r"[|/\\,;:]+")

#: Below this a proposal is a guess, not a match, and goes to a human.
REVIEW_THRESHOLD = 0.72

#: A match this far below the runner-up is ambiguous even if it scores well:
#: "Mount Sinai West" and "Mount Sinai South Nassau" differ in one token.
AMBIGUITY_MARGIN = 0.08


@dataclass(frozen=True)
class Facility:
    """One CMS-certified facility, from Hospital General Information."""

    ccn: str
    name: str
    city: str = ""
    state: str = ""
    county: str = ""
    hospital_type: str = ""
    ownership: str = ""

    @property
    def is_acute(self) -> bool:
        """Only acute care hospitals are priced by IPPS and OPPS."""
        return "acute" in self.hospital_type.casefold()


@dataclass(frozen=True)
class CrosswalkMatch:
    """A proposed location -> CCN resolution, with the evidence for it."""

    location: str
    ccn: str | None
    facility_name: str | None
    score: float
    method: str
    runner_up: str | None = None
    runner_up_score: float = 0.0

    @property
    def is_confident(self) -> bool:
        clear = self.score - self.runner_up_score >= AMBIGUITY_MARGIN
        return bool(self.ccn) and self.score >= REVIEW_THRESHOLD and clear

    @property
    def routed_to(self) -> str:
        return "accepted" if self.is_confident else "review"


#: Hospitals write their own name closed up where CMS spaces it:
#: "NewYork-Presbyterian" against "NEW YORK-PRESBYTERIAN". Splitting on the
#: lowercase-to-uppercase boundary before casefolding recovers the words, and
#: without it the two share no token at all and the site name alone decides the
#: match -- which is how "NewYork-Presbyterian Queens" resolves to the
#: unrelated "Queens Hospital Center".
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z])(?=[A-Z])")


def normalise_facility_name(value: str | None) -> str:
    """Reduce a facility name to its identifying tokens.

    Separators are flattened first: ``NYU Langone|Tisch Hospital`` must become
    two words, not one, or the site name never matches anything.
    """
    text = _CAMEL_BOUNDARY.sub(" ", value or "")
    text = _SEPARATORS.sub(" ", text)
    text = re.sub(r"[^\w\s]+", " ", text)
    text = _NOISE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip().casefold()


def _tokens(value: str) -> set[str]:
    return {t for t in normalise_facility_name(value).split() if len(t) > 1}


#: Words dropped even when comparing names exactly. Deliberately tiny: "the"
#: and "of" carry nothing, but "hospital" and "center" are what separate a
#: system from one of its facilities and must survive.
_ARTICLES = re.compile(r"\b(the|of|at|and|a)\b", re.I)


def exact_key(value: str | None) -> str:
    """A name reduced only by punctuation and articles, for exact comparison.

    Distinct from :func:`normalise_facility_name`, which strips "hospital" and
    "center" as noise. That stripping is right for scoring and wrong for
    identity: it makes the system "Mount Sinai" identical to the facility
    "Mount Sinai Hospital".
    """
    text = _CAMEL_BOUNDARY.sub(" ", value or "")
    text = _SEPARATORS.sub(" ", text)
    text = re.sub(r"[^\w\s]+", " ", text)
    text = _ARTICLES.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip().casefold()


def score_names(location: str, facility: str) -> float:
    """Similarity of two facility names, on their identifying tokens only.

    Symmetric Jaccard, deliberately. Weighting by containment against the
    shorter name looks kinder to a correct match, but it scores a name that
    *drops* the distinguishing token as perfect: "Mount Sinai Morningside"
    against "Mount Sinai Hospital" contains all of the latter's tokens and would
    rank 1.0. Every token that appears on only one side has to cost something,
    because that token is usually the whole difference between two hospitals in
    the same system.
    """
    left, right = _tokens(location), _tokens(facility)
    if not left or not right:
        return 0.0
    overlap = left & right
    if not overlap:
        return 0.0
    return round(len(overlap) / len(left | right), 4)


def load_cms_facilities(path: Path, *, acute_only: bool = True) -> list[Facility]:
    """Read CMS Hospital General Information into facilities.

    ``acute_only`` keeps the facilities IPPS and OPPS actually price. Critical
    access, psychiatric and VA hospitals are paid under other systems, so
    matching to one would attach a benchmark that does not apply.
    """
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    facilities = []
    for row in rows:
        ccn = (row.get("Facility ID") or "").strip()
        name = (row.get("Facility Name") or "").strip()
        if not ccn or not name:
            continue
        facility = Facility(
            ccn=ccn,
            name=name,
            city=(row.get("City/Town") or "").strip(),
            state=(row.get("State") or "").strip(),
            county=(row.get("County/Parish") or "").strip(),
            hospital_type=(row.get("Hospital Type") or "").strip(),
            ownership=(row.get("Hospital Ownership") or "").strip(),
        )
        if acute_only and not facility.is_acute:
            continue
        facilities.append(facility)
    return facilities


def match_location(
    location: str,
    facilities: list[Facility],
    *,
    states: tuple[str, ...] = (),
) -> CrosswalkMatch:
    """Propose the CMS facility a location refers to.

    ``states`` narrows the search where the caller knows it is safe. It is not
    defaulted to NY: the corpus contains a Connecticut hospital, and a filter
    that quietly excludes it would report that facility as unmatched forever.
    """
    pool = [f for f in facilities if not states or f.state in states]
    if not pool or not normalise_facility_name(location):
        return CrosswalkMatch(location, None, None, 0.0, "no candidates")

    # An exact name match settles it. Scoring cannot, because the tokens that
    # distinguish a system from its flagship are the ones scoring discards.
    wanted = exact_key(location)
    exact = [f for f in pool if exact_key(f.name) == wanted]
    if len(exact) == 1:
        return CrosswalkMatch(location, exact[0].ccn, exact[0].name, 1.0, "exact name")

    scored = sorted(
        ((score_names(location, f.name), f) for f in pool),
        key=lambda pair: (-pair[0], pair[1].ccn),
    )
    best_score, best = scored[0]
    runner_score, runner = scored[1] if len(scored) > 1 else (0.0, None)

    if best_score <= 0:
        return CrosswalkMatch(location, None, None, 0.0, "no token overlap")

    # A name whose every token also appears in several other facilities is a
    # system name, not a facility name: "Mount Sinai" fits Mount Sinai Hospital,
    # Mount Sinai West and Mount Sinai Beth Israel equally. Resolving it to the
    # flagship would attach one hospital's geography to a whole system.
    location_tokens = _tokens(location)
    supersets = [f for f in pool if location_tokens and location_tokens <= _tokens(f.name)]
    if len(supersets) > 1:
        return CrosswalkMatch(
            location=location,
            ccn=best.ccn,
            facility_name=best.name,
            score=best_score,
            method=f"under-specified: fits {len(supersets)} facilities",
            runner_up=runner.name if runner else None,
            # Reported as a tie so the ambiguity, not the score, decides routing.
            runner_up_score=best_score,
        )

    return CrosswalkMatch(
        location=location,
        ccn=best.ccn,
        facility_name=best.name,
        score=best_score,
        method="token-overlap",
        runner_up=runner.name if runner else None,
        runner_up_score=runner_score,
    )


@dataclass
class Crosswalk:
    """Resolved location -> CCN decisions, persisted so they are made once.

    Confirmed entries are authoritative and are never re-scored: a human
    decision outranks the scorer, which is the whole point of having a review
    queue.
    """

    confirmed: dict[str, str] = field(default_factory=dict)
    #: Locations a human ruled unmatchable, kept so they stop returning to the
    #: queue. A facility with no CCN is a finding, not an unfinished task.
    rejected: set[str] = field(default_factory=set)

    def resolve(
        self,
        location: str,
        facilities: list[Facility],
        *,
        states: tuple[str, ...] = (),
    ) -> CrosswalkMatch:
        key = location.strip()
        if key in self.confirmed:
            ccn = self.confirmed[key]
            name = next((f.name for f in facilities if f.ccn == ccn), None)
            return CrosswalkMatch(key, ccn, name, 1.0, "confirmed")
        if key in self.rejected:
            return CrosswalkMatch(key, None, None, 0.0, "confirmed unmatchable")
        return match_location(key, facilities, states=states)

    def confirm(self, location: str, ccn: str) -> None:
        self.confirmed[location.strip()] = ccn.strip()
        self.rejected.discard(location.strip())

    def reject(self, location: str) -> None:
        self.rejected.add(location.strip())
        self.confirmed.pop(location.strip(), None)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for location, ccn in sorted(self.confirmed.items()):
                handle.write(
                    json.dumps({"location": location, "ccn": ccn}, ensure_ascii=False) + "\n"
                )
            for location in sorted(self.rejected):
                handle.write(
                    json.dumps({"location": location, "ccn": None}, ensure_ascii=False) + "\n"
                )

    @classmethod
    def load(cls, path: Path) -> Crosswalk:
        crosswalk = cls()
        if not path.exists():
            return crosswalk
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                row = json.loads(line)
                location = str(row.get("location", ""))
                ccn = row.get("ccn")
                if ccn:
                    crosswalk.confirm(location, str(ccn))
                else:
                    crosswalk.reject(location)
        return crosswalk


@dataclass
class CrosswalkReport:
    """What resolving a whole corpus produced, including what it could not."""

    matches: list[CrosswalkMatch] = field(default_factory=list)

    @property
    def accepted(self) -> list[CrosswalkMatch]:
        return [m for m in self.matches if m.routed_to == "accepted"]

    @property
    def review(self) -> list[CrosswalkMatch]:
        return [m for m in self.matches if m.routed_to == "review"]

    @property
    def coverage(self) -> float:
        return len(self.accepted) / len(self.matches) if self.matches else 0.0

    def to_jsonl(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for match in sorted(self.matches, key=lambda m: -m.score):
                row = asdict(match)
                row["routed_to"] = match.routed_to
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def resolve_all(
    locations: list[str],
    facilities: list[Facility],
    crosswalk: Crosswalk | None = None,
    *,
    states: tuple[str, ...] = (),
) -> CrosswalkReport:
    """Resolve every distinct location, routing the uncertain ones to review."""
    crosswalk = crosswalk or Crosswalk()
    seen: dict[str, CrosswalkMatch] = {}
    for location in locations:
        key = location.strip()
        if key and key not in seen:
            seen[key] = crosswalk.resolve(key, facilities, states=states)
    return CrosswalkReport(list(seen.values()))
