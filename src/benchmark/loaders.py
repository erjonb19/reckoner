"""Load the published CMS fee schedules that price a benchmark.

Three schedules, three licensing situations, and they are not the same:

* **IPPS** (inpatient, MS-DRG) is fully public. Table 5 carries the relative
  weight, Tables 2 and 3 the wage index, Tables 1A/1B the standardized amounts.
  Everything needed to price an inpatient rate is downloadable without a click.
* **OPPS** (outpatient, APC) publishes Addendum B keyed on HCPCS. CPT codes
  inside it are AMA-licensed, so CMS puts the file behind a licence
  click-through at ``/apps/ama/license.asp``.
* **PFS** (professional, CPT/HCPCS) is keyed on CPT throughout and sits behind
  the same gate.

The loaders for the gated files are written and tested; they simply need a file
the licence holder downloaded. Nothing here fetches from behind a click-through.

Two layout rules, learned from the files rather than assumed:

* **Columns are found by header text, not by index.** CMS renumbers and
  reorders columns between fiscal years, and the header cells carry footnote
  digits (``3,6 FY 2025 Wage Index...``) and non-breaking spaces. A fixed index
  is correct exactly once.
* **The IPPS standardized amount has two regimes.** Labor share is 67.6% where
  the hospital's wage index exceeds 1 and 62% where it does not (Tables 1A and
  1B). Applying one regime to every hospital misprices every rate in the other
  half, so both are loaded and the regime is chosen per hospital.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from benchmark.models import (
    BenchmarkRate,
    HospitalGeography,
    IppsParameters,
    Schedule,
)

#: CMS header cells carry footnote markers and non-breaking spaces. Written
#: as escapes because a literal NBSP is indistinguishable from a space in
#: source -- these captions really do contain them ("FY\xa02025 Wage Index").
_HEADER_NOISE = re.compile(r"[\s\xa0]+")
_LEADING_FOOTNOTE = re.compile(r"^[\d,\s\xa0]+")

#: Rows in Table 5 that are not payable DRGs.
_UNGROUPABLE = frozenset({"998", "999"})


class SourceFormatError(Exception):
    """The file does not look like the CMS publication it claims to be.

    Raised only by the loaders, which run once at startup against a file the
    operator chose. This is not the ingest path -- there is no row to
    quarantine, and a silently empty benchmark index is far worse than a stop.
    """


def _normalise_header(cell: str) -> str:
    """Strip footnote digits and whitespace so a header can be matched by text."""
    text = _HEADER_NOISE.sub(" ", cell).strip()
    text = _LEADING_FOOTNOTE.sub("", text).strip()
    return text.casefold()


def _find_column(header: list[str], *wanted: str) -> int:
    """Index of the first column whose normalised header contains every phrase.

    Phrases are matched against the normalised header, so callers pass the
    stable part of a caption and stay insensitive to the footnote markers CMS
    moves around each year.
    """
    normalised = [_normalise_header(cell) for cell in header]
    for index, cell in enumerate(normalised):
        if all(phrase.casefold() in cell for phrase in wanted):
            return index
    raise SourceFormatError(f"no column matching {wanted!r} in {normalised!r}")


def _number(value: str | None) -> float | None:
    """Parse a CMS numeric cell, which may carry $ , % or padding."""
    if value is None:
        return None
    cleaned = re.sub(r"[$,%\s\xa0]", "", value)
    if not cleaned or cleaned in {"-", "N/A", "NA"}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _rows(text: str) -> Iterator[list[str]]:
    """Tab-separated CMS text tables, honouring the quoting they use for titles."""
    yield from csv.reader(io.StringIO(text), delimiter="\t")


#: A CMS table zip usually ships the final rule *and* the correction notice for
#: the same table, and they differ: FY2025 Table 5 gives DRG 002 a weight of
#: 9.4038 in the final rule and 9.4045 in the correction notice. The correction
#: notice supersedes, so it is preferred -- but the choice is made here, in the
#: open, rather than falling out of whichever filename sorts first.
_CORRECTION_MARKERS = ("-cn ", " cn ", "correction")


@dataclass(frozen=True)
class _Member:
    """One file found inside a CMS archive, with the name it was found under."""

    name: str
    text: str

    @property
    def is_correction_notice(self) -> bool:
        lowered = f" {self.name.casefold()} "
        return any(marker in lowered for marker in _CORRECTION_MARKERS)


def _read_member(
    path: Path, *, suffix: str, contains: str = "", prefer_correction: bool = True
) -> _Member:
    """Read one member out of a CMS zip, descending through nested zips.

    CMS ships tables as a zip containing a zip, and naming differs between the
    final rule and the correction notice, so members are selected by suffix and
    substring rather than by exact name. Where both publications are present the
    correction notice wins by default, and the member's name travels with the
    text so the caller can record which publication a number came from.
    """
    with zipfile.ZipFile(path) as archive:
        found = _collect_members(archive, suffix=suffix, contains=contains)
    if not found:
        raise SourceFormatError(f"no {contains or suffix!r} member in {path}")
    corrections = [m for m in found if m.is_correction_notice]
    if prefer_correction and corrections:
        return corrections[0]
    plain = [m for m in found if not m.is_correction_notice]
    return (plain or found)[0]


def _collect_members(archive: zipfile.ZipFile, *, suffix: str, contains: str) -> list[_Member]:
    found: list[_Member] = []
    for name in sorted(archive.namelist()):
        if name.lower().endswith(suffix) and contains.casefold() in name.casefold():
            found.append(_Member(name, archive.read(name).decode("utf-8", errors="replace")))
    if found:
        return found
    # Not at this level: descend into any nested archive.
    for name in sorted(archive.namelist()):
        if name.lower().endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(archive.read(name))) as nested:
                found.extend(_collect_members(nested, suffix=suffix, contains=contains))
    return found


#: A header row carries several labelled columns. A title row carries one long
#: caption and then empty cells -- and CMS titles quote the table's own subject
#: ("...MS-DRGS, RELATIVE WEIGHTING FACTORS..."), so matching on text alone
#: finds the title first and yields a one-column header.
_MIN_HEADER_COLUMNS = 3


def _header_and_body(text: str, *, must_contain: str) -> tuple[list[str], list[list[str]]]:
    """Split a CMS table into its header row and its data rows.

    The header is not row 0 -- these files open with one or more title lines --
    so it is located by content: the first row that both carries the caption the
    caller names and actually looks like a header.
    """
    rows = list(_rows(text))
    for index, row in enumerate(rows):
        if sum(1 for cell in row if cell.strip()) < _MIN_HEADER_COLUMNS:
            continue
        if any(must_contain.casefold() in _normalise_header(cell) for cell in row):
            return row, rows[index + 1 :]
    raise SourceFormatError(f"no header row containing {must_contain!r}")


# -- IPPS ----------------------------------------------------------------


def load_ipps_drg_weights(
    path: Path,
    year: int,
    *,
    use_capped_weight: bool = True,
    prefer_correction: bool = True,
) -> list[BenchmarkRate]:
    """Read IPPS Table 5 into MS-DRG relative weights.

    Table 5 publishes two weights per DRG: before the cap, and after the 10%
    cap that limits how far a weight may fall year over year. The capped column
    is the one CMS pays on, so it is the default -- using the uncapped weight
    would misprice exactly the DRGs whose weight moved most.
    """
    member = _read_member(
        path, suffix=".txt", contains="Table 5", prefer_correction=prefer_correction
    )
    header, body = _header_and_body(member.text, must_contain="ms-drg")

    code_at = _find_column(header, "ms-drg")
    title_at = _find_column(header, "ms-drg title")
    weight_at = (
        _find_column(header, "weights", "cap applied")
        if use_capped_weight
        else _find_column(header, "weights", "before cap")
    )

    rates: list[BenchmarkRate] = []
    for row in body:
        if len(row) <= weight_at:
            continue
        code = row[code_at].strip()
        if not code.isdigit() or code in _UNGROUPABLE:
            continue
        weight = _number(row[weight_at])
        if weight is None or weight <= 0:
            continue
        rates.append(
            BenchmarkRate(
                schedule=str(Schedule.IPPS),
                code=code,
                code_type="MS-DRG",
                year=year,
                weight=weight,
                description=row[title_at].strip() if title_at < len(row) else None,
                source=f"IPPS Table 5 FY{year} ({member.name})",
            )
        )
    if not rates:
        raise SourceFormatError(f"IPPS Table 5 at {path} yielded no weights")
    return rates


@dataclass(frozen=True)
class IppsStandardizedAmounts:
    """Tables 1A and 1B: the standardized amount, in both labor-share regimes.

    CMS splits the national amount into a labor-related and a nonlabor-related
    portion, and which split applies depends on the hospital's own wage index --
    67.6/32.4 above 1, 62/38 at or below. One regime cannot stand in for the
    other: at a wage index of 1.4 the difference is several hundred dollars a
    discharge.
    """

    fiscal_year: int
    source: str
    #: Wage index > 1 (Table 1A).
    high_labor_related: float
    high_nonlabor_related: float
    #: Wage index <= 1 (Table 1B).
    low_labor_related: float
    low_nonlabor_related: float

    def for_wage_index(self, wage_index: float) -> IppsParameters:
        """The parameters that apply to a hospital with this wage index."""
        if wage_index > 1.0:
            labor, nonlabor = self.high_labor_related, self.high_nonlabor_related
        else:
            labor, nonlabor = self.low_labor_related, self.low_nonlabor_related
        total = labor + nonlabor
        return IppsParameters(
            fiscal_year=self.fiscal_year,
            labor_share=labor / total,
            nonlabor_share=nonlabor / total,
            standardized_amount=total,
            source=self.source,
        )


def load_ipps_standardized_amounts(
    path: Path, year: int, *, prefer_correction: bool = True
) -> IppsStandardizedAmounts:
    """Read Tables 1A and 1B.

    Only the full-update column is taken -- the hospital submitted quality data
    and is a meaningful EHR user. The reduced-update columns apply to hospitals
    that did not, which is a hospital-specific fact this project does not carry;
    using a reduced amount for everyone would understate every inpatient
    benchmark.
    """
    member = _read_member(path, suffix=".txt", contains="1A", prefer_correction=prefer_correction)
    tables = _split_numbered_tables(member.text)
    high = _first_amount_pair(tables, "table 1a", path)
    low = _first_amount_pair(tables, "table 1b", path)
    return IppsStandardizedAmounts(
        fiscal_year=year,
        source=f"IPPS Tables 1A/1B FY{year} ({member.name})",
        high_labor_related=high[0],
        high_nonlabor_related=high[1],
        low_labor_related=low[0],
        low_nonlabor_related=low[1],
    )


def _split_numbered_tables(text: str) -> dict[str, list[list[str]]]:
    """Group the rows of a combined Tables 1A-1E file under their table caption."""
    tables: dict[str, list[list[str]]] = {}
    current: str | None = None
    for row in _rows(text):
        caption = next(
            (
                _normalise_header(cell)[:8]
                for cell in row
                if _normalise_header(cell).startswith("table 1")
            ),
            None,
        )
        if caption:
            current = caption
            tables[current] = []
        elif current:
            tables[current].append(row)
    return tables


def _first_amount_pair(
    tables: dict[str, list[list[str]]], caption: str, path: Path
) -> tuple[float, float]:
    """First (labor, nonlabor) dollar pair under a table caption.

    The first numeric pair is the full-update column; the reduced-update columns
    follow it on the same row.
    """
    for row in tables.get(caption, []):
        values = [_number(cell) for cell in row]
        pair = [v for v in values if v is not None]
        if len(pair) >= 2 and pair[0] > 1000 and pair[1] > 100:
            return pair[0], pair[1]
    raise SourceFormatError(f"no standardized amounts under {caption!r} in {path}")


@dataclass(frozen=True)
class WageIndexRecord:
    """One hospital's wage index and the CBSA it is paid under."""

    ccn: str
    wage_index: float | None
    geographic_cbsa: str | None
    payment_cbsa: str | None
    case_mix_index: float | None = None
    county: str | None = None

    @property
    def cbsa(self) -> str | None:
        """The CBSA that actually governs payment, which may be a reclassification.

        A hospital reclassified under the MGCRB is paid at another area's wage
        index. Reporting its geographic CBSA would explain the wrong number.
        """
        return self.payment_cbsa or self.geographic_cbsa


