# ADR 0006 — Compare each hospital rate against the carrier's distribution

- **Status:** Accepted
- **Date:** 2026-09-23
- **Resolves:** #70, option (a)

## Context

The join keyed on carrier, not plan. One hospital rate met every plan-level rate
the carrier published for that code at that facility. On one NYU Langone slice,
290 of them fell into a single key: 3,123,265 comparisons from about 125,000
rates on each side. One disagreement was counted once per plan.

Option (b) was to put the matched plan in the key. `docs/plan-matching.md`
measured what the plan strings allow: **15.0% of hospital rate rows** can be
matched to a network by name. UnitedHealthcare stays 82% unmatchable, because
hospitals name employer groups, not networks. Option (b) would leave most rates
without a counterpart.

## Decision

**One outcome per hospital rate.** Its like-class (ADR 0005), comparable payer
rates for the same facility, code, carrier and setting form a distribution:

| field | meaning |
|---|---|
| `payer_min`, `payer_max` | the carrier's lowest and highest comparable rate |
| `payer_rate` | the median, which the variance is measured against |
| `payer_count` | how many comparable payer rates |
| `payer_plan` | the network, or every network the distribution spans, `;`-separated |
| `inside_payer_range` | whether the hospital rate lies within `[payer_min, payer_max]` |

Each hospital rate becomes exactly one of: a comparison row, or one refusal with
one reason. The reason is decided in this order:

1. **TiC-exempt product**, by rule, before anything else. Medicare Advantage
   and Medicaid rates exist only in hospital files.
2. **No payer-side counterpart**: no payer rate at that facility, code, carrier
   and setting, in any class.
3. **Billing class unstated**, where the facility assumption doesn't apply.
4. **Different billing class**: counterparts exist, but only in the other class.
5. Like-class counterparts exist but **none is comparable**: the reason most of
   them were refused, with ties broken in the order `can_compare` checks.
6. Otherwise **compared**.

`candidates`, `pairs_formed` and every refusal count are now in hospital-rate
units. The column keeps the name `pairs_formed`; a "pair" is now a hospital rate
and a carrier distribution.

### Explanations

The existing rules run against a representative payer rate: the comparable rate
nearest the median, repriced at the median, so its vintage and plan belong to a
real payer rate. One rule is new, and one is scoped:

- **`within_payer_range`** (new): the hospital rate lies inside the carrier's own
  range for the code, the range spans more than one value, and there are at
  least two rates. The insurer publishes several prices for the service, and the
  hospital's is among them. A difference from the median is then not a
  disagreement between the two disclosures. It runs after the implausibility
  and granularity checks and before the vintage check.
- **The plan question is asked of every network in the distribution.** Against
  one network it is the pairwise check. Against several: if the hospital's
  plan matches any of them, the row falls through, and a rate outside the whole
  range is a finding. If none matches but one names several networks, it is
  granularity. Otherwise it is `plan_unresolved`, "not matched to any of the
  carrier's N networks".

  *Revised the same day.* The first version skipped the question for
  multi-network distributions, on the grounds that comparing with one network
  would be arbitrary. The first rebuild showed what that cost. Every
  administrator or employer plan that matches none of the carrier's networks
  became `unexplained`: Mount Sinai's residual went from 1.8% to 55% of what
  formed, and its first exemplar was an Aetna TPA plan six times below every
  Aetna network. Asking whether the plan matches *any* network is not
  arbitrary, and it keeps "we can't tell if this is the same contract" apart
  from "this contract disagrees".

Systematic offsets are unchanged in method. Their contract key uses the row's
payer label, the network or networks, which is also what the residual row
carries.

## Consequences

- Every published count changes unit and falls by roughly the fan-out. Old and
  new figures are not comparable. The report says so, and BUILT_VS_PLANNED
  records the old ones under their date.
- The spread across plans is kept, not discarded: `payer_min`, `payer_max` and
  `payer_count` are on every residual row, and A1 is shown them.
- A1's triage queue changes grain too. Labels written against the old queue
  will partly show as `unmatched` in the eval. They won't be scored as wrong.
- `mart_cli --mode pairs` still runs the pairwise join, as a diagnostic.
