"""A3: when a hospital file yields nothing, say which kind of nothing.

The ingest records a file that produced no curated rows as ``status="empty"``
with the error "no curated rows produced". That sentence covers two situations
which need opposite responses:

* **The hospital published no negotiated rates.** Mount Sinai Brooklyn is 82 MB
  of gross charges and cash prices with not one ``payers_information`` entry
  anywhere in it. There is nothing to fix; the file is complete and useless to
  a reconciliation, and any adapter written for it would be written for data
  that does not exist.
* **We could not find the rates.** A file laid out in a way the parser does not
  recognise looks identical from the outside -- same status, same message, same
  zero.

Telling those apart took a full 82 MB download and six probes by hand. This is
that determination, made from what the parse already observed, so it costs
nothing and lands in the audit where the question gets asked.

**The generative half of A3 is not built**, and that is the build order rather
than an omission. CLAUDE.md puts deterministic implementation first and the
agent second, once the tables show where the long tail is. The tables now say
the long tail is *empty*: all twelve systems parse into one of three known
layouts, and the only two files that yield nothing yield nothing correctly. An
adapter generator today would have no non-conforming file to be tested against,
which is precisely the condition guardrail 1 exists to prevent -- generated code
that no deterministic check can validate.

What this does instead is produce the evidence such a generator would need, as a
structured artifact rather than prose, and route the case that has never yet
occurred to a review queue instead of a guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

#: Layouts the parser recognises today. A file that matches none of them is the
#: case A3's generative half would exist for, and has not yet been observed.
KNOWN_LAYOUTS = frozenset({"json", "csv-tall", "csv-wide"})


class Conformance(StrEnum):
    """What a file's emptiness means."""

    #: Rates came out. Nothing to diagnose.
    CONFORMS = "conforms"
    #: The structure was found and read, and carries no payer-specific rates at
    #: all. The file is complete; it is simply gross and cash only.
    NO_NEGOTIATED_RATES = "no_negotiated_rates"
    #: Rates were found and every one of them was filtered or rejected. A data
    #: question, not a layout one -- look at the reject reasons.
    ALL_ROWS_REJECTED = "all_rows_rejected"
    #: The container the parser reads was never found. This is the adapter case.
    STRUCTURE_NOT_FOUND = "structure_not_found"
    #: The read ended mid-document, so any verdict would be about a fragment.
    TRUNCATED = "truncated"
    #: None of the above fits. Goes to a human, and would be the agent's input.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Observation:
    """What one parse saw. Every field is already collected during ingest."""

    layout: str
    structure_found: bool
    items_seen: int
    rates_yielded: int
    rows_kept: int
    rows_rejected: int = 0
    truncated: bool = False
    bytes_read: int = 0


@dataclass(frozen=True)
class Diagnosis:
    """A testable artifact, not a sentence. Guardrail 3."""

    conformance: Conformance
    reason: str
    #: True when a human or an adapter has to do something about it.
    actionable: bool

    @property
    def needs_review(self) -> bool:
        return self.conformance is Conformance.UNKNOWN

    def describe(self) -> str:
        return f"{self.conformance}: {self.reason}"


def diagnose(observation: Observation) -> Diagnosis:
    """Explain an empty or thin parse from what the parse already saw.

    Order matters. Truncation is checked first because every other verdict would
    otherwise be a claim about a fragment of a document.
    """
    o = observation

    if o.rows_kept > 0:
        return Diagnosis(
            Conformance.CONFORMS,
            f"{o.rows_kept:,} rows curated from {o.rates_yielded:,} parsed",
            actionable=False,
        )

    if o.truncated:
        return Diagnosis(
            Conformance.TRUNCATED,
            "the read ended mid-document, so emptiness may be an artifact of the cap",
            actionable=True,
        )

    if o.layout not in KNOWN_LAYOUTS:
        return Diagnosis(
            Conformance.STRUCTURE_NOT_FOUND,
            f"layout {o.layout!r} is not one this parser recognises; an adapter is needed",
            actionable=True,
        )

    if not o.structure_found:
        return Diagnosis(
            Conformance.STRUCTURE_NOT_FOUND,
            (
                f"read {o.bytes_read:,} bytes as {o.layout} and never found the container "
                "rates live in; the file is laid out in a way this parser does not recognise"
            ),
            actionable=True,
        )

    if o.rates_yielded == 0:
        # The structure was found and walked, and produced no rate. That is the
        # hospital's disclosure being gross-and-cash only, not a parser failure.
        return Diagnosis(
            Conformance.NO_NEGOTIATED_RATES,
            (
                f"walked {o.items_seen:,} charge items and found no payer-specific rate; "
                "the hospital published gross and cash prices only, so there is nothing "
                "here to reconcile and nothing to fix"
            ),
            actionable=False,
        )

    if o.rows_rejected > 0:
        return Diagnosis(
            Conformance.ALL_ROWS_REJECTED,
            (
                f"{o.rates_yielded:,} rates parsed and all {o.rows_rejected:,} were rejected; "
                "a data question rather than a layout one -- see the reject reasons"
            ),
            actionable=True,
        )

    return Diagnosis(
        Conformance.UNKNOWN,
        (
            f"{o.rates_yielded:,} rates parsed from {o.items_seen:,} items but none kept and "
            "none rejected; no rule covers this"
        ),
        actionable=True,
    )


__all__ = [
    "KNOWN_LAYOUTS",
    "Conformance",
    "Diagnosis",
    "Observation",
    "diagnose",
]