def load_ipps_wage_index_by_ccn(
    path: Path, *, prefer_correction: bool = True
) -> dict[str, WageIndexRecord]:
    """Read IPPS Table 2: wage index and CBSA for every hospital, keyed by CCN."""
    member = _read_member(
        path, suffix=".txt", contains="Tables 2", prefer_correction=prefer_correction
    )
    header, body = _header_and_body(member.text, must_contain="ccn")

    ccn_at = _find_column(header, "ccn")
    wage_at = _find_column(header, "wage index", "quartile and cap")
    geo_at = _find_column(header, "geographic cbsa")
    pay_at = _find_column(header, "wage index payment cbsa")
    cmi_at = _find_column(header, "case-mix indexes")
    county_at = _find_column(header, "county name")

    def cell(row: list[str], index: int) -> str | None:
        value = row[index].strip() if index < len(row) else ""
        return value or None

    records: dict[str, WageIndexRecord] = {}
    for row in body:
        ccn = cell(row, ccn_at)
        if not ccn or not ccn.strip().isdigit():
            continue
        records[ccn.strip()] = WageIndexRecord(
            ccn=ccn.strip(),
            wage_index=_number(cell(row, wage_at)),
            geographic_cbsa=cell(row, geo_at),
            payment_cbsa=cell(row, pay_at),
            case_mix_index=_number(cell(row, cmi_at)),
            county=cell(row, county_at),
        )
    if not records:
        raise SourceFormatError(f"IPPS Table 2 at {path} yielded no hospitals")
    return records


