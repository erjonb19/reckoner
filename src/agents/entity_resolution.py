"""A2: resolve free-text payer/plan strings to canonical contracting parties.

The problem, measured on the real corpus: 300 distinct payer strings across ten
hospitals, collapsing to perhaps forty actual companies. `uhc`, `united
healthcare`, `united` and `oxford` are one contracting party. Worse than
spelling, the *granularity* differs -- one hospital publishes
`Aetna || All Commercial Plans`, another `ANTHEM || Blue Access Large Group` --
so there is no one-to-one mapping to discover.

Design, in the order the guardrails demand:

1. **A deterministic baseline exists and runs first.** `RuleBasedMatcher` is a
   real matcher, not a strawman. An LLM earns its place only by beating it on a
   labelled set, and if it does not, that is a finding to report rather than a
   failure to hide.
2. **The agent proposes; code disposes.** Every proposal is checked by
   `validate_proposal` against evidence from the curated data before it can
   reach a canonical mapping.
3. **Low confidence goes to a human**, and those decisions become labels.
4. **Every call is costed and timed**, so "we used an LLM" has a number
   attached.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from hospital.profile import normalise_name

#: Canonical contracting parties seen in NY hospital MRFs. Deliberately small
#: and hand-maintained: this is the deterministic baseline, and its ceiling is
#: the number the LLM has to beat.
CANONICAL_PAYERS: dict[str, tuple[str, ...]] = {
    "UnitedHealthcare": ("uhc", "united", "united healthcare", "unitedhealthcare", "oxford", "umr"),
    "Aetna": ("aetna", "coventry"),
    "Anthem / Empire BCBS": ("anthem", "empire", "empire bc", "empire bcbs", "bcbs", "blue cross"),
    "EmblemHealth": ("emblem", "emblemhealth", "ghi", "hip", "emblem health hip"),
    "Cigna": ("cigna", "greatwest", "great west"),
    "Healthfirst": ("healthfirst", "health first"),
    "Fidelis Care": ("fidelis", "fidelis care", "centene"),
    "MetroPlus": ("metroplus", "metro plus"),
    "MVP Health Care": ("mvp", "mvp health care"),
    "CDPHP": ("cdphp",),
    "Humana": ("humana",),
    "MultiPlan": ("multiplan", "multi plan", "phcs"),
    "MagnaCare": ("magnacare", "magna"),
    "Affinity": ("affinity",),
    "WellCare": ("wellcare", "well care"),
    "Highmark": ("highmark",),
    "Independent Health": ("independent health",),
    "Excellus BCBS": ("excellus",),
    "Medicare": ("medicare", "mcr"),
    "Medicaid": ("medicaid", "mcd"),
}

_ALIAS_TO_CANONICAL: dict[str, str] = {
    alias: canonical for canonical, aliases in CANONICAL_PAYERS.items() for alias in aliases
}

#: Bracketed internal payer codes, e.g. "HIGHMARK BCBS [514301]".
_CODE = re.compile(r"\[[^\]]*\]|\b\d{4,8}\b")


@dataclass(frozen=True)
class PayerCandidate:
    """One raw payer/plan string, with the evidence behind it."""

    payer_raw: str
    plan_raw: str
    rate_lines: int = 0
    hospitals: tuple[str, ...] = ()
    product_class: str = ""

    @property
    def key(self) -> str:
        return f"{self.payer_raw} || {self.plan_raw}"


@dataclass(frozen=True)
class MatchProposal:
    """A proposed resolution. Never trusted until validated."""

    key: str
    canonical_payer: str | None
    confidence: float
    source: str
    reasoning: str = ""

    @property
    def is_match(self) -> bool:
        return bool(self.canonical_payer)


@dataclass(frozen=True)
class ValidationResult:
    accepted: bool
    reason: str = ""


@dataclass
class CallStats:
    """Per-call cost and latency, so the agent's price is a number not a vibe."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    errors: int = 0

    #: Claude Opus 5 list price, USD per million tokens.
    input_price_per_mtok: float = 5.00
    output_price_per_mtok: float = 25.00

    @property
    def cost_usd(self) -> float:
        return (
            self.input_tokens / 1_000_000 * self.input_price_per_mtok
            + self.output_tokens / 1_000_000 * self.output_price_per_mtok
        )

    @property
    def mean_latency_ms(self) -> float:
        return self.latency_ms / self.calls if self.calls else 0.0

    def record(self, input_tokens: int, output_tokens: int, elapsed_ms: float) -> None:
        self.calls += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.latency_ms += elapsed_ms


