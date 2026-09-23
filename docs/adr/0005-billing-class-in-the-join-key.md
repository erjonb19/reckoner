# ADR 0005 — Billing class in the join key

- **Status:** Accepted
- **Date:** 2026-09-23
- **Supersedes:** the join definition used by every gold build before this date

## Context

The cross-source join matched a hospital rate to payer rates on facility, code,
carrier and setting. Billing class was checked afterwards, as a refusal.
`docs/refusal-decomposition.md` measured what that did. **70.92% of all
286,599,414 candidates were professional-against-institutional refusals**: a
facility charge for a scan meeting the radiologist's fee for reading it. The
join built those comparisons only to reject every one.

They aren't wrong refusals. A professional fee is not the facility's price, and
no lever should recover them. But counting them as candidates made the
comparable share mostly a measure of how often the two classes co-occur for a
code, not of how often the two disclosures agree. The pooled share was 5.28%;
excluding them, 18.15%, with no pair gained.

## Decision

**Billing class is part of the join key.** A hospital rate is compared only
against payer rates of its own billing class:

- A stated class joins on itself, compared case-insensitively.
- An unstated hospital class joins as `facility`, but only for a facility the
  data shows publishes no professional rates
  (`reconcile.curated.facility_only_hospitals`). That is the same scoping the
  refusal applied, now applied at the key.
- Otherwise an unstated class is refused as `billing_class_unstated`, as
  before.

A hospital rate whose only counterparts are in the other class is refused as
`different_billing_class`, **once per hospital rate**, and never compared.

**Both shares are reported, side by side, for this release:**

- **raw share**: compared hospital rates over every hospital rate considered;
- **like-class share**: compared over the same, less those refused as
  `different_billing_class`.

`coverage` carries `candidates`, `comparable_share` (raw),
`like_class_candidates` and `like_class_share`. The report, the page and
the README show the two together. Neither replaces the other.

## Why both, and not just the new one

The raw share is the continuity figure: every earlier published number used its
denominator. Dropping it in the same release that changes the definition would
make the change unreadable, because a reader couldn't tell how much of the
difference is definition and how much is data. After one release with both,
this ADR can be superseded by one that drops the raw share, if that's the
decision then.

## Consequences

- The join no longer builds comparisons it will refuse. On NYU Langone that was
  most of the work in each slice.
- The decomposition's largest lever, "billing class", stops being one: it was a
  definition, and it is now stated as one.
- `mart_cli --mode pairs` keeps the old pairwise join as a diagnostic under the
  old definition. It is not what gold publishes.
- ADR 0006 changes the grain in the same release. The two shares are therefore
  both in hospital-rate units, and neither is comparable to the old pair counts.
