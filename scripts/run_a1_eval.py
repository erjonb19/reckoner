"""Run A1's agent once over the labelled queue, inside a hard budget, and score it.

Deliberately a script and not a flag on ``agents.triage_evals``: every finding it
sees is a billed call, so running it is a decision someone makes, not an option
someone passes by accident (``docs/labelling-a1.md``).

It changes nothing about the agent. The prompt, the validator, the retry bound
and the 0.80 review threshold are the ones the code already had before a single
label existed; the point of the run is to measure them, not to fit them.

The budget is enforced before each call, against the worst case one call can
cost, so the total cannot pass it. A finding the budget stops is routed to the
human queue with the reason "budget", and the run says how many there were: a
coverage figure that silently counted them as abstentions would be wrong.

    ANTHROPIC_API_KEY=... python scripts/run_a1_eval.py --budget-usd 5 --record
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agents.triage_agent import (
    DEFAULT_MODEL,
    CallLog,
    Outcome,
    TriageAgent,
    cost_usd,
    write_human_queue,
)
from agents.triage_evals import (
    LABELS,
    RESULTS,
    RuleTriager,
    append_result,
    gate,
    load_labels,
    load_queue,
    score,
)

#: The most one call can cost: a generous prompt and the full max_tokens reply.
WORST_CALL_TOKENS = (3_000, 1_024)


class BudgetExhausted(Exception):
    """Raised instead of making a call that could take spend past the budget."""


class Budgeted:
    """A client that refuses a call the budget cannot cover in the worst case."""

    def __init__(self, client: Any, log: CallLog, budget_usd: float, model: str) -> None:  # noqa: ANN401
        self._client, self._log, self._budget = client, log, budget_usd
        worst = cost_usd(model, *WORST_CALL_TOKENS)
        if worst is None:
            raise SystemExit(f"{model} has no known price; refusing to run without a budget")
        self._worst = worst
        self.stopped = 0

    @property
    def messages(self) -> Budgeted:
        return self

    def create(self, **kwargs: Any) -> Any:  # noqa: ANN401
        spent = self._log.cost_usd or 0.0
        if spent + self._worst > self._budget:
            self.stopped += 1
            raise BudgetExhausted(f"budget: ${spent:.4f} spent of ${self._budget:.2f}")
        return self._client.messages.create(**kwargs)


class Recording:
    """Passes rows to the agent and keeps its outcomes, for the human queue."""

    def __init__(self, agent: TriageAgent) -> None:
        self.agent = agent
        self.name = agent.name
        self.outcomes: list[Outcome] = []
        self.rows: list[dict[str, Any]] = []

    def triage(self, rows: Sequence[dict[str, Any]]) -> list[Outcome]:
        self.rows = list(rows)
        self.outcomes = self.agent.triage(rows)
        return self.outcomes


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--budget-usd", type=float, required=True)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--queue", type=Path, default=Path("summary/triage_queue.csv"))
    parser.add_argument("--labels", type=Path, default=LABELS)
    parser.add_argument("--human-queue", type=Path, default=Path("evals/triage_human_queue.csv"))
    parser.add_argument("--calls", type=Path, default=Path("evals/triage_calls.jsonl"))
    parser.add_argument("--record", action="store_true", help=f"append both scores to {RESULTS}")
    args = parser.parse_args(argv)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set; nothing was run.", file=sys.stderr)
        return 2

    import anthropic

    queue = load_queue(args.queue)
    labels = load_labels(args.labels)
    log = CallLog()
    client = Budgeted(anthropic.Anthropic(), log, args.budget_usd, args.model)
    agent = Recording(TriageAgent(client, model=args.model, log=log))

    baseline = score(RuleTriager(), queue, labels)
    result = score(agent, queue, labels, log=log)
    human = write_human_queue(agent.outcomes, agent.rows, args.human_queue)
    log.append_to(args.calls)

    routed = Counter(
        o.reason.split(":")[0].split(" (")[0] for o in agent.outcomes if not o.accepted
    )
    for triager in (baseline, result):
        print(triager.summary())
        passed, why = gate(triager)
        print(f"  gate: {'pass' if passed else 'hold'} -- {why}")
    print(f"human queue: {human} findings -> {args.human_queue}  {dict(routed)}")
    print(f"budget stops: {client.stopped}")
    print(json.dumps({"calls": log.summary()}, indent=1))
    if args.record:
        append_result(baseline)
        append_result(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
