# Labelling A1: how to fill `evals/triage_labels.csv`

A1's agent half (`src/agents/triage_agent.py`) is built and tested against a
stubbed model. It has **not** been run against real data, and it will not be
scored until this file has rows in it. It ships with a header and nothing else,
on purpose. Until you add labels, the eval reports `not measured`, not 0%.

## 1. Make a worksheet

```
python -m agents.triage_evals --queue summary/triage_queue.csv --worksheet evals/triage_worksheet.csv
```

This writes one row per finding in the current triage queue (200 today). The
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
200 findings in the queue, labelling all of them is an afternoon. The 34 rows
the rules call `unexplained` matter most, because they're the only ones the
agent exists for.

## Running the agent itself

The agent is never run from a flag, because every finding it sees is a billed
call. When you decide to run it, you construct it in code with a client:
`TriageAgent(anthropic.Anthropic())`. Each attempt is logged with tokens, cost
and latency, including failed attempts. Anything it can't settle within three
attempts, or answers below 0.80 confidence, goes to the human queue file
written by `write_human_queue`. At Opus 5 list price a finding costs roughly one
cent per attempt, so the 200-finding queue is a few dollars at most. That's
still your call, not a default.
