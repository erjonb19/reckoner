"""A1's agent half: propose a cause for a residual finding, and let code decide.

The deterministic half (:mod:`pipeline.triage`) already accounts for most of the
queue with five near-miss rules. What it cannot do is say *why* a finding it
labels ``unexplained`` disagrees -- a unit mismatch, a carve-out, a plan that is
not the one it appears to be, or a genuine difference between two correct
filings. That is a judgement over a row's fields, which is what a model is for.

This module is the loop around that judgement, built to the four guardrails in
CLAUDE.md before any label exists to tune it against:

1. **The agent proposes, code validates.** :func:`validate` checks every
   proposal against the row it answers: the cause must be in the vocabulary,
   the evidence it cites must be fields the row has, and a cause with a
   checkable precondition must meet it -- a vintage artifact needs a vintage
   gap, a units mismatch needs a ratio large enough to be one.
2. **Nothing it produces is executed.** Output is a classification, not code.
3. **Every action is an artifact.** A :class:`Proposal` or a human-queue row,
   never prose. The ``reasoning`` field is capped and is not what is scored.
4. **Bounded retries, a human queue as the terminal path, and a cost and a
   latency on every call.** :class:`CallLog` holds one record per attempt,
   including the ones that failed, because a failed call is still billed.

**No model has been run against real data.** The client is injected, the tests
drive it with a stub, and the eval harness in :mod:`agents.triage_evals` reports
"not measured" until a labelled set exists. The first real run is a decision
for a human, not a default.
"""

from __future__ import annotations

import csv
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

#: The model the agent calls unless told otherwise. Same as A2's LLM matcher.
DEFAULT_MODEL = "claude-opus-5"

#: USD per million tokens, input and output. First-party list prices; a model
#: not in the table is costed as unknown rather than as free.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

#: Attempts per finding, including the first. A finding that fails this many
#: times goes to a human; it does not go round again.
MAX_ATTEMPTS = 3

#: Below this a validated proposal is still routed to a human.
REVIEW_THRESHOLD = 0.80

#: A vintage artifact is only a checkable claim when the two sides are at least
#: this far apart. Well below the deterministic rule's 180 days, because the
#: agent is allowed to argue a smaller gap -- it is not allowed to argue none.
MIN_VINTAGE_GAP_FOR_ARTIFACT = 30

#: A units or methodology mismatch -- per diem against case rate, per unit
#: against per service -- produces ratios of several times, not a few percent.
MIN_RATIO_FOR_UNITS = 3.0

#: The fields that identify one finding. The same fields key the label file, so
#: a label can be joined to the queue row it describes.
KEY_FIELDS: tuple[str, ...] = (
    "system",
    "facility",
    "carrier",
    "code",
    "code_type",
    "setting",
    "billing_class",
    "hospital_plan",
    "payer_plan",
)

#: The fields the model is shown. Deliberately excludes the deterministic rule's
#: verdict: an agent shown the baseline's answer is scored on agreeing with it.
EVIDENCE_FIELDS: tuple[str, ...] = (
    *KEY_FIELDS,
    "hospital_rate",
    "payer_rate",
    # The carrier's distribution the hospital rate was compared against
    # (ADR 0006). payer_rate is its median.
    "payer_min",
    "payer_max",
    "payer_count",
    "inside_payer_range",
    "ratio",
    "relative_difference",
    "hospital_vintage",
    "payer_vintage",
    "vintage_gap_days",
    "notes",
)


