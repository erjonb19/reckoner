# Labelling A1: how to fill `evals/triage_labels.csv`

A1's agent half (`src/agents/triage_agent.py`) is built and tested against a
stubbed model. **The labels are in:** all 250 findings in the published queue,
labelled September 2026. [Results](#results) has what they show and what they
don't. An empty label file reports `not measured`, never 0%.

## 1. Make a worksheet

```
python -m agents.triage_evals --queue summary/triage_queue.csv --worksheet evals/triage_worksheet.csv
```

This writes one row per finding in the current triage queue (250 today). The
`expected_cause` column is blank. After it come the columns you judge from: the
rates, the ratio, both vintages and the gap, and the deterministic rule's
verdict (`triage_rule`) as a hint. The scorer never reads the hint.

## 2. Fill `expected_cause` for the rows you can judge

Use exactly one of these values. Anything else stops the run and names the line.

| value | use it when |
|---|---|
| `units_or_methodology` | the two rates are expressed differently: per diem vs case rate, per unit vs per service, percentage vs dollars. Usually a ratio of 3× or more. |
| `vintage_artifact` | the files are far enough apart in time that the contract probably moved. The agent may only claim this at a gap of 30 days or more. |
| `contract_offset` | the difference is a base-rate gap across the whole contract (the same ratio on many codes), not something about this code. |
| `plan_mismatch` | the hospital's plan and the payer's network are probably not the same contract. |
| `setting_or_modifier` | setting, billing class or a modifier differ in a way the join missed. |
| `data_error` | one side's value is wrong on its face: a placeholder, a typo, $0.01. |
| `genuine_disagreement` | both filings look right and describe the same contract. This is the actual finding. |
| `insufficient_evidence` | you can't tell from these fields. This is a real label. It tells the eval the right move was to abstain. |

Leave a row blank if you haven't looked at it yet. Blank rows are skipped, so a
half-finished worksheet is fine. Fill `labelled_by` and `labelled_on`
(YYYY-MM-DD). Use `note` for anything a later reader would need, such as the
code's description, or why a ratio that looks like a units mismatch isn't one.

**Label what you believe, not what the rule says.** If you copy `triage_rule`,
you're measuring agreement with the baseline, and the baseline would score
perfectly against its own answers.

## 3. Save as `evals/triage_labels.csv` and score

Copy or rename the worksheet over `evals/triage_labels.csv`. The extra context
columns are ignored, so there's no need to delete them. Then run:

```
python -m agents.triage_evals --queue summary/triage_queue.csv
```

This scores the deterministic baseline, which costs nothing. Add `--record` to
append the score to `evals/triage_results.jsonl`.

## What the numbers mean

- **precision**: of the findings it gave a cause for, the share that were right.
  This is the gate (0.90). A wrong cause in a report reads as a finding. An
  abstention reads as "we don't know", which is honest.
- **coverage**: the share it answered at all, rather than abstaining or sending
  it to a human.
- **unmatched**: labels whose finding is no longer in the queue. Gold changes
  between runs. When the queue is regenerated, labels can drift out of it, and
  they're reported rather than silently dropped.

## How many labels

A minimum of about 50, spread across causes, before any number means much. With
250 findings in the queue, labelling all of them is an afternoon. The 29 rows
the rules call `unexplained` matter most, because they're the only ones the
agent exists for.

## Running the agent itself

The agent is never run from a flag, because every finding it sees is a billed
call. It runs from a script that has to be invoked on purpose, with a budget:

```
ANTHROPIC_API_KEY=... python scripts/run_a1_eval.py --budget-usd 5 --record
```

It needs the optional dependency (`pip install -e .[agents]`). The script
changes nothing about the agent: the prompt, validator, retry bound and 0.80
review threshold are the ones written before any label existed. It scores the
rules and the agent on the same labels and writes the human queue to
`evals/triage_human_queue.csv` and every call to `evals/triage_calls.jsonl`.
Each attempt is logged with tokens, cost and latency, including failed attempts.
Before each call it checks the budget against the worst case one call can cost,
so spend cannot pass the budget. A finding the budget stops goes to the human
queue with the reason, and the run reports how many there were.

## Results

### The rules baseline scores zero, by construction

Over all 250 labels: **precision 0.000, coverage 0.884** (221 wrong, 29
abstained, 0 unmatched).

The rules have exactly one route to `units_or_methodology`: the `implausible`
rule, at a ratio of 10× or more. The mart has already set every finding that far
apart aside as `entity_resolution_suspect` before the queue is built, so on this
queue that rule never fires and the rules never give that answer. The labelled
queue is dominated by that cause (below), so every answer the rules do give is
one of the others, and wrong. The zero measures the queue's make-up, not a
broken rule.

### What the labels measure

**210 of 250 labels are `units_or_methodology`**; the rest are 25
`genuine_disagreement` and 15 `insufficient_evidence`. The queue is dominated by
component-versus-facility mismatches: a hospital publishing one component of a
service against an insurer's rate for the whole of it, or the reverse. Their
ratios run from 0.10 to 9.98, and 106 of the 210 sit between 0.08× and 0.2×.
**So an agent's score on this queue mostly measures one skill**, spotting that
mismatch, and says little about the other seven causes, five of which have no
label at all. It is not a measure of triage in general.

The follow-up is in `docs/BUILT_VS_PLANNED.md`. First, a deterministic
component-pricing detector. Then a smaller queue, re-labelled stratified by
cause.

### The 25 labels that did not match

The first scoring left 25 labels unmatched. All 25 were Montefiore's, not White
Plains'. Montefiore's published file carries its facility name double-encoded,
UTF-8 read as cp1252 ("Center â€“ Children…"). The queue kept it that way; the
label file had it repaired. `item_key` now undoes that one round of
mis-encoding before joining, so both forms are one finding. The labels were
not edited. Before the fix: precision 0.000, coverage 0.924 over 225 matched.