def load_ipps_wage_index_by_cbsa(path: Path, *, prefer_correction: bool = True) -> dict[str, float]:
    """Read IPPS Table 3: wage index by CBSA, for hospitals absent from Table 2."""
    member = _read_member(
        path, suffix=".txt", contains="Tables 3", prefer_correction=prefer_correction
    )
    header, body = _header_and_body(member.text, must_contain="cbsa")

    cbsa_at = _find_column(header, "cbsa")
    wage_at = _find_column(header, "wage index")

    by_cbsa: dict[str, float] = {}
    for row in body:
        if len(row) <= max(cbsa_at, wage_at):
            continue
        cbsa = row[cbsa_at].strip()
        wage = _number(row[wage_at])
        # A CBSA appears more than once where a state spans it; the first row
        # carries the unreclassified index, which is the one to keep.
        if cbsa and wage is not None:
            by_cbsa.setdefault(cbsa, wage)
    if not by_cbsa:
        raise SourceFormatError(f"IPPS Table 3 at {path} yielded no areas")
    return by_cbsa


# -- OPPS and PFS (licence-gated) ----------------------------------------


def load_opps_addendum_b(path: Path, year: int) -> list[BenchmarkRate]:
    """Read OPPS Addendum B into HCPCS-keyed national payment rates.

    Addendum B is behind the AMA licence click-through because it names CPT
    codes. Download it as the licence holder and pass the file here; this
    function never fetches it.

    Only separately payable status indicators are kept. A packaged code (status
    N) has no payment of its own -- its cost is bundled into the procedure it
    accompanies -- so treating its blank rate as zero would report every
    negotiated rate for it as an infinite percent of Medicare.
    """
    text = _read_tabular(path)
    header, body = _header_and_body(text, must_contain="hcpcs")

    code_at = _find_column(header, "hcpcs")
    rate_at = _find_column(header, "payment rate")
    status_at = _find_column(header, "status indicator")
    desc_at = _find_column(header, "short descriptor")

    rates: list[BenchmarkRate] = []
    for row in body:
        if len(row) <= max(code_at, rate_at):
            continue
        code = row[code_at].strip().upper()
        amount = _number(row[rate_at])
        status = row[status_at].strip().upper() if status_at < len(row) else ""
        if not code or amount is None or amount <= 0:
            continue
        if status in _PACKAGED_STATUS_INDICATORS:
            continue
        rates.append(
            BenchmarkRate(
                schedule=str(Schedule.OPPS),
                code=code,
                code_type="HCPCS",
                year=year,
                amount=amount,
                description=row[desc_at].strip() if desc_at < len(row) else None,
                source=f"OPPS Addendum B CY{year}",
            )
        )
    if not rates:
        raise SourceFormatError(f"OPPS Addendum B at {path} yielded no rates")
    return rates


