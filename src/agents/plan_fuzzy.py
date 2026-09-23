"""A2, plan level, first fuzzy pass: aliases and confidence, no model.

:func:`agents.plan_resolution.resolve_plan` matches on product tokens -- HMO,
PPO, EPO, LocalPlus. It says ``unknown`` whenever a side names no product,
and most of the plan space is exactly that:

* UnitedHealthcare's main network is labelled ``ChoicePlus``. The rules read
  that as the uninformative words "choice" and "plus", so it can never match.
* EmblemHealth's 99 payer files are labelled by contract ID --
  ``GHIHOS000001``, ``HIPHOS000091``. The prefix names a product line; no token
  captures it, so every Emblem pair is ``unknown``.
* Empire's ``ConnectionEPO`` is Empire's Connection network; a hospital that
  writes "Empire Connection" names it exactly, and the rules see no product.

This pass reads those names. It is **report-only**: nothing in the mart calls
it, so no reconciliation result moves. It exists to measure what fuzzy plan
matching would buy before anything depends on it -- #70's option (b) keys the
join on plan, and that is only worth building if plans can be matched.

Three tiers, and the middle one is the point:

* **exact** (0.9) -- an alias that names one specific network on both sides.
  Promoted to ``MATCH``.
* **family** (0.6) -- the same product line, not the same contract: a hospital's
  "GHI Access Network" against Emblem's ``GHIHOS000001``. Reported, never a
  match. Matching at family level is what put ``HIP MEDICAID`` against a
  commercial HIP contract in the first prototype.
* **none** -- the rules' verdict stands.

It only ever upgrades an ``unknown``. A rules ``NO_MATCH`` or ``AGGREGATE``
rests on product tokens that disagree or multiply, and a fuzzy pass has no
standing to overrule them. Government products -- Medicare, Medicaid, Child
Health Plus, the Essential Plan -- are never matched: Transparency in Coverage
exempts them, so no commercial network can be their network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from agents.plan_resolution import PlanMatch, PlanVerdict, resolve_plan

#: Confidence at or above which a fuzzy verdict is a ``MATCH``.
MATCH_THRESHOLD = 0.85

EXACT = 0.9
FAMILY = 0.6

#: Phrase -> network. Each names one network on the payer side, and a
#: hospital writing the phrase names that network. Ordered longest first so
#: "open access plus" (Cigna's OAP) is read before "open access" can be.
EXACT_ALIASES: tuple[tuple[str, str], ...] = (
    ("open access managed choice", "managed choice"),
    ("open access elect choice", "elect choice"),
    ("open access plus", "oap"),
    ("health network option", "health network option"),
    ("managed choice", "managed choice"),
    ("elect choice", "elect choice"),
    ("choice plus", "choice plus"),
    ("local plus", "localplus"),
    ("connection", "connection"),
    ("oap", "oap"),
)

#: Payer contract-ID prefix -> product line. A family, not a network.
FAMILY_PREFIXES: dict[str, str] = {"ghihos": "ghi", "hiphos": "hip"}
FAMILY_WORDS = frozenset({"ghi", "hip"})

#: Transparency in Coverage exempts these. Never a commercial network's plan.
_GOVERNMENT = re.compile(
    r"\b(medicare|medicaid|child health plus|chp|schip|essential plan|essential)\b"
)

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_NON_ALPHA = re.compile(r"[^a-z]+")


def normalise(text: str, *, hospital: bool) -> str:
    """Lower case, camel case split, digits and punctuation gone.

    A hospital plan's `` - Msq`` suffix is a facility abbreviation, not part of
    the plan, and is dropped before anything is read from it.
    """
    if hospital:
        text = text.split(" - ")[0]
    spaced = _CAMEL.sub(" ", text).casefold()
    return " " + " ".join(_NON_ALPHA.sub(" ", spaced).split()) + " "


def exact_networks(text: str, *, hospital: bool) -> frozenset[str]:
    norm = normalise(text, hospital=hospital)
    found = set()
    for phrase, network in EXACT_ALIASES:
        if f" {phrase} " in norm:
            found.add(network)
            norm = norm.replace(f" {phrase} ", " ")
    return frozenset(found)


def families(text: str, *, hospital: bool) -> frozenset[str]:
    if hospital:
        return frozenset(w for w in normalise(text, hospital=True).split() if w in FAMILY_WORDS)
    squashed = normalise(text, hospital=False).replace(" ", "")
    return frozenset(v for k, v in FAMILY_PREFIXES.items() if squashed.startswith(k))


def is_government(text: str) -> bool:
    return bool(_GOVERNMENT.search(normalise(text, hospital=True)))


@dataclass(frozen=True)
class FuzzyMatch:
    """A verdict with the evidence and a confidence a reviewer can check."""

    verdict: PlanVerdict
    confidence: float
    tier: str
    reasoning: str
    rules: PlanMatch

    def __bool__(self) -> bool:
        return self.verdict is PlanVerdict.MATCH


def fuzzy_resolve(plan_raw: str | None, network: str | None) -> FuzzyMatch:
    """The rules' verdict, upgraded from ``unknown`` only where a name decides it."""
    rules = resolve_plan(plan_raw, network)
    if rules.verdict is not PlanVerdict.UNKNOWN:
        confidence = 1.0 if rules.verdict is PlanVerdict.MATCH else 0.0
        return FuzzyMatch(rules.verdict, confidence, "rules", rules.reasoning, rules)
    if not plan_raw or not network:
        return FuzzyMatch(PlanVerdict.UNKNOWN, 0.0, "none", "a side is blank", rules)
    if is_government(plan_raw):
        return FuzzyMatch(
            PlanVerdict.UNKNOWN,
            0.0,
            "none",
            "a government product; Transparency in Coverage exempts it",
            rules,
        )

    hospital_nets = exact_networks(plan_raw, hospital=True)
    payer_nets = exact_networks(network, hospital=False)
    shared = hospital_nets & payer_nets
    if shared:
        if len(hospital_nets) > 1:
            return FuzzyMatch(
                PlanVerdict.AGGREGATE,
                EXACT,
                "exact",
                f"the hospital plan names {len(hospital_nets)} networks: "
                f"{', '.join(sorted(hospital_nets))}",
                rules,
            )
        return FuzzyMatch(
            PlanVerdict.MATCH,
            EXACT,
            "exact",
            f"both name the {', '.join(sorted(shared))} network",
            rules,
        )

    shared_family = families(plan_raw, hospital=True) & families(network, hospital=False)
    if shared_family:
        # Deliberately below the threshold: a product line is not a contract.
        return FuzzyMatch(
            PlanVerdict.UNKNOWN,
            FAMILY,
            "family",
            f"same product line ({', '.join(sorted(shared_family))}), contract unknown",
            rules,
        )
    return FuzzyMatch(PlanVerdict.UNKNOWN, 0.0, "none", rules.reasoning, rules)


def resolve_as_rules(plan_raw: str | None, network: str | None) -> PlanMatch:
    """The fuzzy verdict in the rules' shape, so the existing eval can score it."""
    match = fuzzy_resolve(plan_raw, network)
    return PlanMatch(
        match.verdict,
        match.rules.hospital_networks,
        match.rules.payer_networks,
        f"[{match.tier} {match.confidence:.2f}] {match.reasoning}",
    )


__all__ = [
    "EXACT",
    "EXACT_ALIASES",
    "FAMILY",
    "MATCH_THRESHOLD",
    "FuzzyMatch",
    "exact_networks",
    "families",
    "fuzzy_resolve",
    "is_government",
    "normalise",
    "resolve_as_rules",
]
