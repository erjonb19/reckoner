# Reconciliation scope

The reconciliation covers **seven health systems**: every system that appears in
both the hospital lake and the payer target list. That is a design boundary, not
a gap, and this file exists so it is not mistaken for one later.

## The seven that reconcile

Each hospital rate is compared once against the carrier's distribution of rates
for the same service (ADR 0006), and only against rates of its own billing class
(ADR 0005). Ordered by raw share:

| system | hospital rates | compared | raw share | like-class share | residual | offsets |
|---|---:|---:|---:|---:|---:|---:|
| Mount Sinai | 1,348,398 | 625,523 | 46.39% | 47.67% | 54,637 | 29 |
| WMC | 170,710 | 58,238 | 34.12% | 34.12% | 0 | 77 |
| NYU Langone | 9,300,114 | 2,000,760 | 21.51% | 26.02% | 50,300 | 88 |
| White Plains | 350,530 | 66,542 | 18.98% | 19.75% | 212 | 10 |
| Montefiore | 2,597,798 | 427,064 | 16.44% | 17.17% | 3,760 | 3 |
| Northwell | 3,001,740 | 426,316 | 14.20% | 16.89% | 62,169 | 16 |
| NewYork-Presbyterian | 504,350 | 18,387 | 3.65% | 3.83% | 0 | 1 |
| **all seven** | **17,273,640** | **3,622,830** | **20.97%** | **24.15%** | **171,078** | |

Empire does not reach NewYork-Presbyterian, which is why that system sees five
carriers rather than six.

**A high share is not a better result.** WMC's 34% comes from 58,238 comparisons,
about a tenth of Mount Sinai's. The share says how much survived the rules; the
counts say how much there was. Read them together.

**Montefiore reconciles** as of 2026-09-23. Its cloud run was OOM-killed until
the hospital-side aggregate stopped keeping a t-digest per group (#83, #47). It
now peaks at 2,431 MiB of 8,192.

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

## The three payer-side systems that were added

Montefiore (374 NPIs), WMC (41) and White Plains (22) were parsed on the payer
side and absent from the hospital lake, so their payer rows could not reconcile
for the mirror-image reason. That is resolved: all thirteen of their MRFs were
fetched by hand -- both sites block automated access, White Plains with a 403
and WMC behind Cloudflare -- and ingested from `data/Inbox`, 27,940,062 rows.

Montefiore was previously out of scope on the grounds that the constraint barred
internal data. It is in now with permission, and these are public federal filings
like every other file here.

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
