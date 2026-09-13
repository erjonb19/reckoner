# Reconciliation scope

The reconciliation covers **four health systems**. That is a design boundary, not a
gap, and this file exists so it is not mistaken for one later.

## The four

| system | hospital lake | payer system | carriers reached |
|---|---|---|---:|
| Mount Sinai | Mount Sinai Health System | Mount Sinai | 6 |
| Northwell | Northwell Health | Northwell | 6 |
| NYU Langone | NYU Langone Health | NYU Langone | 6 |
| NewYork-Presbyterian | NewYork-Presbyterian | NYP | 5 |

Empire does not reach NewYork-Presbyterian, which is why that system has five.

## The eight that are hospital-side only

NYC Health + Hospitals, Catholic Health System (Buffalo), University of Rochester
Medical Center, Upstate University Hospital, Ellis Medicine, Rochester Regional
Health, Maimonides Medical Center, Crouse Health.

**These were never payer-side targets.** The payer parse filtered to a 940-NPI and
120-TIN anchor list covering seven systems; a hospital outside that list has no
payer rows to be reconciled against, and no amount of processing will produce
any. Their data is still worth holding — it is 51M rows of hospital-side prices,
and the within-system and regional comparisons use it — but cross-source
reconciliation is not a question that can be asked of it.

## Three payer-side systems have no hospital data

Montefiore (374 NPIs), WMC (41) and White Plains (22) are parsed on the payer side
and absent from the hospital lake. Their payer rows cannot reconcile either, for
the mirror-image reason.

## `billing_class`, and the assumption made about it

An earlier draft of this file said Northwell stated `billing_class` on 1.6% of
rows and concluded that only Mount Sinai could be reconciled pairwise. **That was
measured against the wrong denominator.** The 1.6% counted Northwell's 84.9M
chargemaster rows, which have no payer counterpart under any circumstances. On
rows that could actually pair:

| system | rows on CPT/HCPCS/MS-DRG | `facility` | unstated | `professional` |
|---|---:|---:|---:|---:|
| Mount Sinai | 1,766,907 | 100% | 0% | 0 |
| Northwell | 3,577,325 | **39.5%** | 60.5% | 0 |
| NYU Langone | 13,459,491 | 0% | 100% | 0 |
| NewYork-Presbyterian | 528,798 | 0% | 100% | 0 |

**No system in scope publishes a single professional rate.** Lake-wide only
127,537 rows do, all of them at Maimonides Medical Center (121,119) and Upstate
University Hospital (6,418) — neither in scope.

So an unstated billing class on these four systems is read as `facility`, under
`--assume-facility-when-unstated`. Three things make that safe rather than
convenient:

1. **It is scoped by the data, not by a list.** `reconcile.curated.facility_only_hospitals`
   computes which systems publish no professional row and only those are
   eligible. A hardcoded set would stop being true the moment the lake gains a
   system; on the corpus as it stands it excludes exactly Maimonides and Upstate.
2. **It resolves to `facility` specifically, not to "compatible with anything".**
   That distinction is the whole point. The refusal this relaxes existed because
   an absent value matched *both* the payer's professional and its institutional
   rate — a cross-join in which 96.4% of pairs landed against professional, the
   hospital's charge for a scan against the radiologist's fee for reading it.
   Assuming `facility` meets institutional only; a payer professional rate is
   still refused, as `different_billing_class`. On NYP that refusal fires on
   2,020,625 rows, so the guard is doing work rather than waving pairs through.
3. **Every assumed pair says so.** The note rides into the variance row and
   therefore into A1's triage queue, where a reader otherwise has no way to tell
   an inferred billing class from a published one.

Measured effect on NewYork-Presbyterian: **0 pairs before, 106,852 after.**

The assumption is still an assumption. If a system in scope begins publishing
professional rates, `facility_only_hospitals` drops it automatically and its
pairs revert to being refused — which is the behaviour to want, and is tested.

## Known caveat: Empire matched on NPI only

Empire BCBS carries **no TINs at all**; every other carrier matched mostly by TIN.
Its provider coverage therefore rests on a different identifier from the rest.

Any carrier-to-carrier comparison must carry this caveat. Empire reaching more or
fewer services than Aetna is not evidence about either contract until the
identifier difference is ruled out, because the two carriers' rows were selected
by different means.
