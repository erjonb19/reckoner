# Plan matching: what a fuzzy pass buys

Item 10's first pass: fuzzy plan-name matching with confidence scores, no
model, **report-only**. `src/agents/plan_fuzzy.py` is the matcher.
`scripts/measure_plan_matching.py` produced every number below from the six
reconciled systems' hospital plans and the payer networks in silver. Nothing in
the mart calls the fuzzy pass, so no reconciliation result moved.

## The answer first

**Comparable-share lift: 0.00 percentage points, by construction.**
[`refusal-decomposition.md`](refusal-decomposition.md) established why before
this was built. An unresolved plan is an *explanation* attached to a pair that
formed, never a refusal. The join keys on carrier, not plan, so no candidate is
refused over its plan, and matching plans can't turn a refusal into a pair.

What plan matching does move is the explanation. Today 42.5% of pairs are
explained only as `plan_unresolved`. A plan `MATCH` takes a pair out of that
bucket and lets it fall through to the next rule, usually `unexplained`. That
is, **it turns "we can't tell whether these are the same contract" into a real
finding for A1's queue.** It never makes pairs look more alike.

## Plan-level matchability

The share of hospital rate rows whose plan string reaches each verdict against
*any* network its carrier publishes. Each plan counts once, weighted by its rows.

| carrier | rows | rules: match | fuzzy: match | family only | fuzzy: still unknown |
|---|---:|---:|---:|---:|---:|
| Aetna | 1,455,791 | 21.61% | 21.61% | 0.00% | 61.94% |
| Anthem / Empire BCBS | 3,232,037 | 25.93% | **28.84%** | 0.00% | 41.23% |
| Cigna | 1,027,614 | 17.60% | 17.60% | 0.00% | 51.14% |
| EmblemHealth | 3,253,160 | 0.00% | 0.00% | 48.37% | 100.00% |
| UnitedHealthcare | 2,012,717 | 9.39% | 9.39% | 0.00% | 82.33% |
| **all five** | 10,981,319 | **13.87%** | **14.72%** | 14.33% | 69.84% |

**+0.85 points, all of it from one network.** The five new matches are all
Empire's Connection network, which hospitals write as "Empire Connection",
"BCBS BLUE CONNECTION (ALL PLANS)", "Connection SG" and similar. 94k rows.

## Why so little

The gains that looked available before measuring mostly weren't there:

- **UnitedHealthcare, 82% unknown.** The fuzzy pass reads `ChoicePlus` as a
  network, where the rules had discarded it. But no hospital in the corpus
  writes "Choice Plus". They write "Oxford", "Compass", "All Payer" and
  employer groups ("APWU HEALTH PLAN 1027"). Those name who bought the plan,
  not the network, and no alias table can recover a network from them.
- **EmblemHealth, 48% family only, 0% matched.** Emblem's payer files are
  labelled by contract ID (`GHIHOS000001` … `HIPHOS000091`, 99 of them). The
  prefix names the product line, GHI or HIP, and the hospital often names the
  same line. But a product line isn't a contract, so these stay below the match
  threshold at 0.6 confidence. The first prototype matched them anyway, and
  paired `HIP MEDICAID-ENHANCED CARE` with a commercial HIP contract. That
  prototype is why government products are now excluded outright.
- **Aetna's `NY` file** is labelled by state. No plan string can match it or be
  ruled out against it, and the reviewed labels already say so.

## Precision

On the 190 reviewed labels the fuzzy pass scores the same as the rules: 17 true
matches, **0 false positives**. That result is weaker than it looks. **None of
the reviewed labels covers a case the fuzzy pass changes.** They are Mount Sinai
only, and have no Empire Connection, ChoicePlus or Emblem examples. Its
precision on the new matches is unmeasured.

`evals/plan_matching_proposed.jsonl` holds 25 proposed labels: the 5 new
matches and 20 family cases, all `reviewed: false`. They need a person to check
them before the pass can be scored on what it actually does. Sign-off follows
the same process as `plan_matching.jsonl`.

## A rules error, found and not fixed

`resolve_plan` reads "open access" as Aetna's Open Access family before it
can read "open access plus" as Cigna's OAP. So "Cigna Open Access Plus" is
judged a `NO_MATCH` against `NationalOAP`. No hospital writes that string
verbatim, but "CIGNA OPEN ACCESS 1413" (32,552 rows) goes down the same path.

It changes no result today, because `explain()` files `NO_MATCH` under
`plan_unresolved` along with `UNKNOWN`. It is also left alone deliberately: the
mart calls `resolve_plan`, and a fix there is a reviewed change to results, not
something to slip into a report-only pass. The fuzzy pass never overrules a
rules `NO_MATCH`, and a test holds it to that.

A related point: `explain()` treats "these are known to be different contracts"
(`NO_MATCH`) the same as "we can't tell" (`UNKNOWN`). The first is arguably a
refusal, not an explanation. That's worth deciding alongside #70.

## What this means for #70

Option (b), keying the join on plan, needs plans to match. On this corpus the
matchable share is 14.7% of hospital rate rows by string, and alias work has
nearly exhausted what the strings can give. Getting further would take
information neither file publishes in a plan name: the payer's network
membership by employer group, or Emblem's contract IDs mapped to hospital
products. That favours option (a), collapsing to one representative payer rate
per carrier, unless a second data source for plan identity is in scope.