#: OPPS status indicators that are not separately payable. Their payment is
#: bundled into another line, so they have no standalone benchmark.
_PACKAGED_STATUS_INDICATORS = frozenset({"N", "E", "E1", "E2", "B", "C", "M", "W", "Y"})


def load_pfs_rvu(
    path: Path, year: int, conversion_factor: float, *, locality: str | None = None
) -> list[BenchmarkRate]:
    """Read a PFS RVU file into CPT/HCPCS-keyed national payment amounts.

    Also behind the AMA licence gate. The conversion factor has no default and
    must be passed from the PFS final rule -- it changes every year, and a stale
    one rescales every professional comparison silently.

    The non-facility total RVU is used, priced at national GPCIs of 1.0. That is
    a *national* benchmark, not a locality-adjusted one: applying it to a NY
    hospital without the locality GPCIs overstates percent-of-Medicare in
    high-cost areas. Pass ``locality`` only to label what was loaded.
    """
    if conversion_factor <= 0:
        raise ValueError(f"conversion factor must be positive: {conversion_factor}")

    text = _read_tabular(path)
    header, body = _header_and_body(text, must_contain="hcpcs")

    code_at = _find_column(header, "hcpcs")
    work_at = _find_column(header, "work rvu")
    pe_at = _find_column(header, "non-fac pe rvu")
    mp_at = _find_column(header, "mp rvu")
    status_at = _find_column(header, "status code")

    rates: list[BenchmarkRate] = []
    for row in body:
        if len(row) <= max(code_at, work_at, pe_at, mp_at):
            continue
        code = row[code_at].strip().upper()
        status = row[status_at].strip().upper() if status_at < len(row) else ""
        # Status A and R are payable; everything else is bundled, carrier-priced
        # or non-covered, and has no national amount to benchmark against.
        if not code or status not in {"A", "R"}:
            continue
        components = [_number(row[at]) for at in (work_at, pe_at, mp_at)]
        if any(value is None for value in components):
            continue
        total_rvu = sum(value for value in components if value is not None)
        if total_rvu <= 0:
            continue
        rates.append(
            BenchmarkRate(
                schedule=str(Schedule.PFS),
                code=code,
                code_type="CPT",
                year=year,
                amount=round(total_rvu * conversion_factor, 2),
                source=f"PFS RVU CY{year} at CF {conversion_factor}"
                + (f", locality {locality}" if locality else ", national GPCIs"),
            )
        )
    if not rates:
        raise SourceFormatError(f"PFS RVU file at {path} yielded no rates")
    return rates