class Matcher(Protocol):
    name: str

    def propose(self, candidates: Sequence[PayerCandidate]) -> list[MatchProposal]: ...


def canonical_key(text: str) -> str:
    """Normalise a payer string down to something aliases can be looked up by."""
    cleaned = _CODE.sub(" ", normalise_name(text))
    cleaned = re.sub(
        r"\b(health\s*plan|insurance|ins\s*co|inc|llc|of\s*ny|ny)\b", " ", cleaned, flags=re.I
    )
    return re.sub(r"\s+", " ", cleaned).strip().casefold()


class RuleBasedMatcher:
    """The deterministic baseline: normalise, then look up a hand-built alias map.

    Cheap, instant, explainable, and it handles the easy majority. Whatever the
    LLM adds has to be measured against this, not against nothing.
    """

    name = "rule-based"

    def propose(self, candidates: Sequence[PayerCandidate]) -> list[MatchProposal]:
        proposals = []
        for candidate in candidates:
            key = canonical_key(candidate.payer_raw)
            canonical = _ALIAS_TO_CANONICAL.get(key)
            confidence = 1.0 if canonical else 0.0

            if canonical is None:
                # Fall back to the longest alias contained in the string, which
                # catches "united healthcare of new york" without an entry.
                hits = [
                    (alias, name)
                    for alias, name in _ALIAS_TO_CANONICAL.items()
                    if re.search(rf"\b{re.escape(alias)}\b", key)
                ]
                if hits:
                    canonical = max(hits, key=lambda pair: len(pair[0]))[1]
                    confidence = 0.75

            proposals.append(
                MatchProposal(
                    key=candidate.key,
                    canonical_payer=canonical,
                    confidence=confidence,
                    source=self.name,
                    reasoning="alias table" if canonical else "no alias matched",
                )
            )
        return proposals


MATCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "matches": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "canonical_payer": {
                        "type": ["string", "null"],
                        "description": "Canonical name, or null if genuinely unknown.",
                    },
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "reasoning": {"type": "string", "maxLength": 200},
                },
                "required": ["key", "canonical_payer", "confidence", "reasoning"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["matches"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You resolve free-text payer names from hospital price transparency files to \
canonical contracting parties.

Rules:
- Return a canonical name only from the provided list. Never invent one.
- Return null when the string does not clearly belong to any listed party. A \
null is a correct answer, not a failure; a wrong match is worse than none.
- Judge the payer, not the plan. "Aetna || Medicare Managed Care Plan" and \
"Aetna || All Commercial Plans" are both Aetna.
- Third-party network rentals (MultiPlan, PHCS, MagnaCare) are their own party, \
not the underlying carrier.
- Set confidence below 0.8 whenever a human should check the answer."""


class LlmMatcher:
    """Claude-backed matcher, constrained to the canonical list by schema.

    The client is injected rather than constructed so the matcher is testable
    without network access, and so a caller can swap in a cached or batched
    client without touching this code.
    """

    name = "llm"

    def __init__(
        self,
        client: Any,  # noqa: ANN401 - any object exposing .messages.create
        model: str = "claude-opus-5",
        batch_size: int = 25,
        stats: CallStats | None = None,
    ) -> None:
        self.client = client
        self.model = model
        self.batch_size = batch_size
        self.stats = stats or CallStats()

    def propose(self, candidates: Sequence[PayerCandidate]) -> list[MatchProposal]:
        proposals: list[MatchProposal] = []
        for start in range(0, len(candidates), self.batch_size):
            proposals.extend(self._propose_batch(candidates[start : start + self.batch_size]))
        return proposals

    def _propose_batch(self, batch: Sequence[PayerCandidate]) -> list[MatchProposal]:
        payload = [
            {"key": c.key, "payer": c.payer_raw, "plan": c.plan_raw, "rate_lines": c.rate_lines}
            for c in batch
        ]
        prompt = (
            f"Canonical payers:\n{json.dumps(sorted(CANONICAL_PAYERS), indent=None)}\n\n"
            f"Resolve each of these:\n{json.dumps(payload, indent=None)}"
        )

        started = time.monotonic()
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
                output_config={"format": {"type": "json_schema", "schema": MATCH_SCHEMA}},
            )
        except Exception as exc:  # a matcher failure must not stop the pipeline
            self.stats.errors += 1
            return [
                MatchProposal(c.key, None, 0.0, self.name, f"{type(exc).__name__}: {exc}")
                for c in batch
            ]
        elapsed_ms = (time.monotonic() - started) * 1000

        usage = getattr(response, "usage", None)
        self.stats.record(
            getattr(usage, "input_tokens", 0) or 0,
            getattr(usage, "output_tokens", 0) or 0,
            elapsed_ms,
        )
        return self._parse(response, batch)

    def _parse(
        self,
        response: Any,  # noqa: ANN401 - an SDK Message, or a stub shaped like one
        batch: Sequence[PayerCandidate],
    ) -> list[MatchProposal]:
        text = "".join(
            block.text for block in getattr(response, "content", []) if block.type == "text"
        )
        try:
            payload = json.loads(text)
            rows = payload["matches"]
        except (ValueError, KeyError, TypeError) as exc:
            self.stats.errors += 1
            return [
                MatchProposal(c.key, None, 0.0, self.name, f"unparseable response: {exc}")
                for c in batch
            ]

        by_key = {str(row.get("key")): row for row in rows if isinstance(row, dict)}
        proposals = []
        for candidate in batch:
            row = by_key.get(candidate.key)
            if row is None:
                proposals.append(
                    MatchProposal(candidate.key, None, 0.0, self.name, "absent from response")
                )
                continue
            name = row.get("canonical_payer")
            proposals.append(
                MatchProposal(
                    key=candidate.key,
                    canonical_payer=str(name) if name else None,
                    confidence=float(row.get("confidence") or 0.0),
                    source=self.name,
                    reasoning=str(row.get("reasoning") or "")[:200],
                )
            )
        return proposals


def validate_proposal(proposal: MatchProposal, candidate: PayerCandidate) -> ValidationResult:
    """Deterministic gate. Nothing reaches a canonical mapping without passing.

    Guardrail 1 of the project's agent rules: the agent proposes, code decides.
    """
    if proposal.canonical_payer is None:
        return ValidationResult(False, "no canonical payer proposed")
    if proposal.canonical_payer not in CANONICAL_PAYERS:
        # The schema constrains the shape, not the vocabulary.
        return ValidationResult(False, f"unknown canonical payer {proposal.canonical_payer!r}")
    if not 0.0 <= proposal.confidence <= 1.0:
        return ValidationResult(False, f"confidence out of range: {proposal.confidence}")
    if proposal.key != candidate.key:
        return ValidationResult(False, "proposal does not match the candidate it answers")

    # The proposed party must share a token with the raw string, or the match is
    # a guess dressed as an answer.
    aliases = CANONICAL_PAYERS[proposal.canonical_payer]
    haystack = canonical_key(candidate.payer_raw)
    if not any(alias in haystack or haystack in alias for alias in aliases):
        return ValidationResult(False, "no lexical overlap with the proposed payer")
    return ValidationResult(True)


@dataclass
class Resolution:
    """The outcome for one candidate after proposal, validation and routing."""

    candidate: PayerCandidate
    proposal: MatchProposal
    validation: ValidationResult
    routed_to: str = "accepted"


def resolve(
    candidates: Sequence[PayerCandidate],
    matcher: Matcher,
    review_threshold: float = 0.80,
) -> list[Resolution]:
    """Propose, validate, and route. Anything uncertain goes to a human."""
    proposals = {p.key: p for p in matcher.propose(candidates)}
    resolutions = []
    for candidate in candidates:
        proposal = proposals.get(
            candidate.key,
            MatchProposal(candidate.key, None, 0.0, matcher.name, "no proposal returned"),
        )
        validation = validate_proposal(proposal, candidate)
        uncertain = not validation.accepted or proposal.confidence < review_threshold
        routed = "review" if uncertain else "accepted"
        resolutions.append(Resolution(candidate, proposal, validation, routed))
    return resolutions


def candidates_from_pairs(
    pairs: dict[str, int], hospitals: dict[str, tuple[str, ...]] | None = None
) -> list[PayerCandidate]:
    """Build candidates from the `payer || plan` counts the profiler produces."""
    hospitals = hospitals or {}
    candidates = []
    for key, count in pairs.items():
        payer, _, plan = key.partition(" || ")
        candidates.append(
            PayerCandidate(
                payer_raw=payer,
                plan_raw=plan,
                rate_lines=count,
                hospitals=hospitals.get(key, ()),
            )
        )
    return sorted(candidates, key=lambda c: -c.rate_lines)


__all__ = [
    "CANONICAL_PAYERS",
    "CallStats",
    "LlmMatcher",
    "MatchProposal",
    "Matcher",
    "PayerCandidate",
    "Resolution",
    "RuleBasedMatcher",
    "ValidationResult",
    "candidates_from_pairs",
    "canonical_key",
    "resolve",
    "validate_proposal",
]