class Cause(StrEnum):
    """Why a hospital and a payer disagree about one negotiated rate."""

    #: The two rates are expressed differently: per diem against case rate,
    #: per unit against per service, a percentage against a dollar amount.
    UNITS_OR_METHODOLOGY = "units_or_methodology"
    #: The files are far enough apart in time for the contract to have moved.
    VINTAGE_ARTIFACT = "vintage_artifact"
    #: A base-rate difference across the contract, not a fact about this code.
    CONTRACT_OFFSET = "contract_offset"
    #: The hospital's plan and the payer's network are not the same contract.
    PLAN_MISMATCH = "plan_mismatch"
    #: Setting, billing class or modifier differ in a way the join did not see.
    SETTING_OR_MODIFIER = "setting_or_modifier"
    #: One side's value is wrong on its face: a placeholder, a typo, a $0.01.
    DATA_ERROR = "data_error"
    #: Both filings look right and describe the same contract. The finding.
    GENUINE_DISAGREEMENT = "genuine_disagreement"
    #: The fields do not support any of the above. An abstention, not an error.
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


CAUSES: tuple[str, ...] = tuple(str(c) for c in Cause)


def item_key(row: dict[str, Any]) -> str:
    """One finding's identity, stable across runs of the same gold."""
    return " | ".join(str(row.get(name) or "").strip() for name in KEY_FIELDS)


@dataclass(frozen=True)
class Proposal:
    """What the agent says about one finding. Never trusted until validated."""

    key: str
    cause: str
    confidence: float
    evidence: tuple[str, ...] = ()
    reasoning: str = ""
    source: str = "llm"

    @property
    def abstains(self) -> bool:
        return self.cause == Cause.INSUFFICIENT_EVIDENCE


@dataclass(frozen=True)
class Verdict:
    accepted: bool
    reason: str = ""


def _as_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def validate(proposal: Proposal, row: dict[str, Any]) -> Verdict:
    """The deterministic gate. Guardrail 1: the agent proposes, code decides.

    Checks shape first, then the claims a row can actually confirm. A cause
    with no checkable precondition passes on shape alone; that is a limit of
    the evidence, and the eval is what measures whether those are right.
    """
    if proposal.key != item_key(row):
        return Verdict(False, "proposal does not answer this finding")
    if proposal.cause not in CAUSES:
        # The schema constrains the shape; only this constrains the vocabulary.
        return Verdict(False, f"unknown cause {proposal.cause!r}")
    if not 0.0 <= proposal.confidence <= 1.0:
        return Verdict(False, f"confidence out of range: {proposal.confidence}")
    unknown = [name for name in proposal.evidence if name not in EVIDENCE_FIELDS]
    if unknown:
        return Verdict(False, f"cites fields the finding does not have: {', '.join(unknown)}")
    if not proposal.evidence and not proposal.abstains:
        return Verdict(False, "a cause with no cited evidence is a guess")

    if proposal.cause == Cause.VINTAGE_ARTIFACT:
        gap = _as_float(row.get("vintage_gap_days"))
        if gap is None:
            return Verdict(False, "vintage artifact claimed, but the vintage gap is unknown")
        if gap < MIN_VINTAGE_GAP_FOR_ARTIFACT:
            return Verdict(False, f"vintage artifact claimed across a {gap:.0f}-day gap")

    if proposal.cause == Cause.UNITS_OR_METHODOLOGY:
        ratio = _as_float(row.get("ratio"))
        if not ratio:
            return Verdict(False, "units mismatch claimed, but the ratio is unknown")
        if max(ratio, 1 / ratio) < MIN_RATIO_FOR_UNITS:
            return Verdict(False, f"units mismatch claimed at a ratio of {ratio:.2f}")

    if proposal.cause == Cause.PLAN_MISMATCH and not (
        str(row.get("hospital_plan") or "").strip() or str(row.get("payer_plan") or "").strip()
    ):
        return Verdict(False, "plan mismatch claimed, but neither side names a plan")

    return Verdict(True)


@dataclass(frozen=True)
class CallRecord:
    """One attempt, successful or not. Guardrail 4: every call has a price."""

    key: str
    attempt: int
    model: str
    outcome: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = 0.0
    latency_ms: float = 0.0
    detail: str = ""
    recorded_at: str = ""


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """What one call cost, or ``None`` for a model with no known price.

    ``None`` rather than zero: a total that silently counts an unpriced model
    as free is a total that is wrong in the one direction nobody checks.
    """
    prices = PRICES_PER_MTOK.get(model)
    if prices is None:
        return None
    return input_tokens / 1_000_000 * prices[0] + output_tokens / 1_000_000 * prices[1]