def _read_tabular(path: Path) -> str:
    """Read a CMS table from a zip, csv or txt path, whichever the operator has."""
    if path.suffix.lower() == ".zip":
        for suffix in (".csv", ".txt"):
            try:
                return _read_member(path, suffix=suffix).text
            except SourceFormatError:
                continue
        raise SourceFormatError(f"no csv or txt member in {path}")
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    # Addendum B ships as CSV, the RVU file as tab-separated or fixed columns.
    # Normalising commas to tabs here would corrupt quoted descriptors, so the
    # delimiter is sniffed on the header instead.
    return text if "\t" in text.split("\n", 1)[0] else _commas_to_tabs(text)


def _commas_to_tabs(text: str) -> str:
    out = io.StringIO()
    writer = csv.writer(out, delimiter="\t", lineterminator="\n")
    writer.writerows(csv.reader(io.StringIO(text)))
    return out.getvalue()


# -- hospital geography --------------------------------------------------


def geography_for(
    hospital: str,
    ccn: str | None,
    by_ccn: dict[str, WageIndexRecord],
    by_cbsa: dict[str, float] | None = None,
) -> HospitalGeography | None:
    """Assemble the geography a hospital needs to price an IPPS weight.

    Returns None rather than a zero-filled record when the hospital cannot be
    located: percent_of_medicare turns a missing geography into a reason code,
    and a fabricated wage index of 1.0 would instead produce a plausible,
    confidently wrong number.
    """
    if not ccn:
        return None
    record = by_ccn.get(ccn.strip())
    if record is None:
        return None

    wage_index = record.wage_index
    if wage_index is None and by_cbsa and record.cbsa:
        wage_index = by_cbsa.get(record.cbsa)
    if wage_index is None:
        return None

    return HospitalGeography(
        hospital=hospital,
        cbsa=record.cbsa,
        wage_index=wage_index,
        pfs_locality=None,
    )
