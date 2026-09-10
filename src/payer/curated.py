"""Read already-parsed Transparency in Coverage rates into the curated shape.

The payer files themselves are 100 GB to 1 TB+ and have already been parsed
once, upstream, into ``payer_parquet/*.parquet``. Architecture rule 1 says
parse once and land curated, so **nothing here reopens a raw payer file**.
This module is a reader over that output and nothing more.

The upstream contract is documented in ``mrf_pipeline/docs/PAYER_PARQUET_SCHEMA.md``
and profiled against the real files. Six of its findings are load-bearing here,
because each is a way to get a confidently wrong number:

1. **A ``.part`` file is an open pyarrow writer handle**, not a short file --
   it has no footer and cannot be read at all. The rename to ``.parquet`` *is*
   the completion signal, so a plain glob for ``*.parquet`` is the whole
   dataset and in-flight parses exclude themselves. That is why this module
   never filters on payer name to skip work in progress: it would go stale the
   moment a lane finished.
2. **Two Cigna files are row-for-row duplicates of two others.** 13.2% of the
   corpus, counted twice, if a reader trusts the glob.
3. **Exact duplicate rows exist inside files** -- 17.8% of ``Aetna_NY``. So
   ``COUNT(*)`` is not a count of priced facts and a median over raw rows is
   weighted by however often the parser emitted the same line.
4. **``negotiated_rate`` changes unit with ``rate_type``.** ``percentage`` rows
   are percents of billed charges and ``per diem`` rows are per-day. Averaged
   in with dollars they silently drag every summary down.
5. **``group_tins`` is a fan-out width, not an identifier.** A rate shared with
   514,491 tax IDs is the payer's network-wide fee schedule, not what it pays
   this health system.
6. **13.1% of rows span more than one health system**, and treating the
   comma-joined ``systems`` string as one system is the bug that previously
   misattributed 44% of rows.

Two things the hospital side has and this data does not, which the loader has
to supply from outside the Parquet:

* **Vintage.** The Parquet carries no as-of date at all. The reporting month
  survives only in the source URL, so it is reconstructed here as a declared
  table rather than inferred from file mtime -- mtime is when the parse ran,
  which would date every payer file to whenever the pipeline last happened to
  execute and quietly destroy the vintage-mismatch analysis that CLAUDE.md
  names as the structural hazard.
* **Provider identity below the health system.** The payer side resolves to a
  system, never to a facility, because the provider-group boundary is dissolved
  into matched TINs upstream. The hospital side's unit is the facility. That
  gap cannot be closed from this data, so it is made explicit: see
  :func:`to_comparable_rates` and the ``facilities`` argument.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.dataset as ds
import pyarrow.parquet as pq

from agents.entity_resolution import CANONICAL_PAYERS, PayerCandidate, RuleBasedMatcher
from payer.contract import ContractCheck, Severity, validate_file, validate_schema
from reconcile.comparability import ComparableRate
from storage import Location

#: Columns the curated shape needs. The upstream file has 14; ``description``
#: is excluded because the schema doc records the same code carrying up to
#: three different descriptions, so it is not a key and not a fact.
NEEDED_COLUMNS = (
    "billing_code",
    "code_type",
    "negotiated_rate",
    "rate_type",
    "billing_class",
    "service_codes",
    "group_tins",
    "payer",
    "systems",
    "system_count",
)

#: Files that duplicate another file wholesale. ``Cigna_PathwellOAP`` is
#: identical to ``Cigna_NationalOAP`` and ``Cigna_PathwellPPO`` to
#: ``Cigna_NationalPPO`` on all thirteen non-``payer`` columns, verified
#: upstream with ``EXCEPT ALL`` in both directions. Keeping both double-weights
#: 2,717,020 Cigna rates. The ``National`` pair is kept arbitrarily -- they are
#: identical, so the choice is only about not counting them twice.
DUPLICATE_PAYER_FILES = frozenset({"Cigna_PathwellOAP", "Cigna_PathwellPPO"})

#: Payer-proprietary code systems with no cross-source meaning. Aetna's
#: ``LOCAL`` codes and Cigna's single ``CSTM-ALL`` code cannot be matched to a
#: hospital's published CPT or DRG, so a pair built on them would be a string
#: coincidence rather than the same service.
UNTRANSLATABLE_CODE_TYPES = frozenset({"LOCAL", "CSTM-ALL"})

#: Reporting month of each payer file, kept as an override for files whose own
#: header is missing or wrong. It used to be the only source, because the early
#: Parquet carried no date column and a file's mtime is the parse time rather
#: than the vintage. The parser now writes the source's ``last_updated_on``
#: into every row, so :func:`_file_vintage` reads the vintage off the data and
#: this map only fills gaps: across the 120 files on hand it agrees with the
#: header everywhere it has an opinion, and covers 12 of them.
#:
#: Keeping the map matters because a hardcoded list silently stops describing
#: the lake as the lake grows -- Empire, Emblem and UHC were all landing with
#: ``None`` here, which is exactly the carriers a vintage-aware rule then
#: cannot reason about.
PAYER_SOURCE_VINTAGES: dict[str, str] = {
    "Aetna_NY": "2026-06-05",
    "AetnaALIC_Hmo": "2026-08-05",
    "AetnaALIC_Epo": "2026-08-05",
    "AetnaALIC_Ppo": "2026-08-05",
    "AetnaALIC_OpenAccessElectChoice": "2026-08-05",
    "AetnaALIC_OpenAccessManagedChoice": "2026-08-05",
    "AetnaALIC_OpenAccessHealthNetworkOption": "2026-08-05",
    "Cigna_LocalPlus": "2026-08-01",
    "Cigna_NationalOAP": "2026-08-01",
    "Cigna_NationalPPO": "2026-08-01",
    "Cigna_PathwellOAP": "2026-08-01",
    "Cigna_PathwellPPO": "2026-08-01",
}

#: Transparency in Coverage covers the commercial group and individual market
#: only; CMS explicitly exempts Medicare, Medicare Advantage, Medicaid and
#: Medicaid MCO plans. So every row in a TiC file is commercial by rule, and
#: this is a fact about the regulation rather than an inference from the data.
TIC_PRODUCT_CLASS = "commercial"

#: Place-of-service codes that mean an inpatient stay, and those that mean an
#: outpatient encounter. Only used to type a rate's setting; a list spanning
#: both resolves to ``both``.
_INPATIENT_POS = frozenset({"21", "51", "52", "55", "56", "61"})
_OUTPATIENT_POS = frozenset({"19", "22", "23", "24", "49", "50", "71", "72"})

#: ``CSTM-00`` is a payer literal meaning "all places", not place-of-service
#: code 00, and an empty string means the source omitted the field -- which
#: upstream profiling shows is exactly co-extensive with Cigna's institutional
#: rows. Neither restricts the rate to a setting, which is the same thing the
#: hospital side means by ``both``. Mapping them to ``both`` rather than to
#: ``None`` is therefore a semantic match, not a fudge: it is what lets an
#: unrestricted payer rate meet an unrestricted hospital rate in the join.
_UNRESTRICTED_SERVICE_CODES = frozenset({"CSTM-00", ""})

#: The hospital MRF vocabulary calls an institutional claim ``facility``.
#: Carrying the payer's word through unchanged would make every pair differ on
#: billing class and refuse for a difference in spelling.
_BILLING_CLASS_TO_HOSPITAL = {"institutional": "facility", "professional": "professional"}


class SystemAttribution(StrEnum):
    """How to attribute a rate that spans more than one health system.

    The upstream doc requires this to be a deliberate choice, because it is the
    difference between two defensible numbers and one previously shipped bug.
    """

    #: Keep only rows that name exactly one system. Under-counts, and the
    #: undercount is uneven across systems, but every row it keeps is
    #: attributable. The default, because a cross-source variance is a claim
    #: about one system's contract.
    EXCLUSIVE = "exclusive"
    #: Credit the rate to every system it touches. Counts over-sum, and the
    #: same rate appears under several systems.
    EXPLODE = "explode"


@dataclass(frozen=True)
class PayerFile:
    """One completed payer Parquet file, and what its name encodes.

    ``payer`` upstream is a hand-authored config label that equals the filename
    stem. It is not a payer name and not a network name; it encodes carrier,
    legal entity and product by convention only. So it is decomposed here
    rather than trusted as an identifier.
    """

    path: Path
    stem: str
    carrier: str
    network: str
    vintage: str | None
    #: Contract errors found at load time, as rendered strings. Non-empty means
    #: the file is quarantined: kept visible in the summary, kept out of the
    #: dataset. Warnings are deliberately absent -- a blank billing code is not
    #: grounds to drop a file.
    contract_errors: tuple[str, ...] = ()

    @property
    def is_duplicate(self) -> bool:
        return self.stem in DUPLICATE_PAYER_FILES

    @property
    def is_quarantined(self) -> bool:
        return bool(self.contract_errors)


def _file_vintage(path: Path) -> str | None:
    """The ``last_updated_on`` the payer stamped on this file, if it has one.

    Read from the first row group rather than the whole column: the value is
    file-level metadata the parser copies onto every row, so row one carries it
    and a 6.6 million row scan would answer the same question. Verified
    single-valued across all 120 files on hand.

    Returns ``None`` for a file predating the column, or one whose header the
    payer left empty -- both fall back to :data:`PAYER_SOURCE_VINTAGES`.
    """
    try:
        pf = pq.ParquetFile(path)
        if "last_updated_on" not in pf.schema_arrow.names:
            return None
        if pf.num_row_groups == 0:
            return None
        column = pf.read_row_group(0, columns=["last_updated_on"]).column(0)
        if column.length() == 0:
            return None
        value = column[0].as_py()
    except (OSError, pa.ArrowInvalid):
        return None
    return value or None


def discover_payer_files(
    root: Path,
    *,
    include_duplicates: bool = False,
    include_quarantined: bool = False,
    validate: ContractCheck = ContractCheck.SCHEMA,
) -> list[PayerFile]:
    """Every completed payer file under ``root``, contract-checked on the way in.

    Non-recursive by design. ``old_npi_only/`` and ``trial_60tins/`` hold
    superseded output from earlier runs and are not part of the contract; a
    flat glob excludes them without needing to name them. In-flight parses
    exclude themselves by still being ``.part``.

    ``validate`` defaults to :attr:`ContractCheck.SCHEMA`, which reads only the
    Parquet footer: 0.03 seconds across all 120 files, against roughly two
    minutes for :attr:`ContractCheck.FULL`. That is the whole reason the gate can
    be on by default, and the cheap tier covers the failures that would break the
    read anyway -- a missing column or a wrong type makes the next ``to_table``
    raise, while a blank code in one row does not.

    A file with contract *errors* is quarantined rather than fatal, per
    architecture rule 4: it is dropped from the returned list but still reported
    by :func:`file_summary` with its reason, so a vanished payer is a named
    exclusion instead of a smaller number nobody questions. Warnings never
    quarantine.
    """
    if not root.exists():
        raise FileNotFoundError(f"no payer parquet directory at {root}")

    files = []
    for path in sorted(root.glob("*.parquet")):
        stem = path.stem
        carrier, network = split_label(stem)
        files.append(
            PayerFile(
                path=path,
                stem=stem,
                carrier=carrier,
                network=network,
                vintage=PAYER_SOURCE_VINTAGES.get(stem) or _file_vintage(path),
                contract_errors=_contract_errors(path, validate),
            )
        )
    if not include_duplicates:
        files = [f for f in files if not f.is_duplicate]
    if not include_quarantined:
        files = [f for f in files if not f.is_quarantined]
    return files


def _contract_errors(path: Path, level: ContractCheck) -> tuple[str, ...]:
    """Contract errors for one file at the requested depth. Warnings are ignored."""
    if level is ContractCheck.NONE:
        return ()
    check = validate_schema if level is ContractCheck.SCHEMA else validate_file
    violations, _ = check(path)
    return tuple(v.describe() for v in violations if v.severity is Severity.ERROR)


def split_label(stem: str) -> tuple[str, str]:
    """Split a config label into its carrier and network halves.

    ``AetnaALIC_OpenAccessManagedChoice`` is Aetna Life Insurance Company's
    Open Access Managed Choice network. The convention is one underscore
    between carrier-and-entity and product, except for UHC, which prefixes a
    state.
    """
    head, _, tail = stem.partition("_")
    if head.upper() == "UHC" and tail:
        # UHC_NY_ChoicePlus -> carrier UHC, network ChoicePlus.
        _state, _, product = tail.partition("_")
        if product:
            return head, product
    return head, tail or head


@dataclass(frozen=True)
class PayerFilter:
    """Narrowing applied in Arrow, before rows are materialised.

    Deliberately neutral by default. Every filter here removes rows *before*
    the comparability layer sees them, so a default that dropped percentage or
    placeholder rates would flatter the comparable share by hiding the
    refusals it is meant to measure. The opinionated values belong at the call
    site, where they are visible in the run that reports them.
    """

    systems: tuple[str, ...] = ()
    codes: tuple[str, ...] = ()
    #: Keep only codes beginning with this, so a run can be sharded.
    #:
    #: This is the memory bound, not a narrowing of the question.
    #: :func:`aggregate_rates` materialises the whole filtered table and then
    #: takes a distinct over every column, so its peak cost scales with rows
    #: entering it, not rows coming out: NYU Langone sends 15.1M and reached
    #: 58 GB of virtual memory. Sharded totals equal unsharded ones because
    #: every join key contains the code, so no pair straddles a shard.
    code_prefix: str = ""
    code_types: tuple[str, ...] = ()
    #: Drop payer-proprietary code systems that cannot mean anything to a
    #: hospital file. On by default: these are not a refusal to measure, they
    #: are codes with no counterpart by construction.
    drop_untranslatable_codes: bool = True
    #: Maximum fan-out width. ``None`` keeps network-wide fee schedules, which
    #: are real rates but are not this system's contract.
    max_group_tins: int | None = None
    #: Minimum rate to keep. ``0.0`` keeps the placeholders so the
    #: comparability layer can refuse them by name.
    min_rate: float = 0.0
    attribution: SystemAttribution = SystemAttribution.EXCLUSIVE

    def expression(self) -> ds.Expression | None:
        terms: list[ds.Expression] = []
        if self.attribution is SystemAttribution.EXCLUSIVE:
            terms.append(ds.field("system_count") == 1)
        if self.systems:
            terms.append(_touches_system(list(self.systems)))
        if self.codes:
            terms.append(ds.field("billing_code").isin(list(self.codes)))
        if self.code_prefix:
            terms.append(pc.starts_with(ds.field("billing_code"), self.code_prefix))
        if self.code_types:
            terms.append(ds.field("code_type").isin(list(self.code_types)))
        if self.drop_untranslatable_codes:
            terms.append(~ds.field("code_type").isin(sorted(UNTRANSLATABLE_CODE_TYPES)))
        if self.max_group_tins is not None:
            terms.append(ds.field("group_tins") <= self.max_group_tins)
        if self.min_rate > 0:
            terms.append(ds.field("negotiated_rate") >= self.min_rate)
        if not terms:
            return None
        combined = terms[0]
        for term in terms[1:]:
            combined = combined & term
        return combined


def _touches_system(systems: Sequence[str]) -> ds.Expression:
    """Match rows whose comma-joined ``systems`` list contains any of these.

    A substring test rather than equality, because the column holds a sorted
    comma-joined list and ``NYU Langone`` must match both the exclusive row and
    the ``Mount Sinai,NYP,NYU Langone,WMC`` one. Under
    :attr:`SystemAttribution.EXCLUSIVE` the ``system_count == 1`` term has
    already reduced this to equality; the substring form is what makes
    ``EXPLODE`` work.
    """
    field = ds.field("systems")
    term = pc.match_substring(field, systems[0])
    for name in systems[1:]:
        term = term | pc.match_substring(field, name)
    return term


def open_payer_dataset(
    files: Sequence[PayerFile], *, location: Location | None = None
) -> ds.Dataset:
    """Open the completed payer files as one dataset.

    All ten completed files share an identical schema, so they form a single
    dataset with no partitioning and no manifest.
    """
    if not files:
        raise FileNotFoundError("no completed payer parquet files to read")
    if location is None:
        return ds.dataset([str(f.path) for f in files], format="parquet")
    # A list of paths rather than a root, so the seam supplies the filesystem and
    # each file is named relative to it.
    paths = [location.child(f.path.name).root for f in files]
    return ds.dataset(paths, filesystem=location.filesystem, format="parquet")


def aggregate_rates(
    dataset: ds.Dataset,
    where: PayerFilter | None = None,
) -> pa.Table:
    """Collapse payer rate lines to one representative rate per key.

    Two collapses happen, in order, and they are not the same thing:

    * **Exact duplicate removal.** Whole rows repeat inside a file -- 17.8% of
      ``Aetna_NY``. These are an artifact of the provider-group boundary being
      dissolved upstream, not evidence that a rate was negotiated twice, so
      they are removed before anything is counted.
    * **Median per key.** 45% of upstream keys carry more than one distinct
      rate, up to 1,056 of them. The median is used for the same reason the
      hospital side uses it: one mis-scaled row moves a mean enough to dominate
      every spread it lands in, and these files contain rates from $3.39 to
      $3,200 on a single key.
    """
    where = where or PayerFilter()
    scanned = dataset.to_table(columns=list(NEEDED_COLUMNS), filter=where.expression())
    if scanned.num_rows == 0:
        return scanned

    # Distinct over every column, including the rate: exact duplicate rows only.
    deduped = scanned.group_by(list(NEEDED_COLUMNS)).aggregate([])

    keys = [
        "billing_code",
        "code_type",
        "rate_type",
        "billing_class",
        "service_codes",
        "payer",
        "systems",
        "system_count",
    ]
    return deduped.group_by(keys).aggregate(
        [
            ("negotiated_rate", "approximate_median"),
            ("negotiated_rate", "count"),
            ("group_tins", "min"),
        ]
    )


def to_comparable_rates(
    table: pa.Table,
    files: Sequence[PayerFile],
    *,
    facilities: Mapping[str, Sequence[str]] | None = None,
    attribution: SystemAttribution = SystemAttribution.EXCLUSIVE,
    canonicalise_payers: bool = True,
) -> list[ComparableRate]:
    """Turn aggregated payer rows into the curated ``ComparableRate`` shape.

    ``facilities`` maps a health-system name as the payer file spells it to the
    facility names the hospital side publishes. It exists because the two
    sources do not identify a provider at the same grain: the hospital MRF
    names a facility, and the payer file resolves only to a system, since the
    provider-group boundary is dissolved into matched TINs upstream and cannot
    be recovered from this Parquet.

    Passing it fans one system-level payer rate onto each facility of that
    system. That is an assumption -- that the system's contracted rate applies
    at its facilities -- and it is the assumption the join requires, so it is
    made here in one place where it can be seen and turned off, rather than
    buried in a join condition. Without it the payer rate keeps the system name
    and will only meet a hospital row that publishes under the system name too.
    """
    if table.num_rows == 0:
        return []

    by_stem = {f.stem: f for f in files}
    rows = table.to_pylist()

    canonical: dict[str, str] = {}
    if canonicalise_payers:
        canonical = _canonical_payers({str(r.get("payer") or "") for r in rows}, by_stem)

    rates: list[ComparableRate] = []
    for row in rows:
        stem = str(row.get("payer") or "")
        source_file = by_stem.get(stem)
        rate = row.get("negotiated_rate_approximate_median")
        if rate is None:
            continue

        rate_type = str(row.get("rate_type") or "")
        for system in _systems_of(row, attribution):
            for hospital in _facilities_of(system, facilities):
                rates.append(
                    ComparableRate(
                        source="payer",
                        hospital=hospital,
                        code=str(row.get("billing_code") or ""),
                        code_type=_optional(row.get("code_type")),
                        setting=_setting_of(str(row.get("service_codes") or "")),
                        billing_class=_BILLING_CLASS_TO_HOSPITAL.get(
                            str(row.get("billing_class") or "").strip().casefold()
                        ),
                        payer=canonical.get(stem) or stem,
                        plan=source_file.network if source_file else None,
                        product_class=TIC_PRODUCT_CLASS,
                        rate_kind=_rate_kind_of(rate_type),
                        rate_dollar=float(rate),
                        methodology=rate_type or None,
                        vintage=source_file.vintage if source_file else None,
                        location=system,
                    )
                )
    return rates


def _systems_of(row: Mapping[str, Any], attribution: SystemAttribution) -> list[str]:
    """The systems a row is attributed to, under the chosen policy."""
    raw = str(row.get("systems") or "").strip()
    if not raw:
        return []
    names = [name.strip() for name in raw.split(",") if name.strip()]
    if attribution is SystemAttribution.EXPLODE:
        return names
    return names if len(names) == 1 else []


def _facilities_of(system: str, facilities: Mapping[str, Sequence[str]] | None) -> Sequence[str]:
    """Facilities to attribute a system-level rate to, or the system itself."""
    if not facilities:
        return (system,)
    return facilities.get(system) or (system,)


def _rate_kind_of(rate_type: str) -> str:
    """``dollar`` or ``percentage``, from the upstream methodology label.

    A ``percentage`` row is a percent of billed charges in the range 10-100.
    Typing it as a dollar would let the comparability layer subtract 58 from a
    hospital's $12,000 and call the result a variance.
    """
    return "percentage" if rate_type.strip().casefold() == "percentage" else "dollar"


def _setting_of(service_codes: str) -> str | None:
    """Inpatient, outpatient or both, from the pipe-joined place-of-service list.

    Returns ``both`` for an unrestricted rate, which is what the hospital side
    also calls a rate that is not setting-specific.
    """
    text = service_codes.strip()
    if text in _UNRESTRICTED_SERVICE_CODES:
        return "both"
    codes = {part.strip() for part in text.split("|") if part.strip()}
    if not codes:
        return "both"
    inpatient = bool(codes & _INPATIENT_POS)
    outpatient = bool(codes & _OUTPATIENT_POS)
    if inpatient and outpatient:
        return "both"
    if inpatient:
        return "inpatient"
    if outpatient:
        return "outpatient"
    # A list naming only office and other non-facility places -- real, but not
    # a setting the hospital side distinguishes.
    return None


def _canonical_payers(stems: set[str], by_stem: Mapping[str, PayerFile]) -> dict[str, str]:
    """Resolve config labels to canonical contracting parties with the A2 matcher.

    The same rule-based matcher the eval scores is the one that runs here, so
    the score describes the pipeline rather than a parallel implementation.

    The labels need one accommodation the hospital side does not: they are
    filenames, not payer strings, so ``AetnaALIC_OpenAccessManagedChoice``
    normalises to a key the alias table has never seen. Where the whole label
    fails to resolve, the carrier half is tried on its own, and failing that
    the longest known alias the label starts with. Both retries go back through
    the same matcher rather than around it.
    """
    ordered = sorted(stems)
    matcher = RuleBasedMatcher()
    candidates = [
        PayerCandidate(payer_raw=stem, plan_raw=by_stem[stem].network if stem in by_stem else "")
        for stem in ordered
    ]
    resolved: dict[str, str] = {}
    unresolved: list[str] = []
    for stem, proposal in zip(ordered, matcher.propose(candidates), strict=True):
        if proposal.canonical_payer:
            resolved[stem] = proposal.canonical_payer
        else:
            unresolved.append(stem)

    if not unresolved:
        return resolved

    retries = [PayerCandidate(payer_raw=_carrier_alias(stem), plan_raw="") for stem in unresolved]
    for stem, proposal in zip(unresolved, matcher.propose(retries), strict=True):
        if proposal.canonical_payer:
            resolved[stem] = proposal.canonical_payer
    return resolved


_ALIASES_BY_LENGTH = sorted(
    ((alias, canonical) for canonical, aliases in CANONICAL_PAYERS.items() for alias in aliases),
    key=lambda pair: -len(pair[0]),
)


def _carrier_alias(stem: str) -> str:
    """The known payer alias a config label starts with, else its carrier half.

    ``AetnaALIC_Hmo`` starts with ``aetna``; ``UHC_NY_ChoicePlus`` starts with
    ``uhc``. Longest alias first, so ``united healthcare`` is preferred over
    ``united``.
    """
    lowered = re.sub(r"[^a-z]", "", stem.casefold())
    for alias, _canonical in _ALIASES_BY_LENGTH:
        if lowered.startswith(re.sub(r"[^a-z]", "", alias.casefold())):
            return alias
    return split_label(stem)[0]


def _optional(value: Any) -> str | None:  # noqa: ANN401 - an Arrow cell, any scalar type
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def load_comparable_rates(
    root: Path,
    where: PayerFilter | None = None,
    *,
    facilities: Mapping[str, Sequence[str]] | None = None,
    canonicalise_payers: bool = True,
) -> list[ComparableRate]:
    """Read the payer Parquet straight to comparable rates."""
    where = where or PayerFilter()
    files = discover_payer_files(root)
    table = aggregate_rates(open_payer_dataset(files), where)
    return to_comparable_rates(
        table,
        files,
        facilities=facilities,
        attribution=where.attribution,
        canonicalise_payers=canonicalise_payers,
    )


def distinct_systems(root: Path) -> dict[str, int]:
    """Row counts per health system, exploded, for a caller building a map."""
    files = discover_payer_files(root)
    table = open_payer_dataset(files).to_table(columns=["systems"])
    counts: dict[str, int] = {}
    for value in table.column("systems").to_pylist():
        for name in str(value or "").split(","):
            name = name.strip()
            if name:
                counts[name] = counts.get(name, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: -item[1]))


def _skip_reason(payer_file: PayerFile) -> str:
    """Why a file was left out, in the order the checks apply."""
    if payer_file.is_duplicate:
        return "duplicate of a National file"
    if payer_file.is_quarantined:
        return f"contract: {payer_file.contract_errors[0]}"
    return ""


def file_summary(root: Path) -> list[dict[str, Any]]:
    """What was read and what was skipped, for the load audit.

    Reports in-flight and superseded files explicitly rather than letting a
    glob silently define the dataset, because an absent payer file is ambiguous
    between "not yet parsed" and "parsed, no target rows".

    A file quarantined by the contract is reported here for the same reason:
    dropping it from the dataset without saying so would turn a broken payer
    file into a quietly smaller number.
    """
    completed = discover_payer_files(root, include_duplicates=True, include_quarantined=True)
    # ``Path.stem`` strips one suffix, leaving "X.parquet" on an "X.parquet.part".
    in_flight = sorted(p.name.removesuffix(".parquet.part") for p in root.glob("*.parquet.part"))
    return [
        {
            "stem": f.stem,
            "carrier": f.carrier,
            "network": f.network,
            "vintage": f.vintage,
            "read": not (f.is_duplicate or f.is_quarantined),
            "skipped_reason": _skip_reason(f),
            "contract_errors": list(f.contract_errors),
        }
        for f in completed
    ] + [
        {
            "stem": stem,
            "carrier": split_label(stem)[0],
            "network": split_label(stem)[1],
            "vintage": PAYER_SOURCE_VINTAGES.get(stem),
            "read": False,
            "skipped_reason": "parse in flight (.part has no footer)",
            "contract_errors": [],
        }
        for stem in in_flight
    ]


__all__ = [
    "DUPLICATE_PAYER_FILES",
    "NEEDED_COLUMNS",
    "PAYER_SOURCE_VINTAGES",
    "TIC_PRODUCT_CLASS",
    "UNTRANSLATABLE_CODE_TYPES",
    "PayerFile",
    "PayerFilter",
    "SystemAttribution",
    "aggregate_rates",
    "discover_payer_files",
    "distinct_systems",
    "file_summary",
    "load_comparable_rates",
    "open_payer_dataset",
    "split_label",
    "to_comparable_rates",
]