@dataclass
class CallLog:
    records: list[CallRecord] = field(default_factory=list)

    def add(self, record: CallRecord) -> None:
        self.records.append(record)

    @property
    def calls(self) -> int:
        return len(self.records)

    @property
    def cost_usd(self) -> float | None:
        costs = [r.cost_usd for r in self.records]
        if any(c is None for c in costs):
            return None
        return sum(c for c in costs if c is not None)

    @property
    def mean_latency_ms(self) -> float:
        return sum(r.latency_ms for r in self.records) / self.calls if self.calls else 0.0

    def summary(self) -> dict[str, Any]:
        outcomes: dict[str, int] = {}
        for record in self.records:
            outcomes[record.outcome] = outcomes.get(record.outcome, 0) + 1
        return {
            "calls": self.calls,
            "input_tokens": sum(r.input_tokens for r in self.records),
            "output_tokens": sum(r.output_tokens for r in self.records),
            "cost_usd": None if self.cost_usd is None else round(self.cost_usd, 6),
            "mean_latency_ms": round(self.mean_latency_ms, 1),
            "outcomes": outcomes,
        }

    def append_to(self, path: Path) -> None:
        """Append, never overwrite: spend is only meaningful as a history."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for record in self.records:
                handle.write(json.dumps(asdict(record)) + "\n")


@dataclass(frozen=True)
class Outcome:
    """Where one finding ended up, and why."""

    key: str
    routed: str  # "accepted" or "human"
    reason: str
    attempts: int
    proposal: Proposal | None = None

    @property
    def accepted(self) -> bool:
        return self.routed == "accepted"


OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cause": {"type": "string", "enum": list(CAUSES)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence": {
            "type": "array",
            "items": {"type": "string", "enum": list(EVIDENCE_FIELDS)},
            "description": "The fields of the finding that support the cause.",
        },
        "reasoning": {"type": "string", "maxLength": 300},
    },
    "required": ["cause", "confidence", "evidence", "reasoning"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You classify why a hospital's published negotiated rate and an insurer's \
published negotiated rate disagree, for what the join believes is the same \
service, facility, carrier, setting and billing class.

The hospital file (45 CFR 180) covers every payer and plan the hospital \
contracts with; the insurer file (Transparency in Coverage) covers commercial \
group and individual plans only. Hospital files update about once a year and \
insurer files monthly, so the two can describe different moments in a contract.

Choose one cause:
- units_or_methodology: the rates are expressed differently (per diem vs case \
rate, per unit vs per service, percentage vs dollars). Expect a ratio of \
several times.
- vintage_artifact: the files are far enough apart in time for the contract to \
have changed. Cite the vintages.
- contract_offset: a base-rate difference across the whole contract rather \
than a fact about this code.
- plan_mismatch: the hospital's plan and the insurer's network are probably \
not the same contract.
- setting_or_modifier: setting, billing class or a modifier differ in a way \
the fields suggest the join missed.
- data_error: one side's value is wrong on its face.
- genuine_disagreement: both look right and describe the same contract.
- insufficient_evidence: the fields do not support any of the above.

insufficient_evidence is a correct answer, not a failure; a confident wrong \
cause is worse than an abstention. Cite only fields you were given. Set \
confidence below 0.8 whenever a person should check the answer."""


class ApiFailure(Exception):
    """A call that did not return a usable response, and whether to try again."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


def is_retryable(exc: BaseException) -> bool:
    """Rate limits, server errors and dropped connections; nothing else.

    Classified by shape rather than by the SDK's exception classes so the loop
    runs, and is tested, without the SDK installed. A 4xx other than 408, 409
    and 429 is the request's fault and will fail identically on every retry.
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status in (408, 409, 429) or status >= 500
    name = type(exc).__name__
    return "Connection" in name or "Timeout" in name


