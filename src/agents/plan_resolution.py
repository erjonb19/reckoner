"""A2, plan level: does a hospital's plan name mean the payer's network?

The payer-level matcher in :mod:`agents.entity_resolution` answers "is this the
same company". This answers the next question, which is where the variance mart
actually stalls: **is this the same contract**. Without it every cross-source
pair is ``plan_unresolved`` -- 70.9% of Mount Sinai's comparable pairs, and the
sole reason ``unexplained`` is empty.

The two sides name plans in different ways, and the difference decides whether
matching is possible at all:

* **By product.** Mount Sinai writes ``Cigna Localplus - Msq``: carrier, product,
  then an abbreviation of the *facility* (``Msq`` is Mount Sinai Queens). The
  product is exactly what a payer file's network label carries, so the two are
  directly comparable and no external data is needed.
* **By employer.** NYU Langone writes ``SCREEN ACTORS GUILD 1220``. That names
  who bought the plan, not what it is. Nothing in the payer Parquet can match it,
  because plan identity is discarded upstream -- recovering it means going back
  to the payer's index, which is a separate piece of work.

So this module handles the product case and says ``unknown`` for the rest,
rather than guessing. An unresolved plan is not a finding; it is an admission.

Three verdicts, and the middle one matters most:

* ``MATCH`` -- both name the same network. The pair may be differenced.
* ``AGGREGATE`` -- the hospital named several networks in one plan, as Aetna's
  ``Hmo/Pos/Ppo`` does. One published rate covers three products, so it cannot be
  attributed to the payer's single network. That is a real granularity mismatch
  and a fact about the hospital's disclosure, not a failure to match.
* ``NO_MATCH`` / ``UNKNOWN`` -- different networks, or not enough to tell.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

#: Product tokens that identify a network. Qualifiers a payer bolts on -- the
#: ``National`` in ``NationalPPO``, the ``Choice`` in ``ChoicePlus`` -- are
#: deliberately absent: they narrow a network without naming a different kind of
#: one, and matching on them would split ``Cigna Ppo`` from ``NationalPPO``.
PRODUCT_TOKENS = frozenset(
    {
        "hmo",
        "ppo",
        "epo",
        "pos",
        "oap",
        "localplus",
        "indemnity",
        "openaccess",
    }
)

#: Spellings that mean one of the tokens above. ``oap`` is Cigna's Open Access
#: Plus and ``openaccess`` is Aetna's Open Access family; they are kept apart
#: because they are different carriers' products, and a carrier check already
#: gates the comparison.
_SYNONYMS = {
    "localplus": "localplus",
    "local plus": "localplus",
    "openaccess": "openaccess",
    "open access": "openaccess",
    "oap": "oap",
}

#: A word that names no network at all. A plan called only "Commercial" says the
#: market, not the contract, so it cannot be matched to one network out of six.
_UNINFORMATIVE = frozenset(
    {
        "commercial",
        "all",
        "payer",
        "allpayer",
        "student",
        "health",
        "savings",
        "plus",
        "rates",
        "whole",
        "signature",
        "administrators",
        "tpa",
        "national",
        "choice",
        "select",
        "network",
        "option",
        "elect",
        "managed",
        "insurance",
        "company",
    }
)

#: Split camel case both ways the payer labels use it: ``NationalPPO`` at the
#: lower-to-upper boundary, and ``POSChoicePlus`` where an all-caps product
#: runs straight into a capitalised qualifier.
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
#: Digits are separators, not content: they are employer codes on the hospital
#: side (``SCREEN ACTORS GUILD 1220``) and plan variants on the payer side
#: (``ChoiceEPO50``), and neither names a network.
_SPLIT = re.compile(r"[^a-z]+")


class PlanVerdict(StrEnum):
    """Whether a hospital plan and a payer network are the same contract."""

    MATCH = "match"
    AGGREGATE = "aggregate"
    NO_MATCH = "no_match"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PlanMatch:
    """The verdict, with the tokens that produced it so a reviewer can check."""

    verdict: PlanVerdict
    hospital_networks: frozenset[str]
    payer_networks: frozenset[str]
    reasoning: str = ""

    def __bool__(self) -> bool:
        return self.verdict is PlanVerdict.MATCH


def network_tokens(text: str | None) -> frozenset[str]:
    """The product tokens a plan or network label names.

    Handles the two spellings the sources actually use: a hospital's
    ``Hmo/Oap`` separated by punctuation, and a payer's ``NationalPPO`` run
    together in camel case.
    """
    if not text:
        return frozenset()
    expanded = _CAMEL.sub(" ", text).casefold()
    for phrase, token in _SYNONYMS.items():
        if phrase in expanded:
            expanded = expanded.replace(phrase, f" {token} ")
    words = [w for w in _SPLIT.split(expanded) if w and w not in _UNINFORMATIVE]
    return frozenset(w for w in words if w in PRODUCT_TOKENS)


def resolve_plan(plan_raw: str | None, network: str | None) -> PlanMatch:
    """Decide whether a hospital plan names the payer's network.

    The order matters. A hospital plan naming several networks is reported as an
    aggregate *even when* one of them is the payer's, because a single published
    rate covering HMO, POS and PPO cannot be said to be the PPO rate. Treating
    that as a match would manufacture a variance out of the hospital's own
    aggregation.
    """
    hospital = network_tokens(plan_raw)
    payer = network_tokens(network)

    if not hospital or not payer:
        return PlanMatch(
            PlanVerdict.UNKNOWN,
            hospital,
            payer,
            "a side names no network product",
        )

    if len(hospital) > 1:
        return PlanMatch(
            PlanVerdict.AGGREGATE,
            hospital,
            payer,
            f"the hospital plan covers {len(hospital)} networks: {', '.join(sorted(hospital))}",
        )

    shared = hospital & payer
    if shared:
        return PlanMatch(
            PlanVerdict.MATCH, hospital, payer, f"both name {', '.join(sorted(shared))}"
        )
    return PlanMatch(
        PlanVerdict.NO_MATCH,
        hospital,
        payer,
        f"{', '.join(sorted(hospital))} against {', '.join(sorted(payer))}",
    )


__all__ = [
    "PRODUCT_TOKENS",
    "PlanMatch",
    "PlanVerdict",
    "network_tokens",
    "resolve_plan",
]