def _prompt(row: dict[str, Any], feedback: str) -> str:
    evidence = {name: row.get(name) for name in EVIDENCE_FIELDS}
    text = f"Finding:\n{json.dumps(evidence, default=str, sort_keys=True)}"
    if feedback:
        text += (
            "\n\nYour previous answer for this finding was rejected by a "
            f"deterministic check: {feedback}. Answer again."
        )
    return text


class TriageAgent:
    """Propose, validate, retry within a bound, and route what is left to a human.

    The client is injected so the loop is testable without a network and so a
    caller can substitute a cached or batched client. It needs only
    ``client.messages.create(**kwargs)`` returning an object with ``content``,
    ``stop_reason`` and ``usage``.
    """

    name = "llm"

    def __init__(
        self,
        client: Any,  # noqa: ANN401 - any object exposing .messages.create
        *,
        model: str = DEFAULT_MODEL,
        max_attempts: int = MAX_ATTEMPTS,
        review_threshold: float = REVIEW_THRESHOLD,
        backoff_seconds: float = 2.0,
        log: CallLog | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self.client = client
        self.model = model
        self.max_attempts = max_attempts
        self.review_threshold = review_threshold
        self.backoff_seconds = backoff_seconds
        self.log = log or CallLog()
        self.sleep = sleep
        self.clock = clock

    def triage(self, rows: Sequence[dict[str, Any]]) -> list[Outcome]:
        return [self.triage_one(row) for row in rows]

    def triage_one(self, row: dict[str, Any]) -> Outcome:
        key = item_key(row)
        feedback = ""
        last = "no attempt made"
        proposal: Proposal | None = None

        for attempt in range(1, self.max_attempts + 1):
            try:
                proposal = self._call(row, key, attempt, feedback)
            except ApiFailure as failure:
                last = str(failure)
                if not failure.retryable:
                    return Outcome(key, "human", f"not retryable: {last}", attempt)
                if attempt < self.max_attempts:
                    self.sleep(self.backoff_seconds * 2 ** (attempt - 1))
                continue

            verdict = validate(proposal, row)
            if not verdict.accepted:
                # The model is told why, once per attempt, and tries again.
                last = f"rejected: {verdict.reason}"
                feedback = verdict.reason
                continue
            if proposal.confidence < self.review_threshold:
                return Outcome(
                    key, "human", f"low confidence ({proposal.confidence:.2f})", attempt, proposal
                )
            return Outcome(key, "accepted", "validated", attempt, proposal)

        return Outcome(key, "human", f"retries exhausted: {last}", self.max_attempts, proposal)

    def _call(self, row: dict[str, Any], key: str, attempt: int, feedback: str) -> Proposal:
        started = self.clock()
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": _prompt(row, feedback)}],
                output_config={"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
            )
        except Exception as exc:  # classified, logged and routed; never raised
            elapsed = (self.clock() - started) * 1000
            retryable = is_retryable(exc)
            self.log.add(
                CallRecord(
                    key,
                    attempt,
                    self.model,
                    "api_error" if retryable else "api_error_fatal",
                    latency_ms=elapsed,
                    detail=f"{type(exc).__name__}: {exc}"[:300],
                    recorded_at=stamp,
                )
            )
            raise ApiFailure(f"{type(exc).__name__}: {exc}", retryable=retryable) from exc

        elapsed = (self.clock() - started) * 1000
        usage = getattr(response, "usage", None)
        tokens_in = int(getattr(usage, "input_tokens", 0) or 0)
        tokens_out = int(getattr(usage, "output_tokens", 0) or 0)

        def record(outcome: str, detail: str = "") -> None:
            self.log.add(
                CallRecord(
                    key,
                    attempt,
                    self.model,
                    outcome,
                    tokens_in,
                    tokens_out,
                    cost_usd(self.model, tokens_in, tokens_out),
                    elapsed,
                    detail[:300],
                    stamp,
                )
            )

        if getattr(response, "stop_reason", None) == "refusal":
            record("refusal")
            # The same request refused once will be refused again.
            raise ApiFailure("the model declined this finding", retryable=False)

        text = "".join(
            getattr(block, "text", "")
            for block in getattr(response, "content", [])
            if getattr(block, "type", "") == "text"
        )
        try:
            payload = json.loads(text)
            proposal = Proposal(
                key=key,
                cause=str(payload["cause"]),
                confidence=float(payload["confidence"]),
                evidence=tuple(str(x) for x in payload.get("evidence") or ()),
                reasoning=str(payload.get("reasoning") or "")[:300],
                source=self.name,
            )
        except (ValueError, KeyError, TypeError) as exc:
            record("unparseable", f"{type(exc).__name__}: {exc}")
            raise ApiFailure(f"unparseable response: {exc}", retryable=True) from exc

        record("ok")
        return proposal


#: What each deterministic rule in :mod:`pipeline.triage` says, in this
#: vocabulary. The baseline an agent has to beat is these rules scored on the
#: same labels, so they need to answer in the same terms.
RULE_TO_CAUSE: dict[str, str] = {
    "implausible": Cause.UNITS_OR_METHODOLOGY,
    "vintage_artifact": Cause.VINTAGE_ARTIFACT,
    "systematic_offset": Cause.CONTRACT_OFFSET,
    "granularity_mismatch": Cause.PLAN_MISMATCH,
    "marginal": Cause.GENUINE_DISAGREEMENT,
    # The rules' own abstention. Scored as one, not as a wrong answer.
    "unexplained": Cause.INSUFFICIENT_EVIDENCE,
}


class RuleTriager:
    """The deterministic baseline, answering in the agent's vocabulary.

    Reads the verdict the report stage already wrote to the queue rather than
    re-running the rules, so the baseline scored is the one that was published.
    """

    name = "rules"

    def triage(self, rows: Sequence[dict[str, Any]]) -> list[Outcome]:
        out = []
        for row in rows:
            key = item_key(row)
            cause = RULE_TO_CAUSE.get(
                str(row.get("triage_rule") or ""), Cause.INSUFFICIENT_EVIDENCE
            )
            proposal = Proposal(key, str(cause), 1.0, source=self.name)
            out.append(Outcome(key, "accepted", "rule", 1, proposal))
        return out


def write_human_queue(
    outcomes: Sequence[Outcome], rows: Sequence[dict[str, Any]], path: Path
) -> int:
    """The terminal path, as a file a person can open. Returns rows written."""
    by_key = {item_key(row): row for row in rows}
    queued = [o for o in outcomes if not o.accepted]
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        *EVIDENCE_FIELDS,
        "routed_because",
        "attempts",
        "proposed_cause",
        "proposed_confidence",
        "proposed_reasoning",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for outcome in queued:
            row = by_key.get(outcome.key, {})
            proposal = outcome.proposal
            writer.writerow(
                {
                    **{name: row.get(name, "") for name in EVIDENCE_FIELDS},
                    "routed_because": outcome.reason,
                    "attempts": outcome.attempts,
                    "proposed_cause": proposal.cause if proposal else "",
                    "proposed_confidence": proposal.confidence if proposal else "",
                    "proposed_reasoning": proposal.reasoning if proposal else "",
                }
            )
    return len(queued)


__all__ = [
    "CAUSES",
    "DEFAULT_MODEL",
    "EVIDENCE_FIELDS",
    "KEY_FIELDS",
    "MAX_ATTEMPTS",
    "OUTPUT_SCHEMA",
    "PRICES_PER_MTOK",
    "REVIEW_THRESHOLD",
    "RULE_TO_CAUSE",
    "ApiFailure",
    "CallLog",
    "CallRecord",
    "Cause",
    "Outcome",
    "Proposal",
    "RuleTriager",
    "TriageAgent",
    "Verdict",
    "cost_usd",
    "is_retryable",
    "item_key",
    "validate",
    "write_human_queue",
]
