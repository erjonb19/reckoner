"""Profile the payer/plan and methodology composition of a hospital MRF.

This is reconnaissance, not ingest. docs/SPEC.md flags two unmeasured risks --
entity-resolution difficulty and methodology heterogeneity -- and says to sample
real payer/plan fields before committing to the Phase 4 design. This module
answers three questions per file, without landing any rates:

1. Which payer/plan strings does this hospital actually publish?
2. Which products do they represent -- and which are hospital-side only, so can
   never be reconciled against a payer TiC file?
3. What share of rate lines carry a dollar amount at all, rather than an
   algorithm or percentage that is not directly comparable?

Both CMS template layouts are handled: "tall" CSV with payer_name/plan_name
columns, "wide" CSV encoding payer and plan in the column headers, and JSON.
"""

from __future__ import annotations

import codecs
import csv
import html
import io
import re
import sys
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import ijson

from hospital.streaming import MrfStream

# Some MRFs carry very long attestation/description cells.
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

PAIR_SEPARATOR = " || "

#: Ordered product-classification rules. Order matters: a plan named
#: "MAP_Medicare Advantage_MLTC" is a dual-eligible/long-term-care product, not
#: plain Medicare Advantage, so the narrower rules run first. Anything that does
#: not match lands in "unclassified" rather than being forced into a bucket.
PRODUCT_RULES: tuple[tuple[str, str], ...] = (
    # Not payer-specific negotiated rates at all -- these are the cash/uninsured
    # columns some hospitals emit as if they were payers.
    ("non_payer", r"self\s*pay|non\s*contracted|uninsured|charity|cash\s*price"),
    ("dual_or_ltc", r"\bmap\b|\bpace\b|\bmltc\b|\bdual\b|\bd[-\s]?snp\b"),
    # MCR and MCD are the abbreviations hospitals actually use in plan names.
    (
        "medicare_advantage",
        r"medicare\s*(advantage|managed\s*care)|\bmedicare\b|\bmcr\b|\bsenior\b",
    ),
    ("essential_plan", r"\bessential\b"),
    ("medicaid_managed", r"medicaid|\bmcd\b|\bharp\b|\bchp\b|child\s*health"),
    ("exchange_individual", r"exchange|marketplace|\bqhp\b|\bindividual\b"),
    ("ambiguous_all_products", r"all\s*products"),
    ("commercial_aggregate", r"all\s*commercial|all\s*plans"),
    ("commercial", r"commercial|\bgroup\b|\bppo\b|\bhmo\b|\bepo\b|\bpos\b|indemnity"),
)

_COMPILED = tuple((label, re.compile(pattern, re.I)) for label, pattern in PRODUCT_RULES)

#: Products CMS exempts from Transparency in Coverage. Rates for these exist only
#: in hospital-side files, so they can be reported but never reconciled.
#:
#: UNVERIFIED: essential_plan is NY's Basic Health Program under ACA s1331. It is
#: neither Medicaid nor individual-market coverage, and CMS does not name it in
#: the TiC exemption list either way. It is treated as exempt here because a BHP
#: is state-administered rather than issuer-offered group/individual coverage.
#: Confirm against the rule text before any published figure leans on it -- at
#: 11.9% of sampled lines it is large enough to move the headline.
HOSPITAL_ONLY_CLASSES = frozenset(
    {"medicare_advantage", "medicaid_managed", "dual_or_ltc", "essential_plan"}
)

_WIDE_COLUMN = re.compile(
    r"^standard_charge\|(?P<payer>[^|]+)\|(?P<plan>[^|]+)\|(?P<measure>[a-z_]+)$", re.I
)
_MEASURES = {
    "negotiated_dollar": "dollar",
    "negotiated_algorithm": "algorithm",
    "negotiated_percentage": "percentage",
}


#: Real plan names pack several products into one string with punctuation as the
#: separator ("MAP_Medicare Advantage_MLTC", "HARP_Managed Medicaid"). Underscore
#: is a word character, so \b anchors miss unless we normalise first.
_SEPARATORS = re.compile(r"[_/|,;+&()\[\]-]+")

#: Several hospitals append an internal payer code, e.g.
#: "HIGHMARK BLUE CROSS BLUE SHIELD [5143]". Useful for entity resolution later,
#: but noise for classification.
_BRACKETED_CODE = re.compile(r"\[[^\]]*\]")


def normalise_name(value: str | None) -> str:
    """Unescape, strip internal codes, and flatten separators for matching.

    Real files carry HTML entities ("Essential 1&amp;2") because the publisher
    round-tripped the name through a web page.
    """
    text = html.unescape(value or "")
    text = _BRACKETED_CODE.sub(" ", text)
    return re.sub(r"\s+", " ", _SEPARATORS.sub(" ", text)).strip()


def classify_product(payer: str | None, plan: str | None) -> str:
    clean_payer = normalise_name(payer)
    clean_plan = normalise_name(plan)
    text = f"{clean_payer} {clean_plan}"
    for label, pattern in _COMPILED:
        if pattern.search(text):
            return label
    # No product keyword at all. Distinguish "the hospital published no plan
    # detail" (plan echoes the payer, or is blank) from a genuinely unknown
    # string -- the former is a finding about join granularity, not a gap.
    if not clean_plan or clean_plan.casefold() == clean_payer.casefold():
        return "no_plan_detail"
    return "unclassified"


@dataclass
class MrfProfile:
    url: str
    container: str = "?"
    layout: str = "?"
    bytes_scanned: int = 0
    truncated: bool = False
    rate_lines: int = 0
    pairs: Counter[str] = field(default_factory=Counter)
    methodology: Counter[str] = field(default_factory=Counter)
    value_kind: Counter[str] = field(default_factory=Counter)
    product_class: Counter[str] = field(default_factory=Counter)
    #: product_class x value_kind, keyed "class|kind". The reconcilable universe
    #: is a joint condition (commercial AND dollar-denominated); multiplying the
    #: two marginals assumes an independence these fields do not have -- exempt
    #: products lean far more heavily on fee-schedule algorithms.
    joint: Counter[str] = field(default_factory=Counter)
    error: str | None = None

    def record(
        self,
        payer: str | None,
        plan: str | None,
        methodology: str | None,
        dollar: object = None,
        algorithm: object = None,
        percentage: object = None,
    ) -> None:
        payer = (payer or "").strip()
        plan = (plan or "").strip()
        if not payer and not plan:
            return
        self.rate_lines += 1
        self.pairs[f"{payer}{PAIR_SEPARATOR}{plan}"] += 1
        self.methodology[(methodology or "").strip().lower() or "(blank)"] += 1
        product = classify_product(payer, plan)
        self.product_class[product] += 1
        if _present(dollar):
            kind = "dollar"
        elif _present(algorithm):
            kind = "algorithm"
        elif _present(percentage):
            kind = "percentage"
        else:
            kind = "none"
        self.value_kind[kind] += 1
        self.joint[f"{product}|{kind}"] += 1

    @property
    def hospital_only_lines(self) -> int:
        return sum(n for cls, n in self.product_class.items() if cls in HOSPITAL_ONLY_CLASSES)


def _present(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def profile_stream(stream: MrfStream, url: str = "") -> MrfProfile:
    """Consume a decoded MRF stream and return its composition profile."""
    profile = MrfProfile(url=url, container=stream.container)
    reader = stream.reader()
    # Real files start with a UTF-8 BOM often enough that sniffing past it
    # matters: without this a BOM'd JSON file is mistaken for CSV. Consume it so
    # ijson, which does not tolerate one, never sees it.
    if reader.peek(3)[:3] == codecs.BOM_UTF8:
        reader.read(3)
    head = reader.peek(4096).lstrip()
    try:
        if head[:1] == b"{":
            profile.layout = "json"
            _profile_json(reader, profile)
        else:
            _profile_csv(reader, profile)
    except Exception as exc:
        if not stream.truncated:
            profile.error = f"{type(exc).__name__}: {exc}"
    profile.bytes_scanned = stream.decoded_bytes
    profile.truncated = stream.truncated
    return profile


def _profile_json(reader: io.BufferedReader, profile: MrfProfile) -> None:
    try:
        for item in ijson.items(reader, "standard_charge_information.item"):
            for charge in item.get("standard_charges") or []:
                for payer in charge.get("payers_information") or []:
                    profile.record(
                        payer.get("payer_name"),
                        payer.get("plan_name"),
                        payer.get("methodology"),
                        dollar=payer.get("standard_charge_dollar"),
                        algorithm=payer.get("standard_charge_algorithm"),
                        percentage=payer.get("standard_charge_percentage"),
                    )
    except ijson.IncompleteJSONError:
        # Expected whenever the byte cap cut the document short.
        return


def find_header(rows: Iterator[list[str]], max_rows: int = 9) -> list[str] | None:
    """Locate the CMS template column-header row, which is not row 0."""
    for index, row in enumerate(rows):
        if _is_header(row):
            return row
        if index >= max_rows:
            break
    return None


def _is_header(row: list[str]) -> bool:
    lowered = [c.strip().lower() for c in row]
    return "payer_name" in lowered or any(_WIDE_COLUMN.match(c) for c in lowered)


def csv_row_recorder(
    header: list[str], profile: MrfProfile
) -> tuple[Callable[[list[str]], None], str]:
    """Build a per-row recorder for a CSV header, in either template layout.

    Shared by the streaming pass and the range-window sampler so both agree on
    exactly what a row means.
    """
    index_of = {name.strip().lower(): i for i, name in enumerate(header)}
    if "payer_name" in index_of:
        return _tall_recorder(index_of, profile), "csv-tall"
    return _wide_recorder(header, profile), "csv-wide"


def _tall_recorder(index_of: dict[str, int], profile: MrfProfile) -> Callable[[list[str]], None]:
    payer_i = index_of["payer_name"]
    plan_i = index_of.get("plan_name", -1)
    method_i = index_of.get("standard_charge|methodology", -1)
    dollar_i = index_of.get("standard_charge|negotiated_dollar", -1)
    algo_i = index_of.get("standard_charge|negotiated_algorithm", -1)
    pct_i = index_of.get("standard_charge|negotiated_percentage", -1)

    def cell(row: list[str], i: int) -> str | None:
        return row[i] if 0 <= i < len(row) else None

    def record(row: list[str]) -> None:
        if len(row) <= payer_i:
            return
        profile.record(
            row[payer_i],
            cell(row, plan_i),
            cell(row, method_i),
            dollar=cell(row, dollar_i),
            algorithm=cell(row, algo_i),
            percentage=cell(row, pct_i),
        )

    return record


def _wide_recorder(header: list[str], profile: MrfProfile) -> Callable[[list[str]], None]:
    """Wide layout encodes payer and plan in the column name itself."""
    columns: list[tuple[int, str, str, str]] = []
    for i, name in enumerate(header):
        match = _WIDE_COLUMN.match(name.strip())
        if not match:
            continue
        measure = _MEASURES.get(match["measure"].lower())
        if measure:
            columns.append((i, match["payer"].strip(), match["plan"].strip(), measure))

    def record(row: list[str]) -> None:
        for i, payer, plan, measure in columns:
            if i >= len(row) or not row[i].strip():
                continue
            profile.record(
                payer,
                plan,
                None,
                dollar=row[i] if measure == "dollar" else None,
                algorithm=row[i] if measure == "algorithm" else None,
                percentage=row[i] if measure == "percentage" else None,
            )

    return record


def _profile_csv(reader: io.BufferedReader, profile: MrfProfile) -> None:
    text = io.TextIOWrapper(reader, encoding="utf-8-sig", errors="replace", newline="")
    rows = csv.reader(text)

    header = find_header(rows)
    if header is None:
        profile.error = "no CMS template header row found in first 9 rows"
        return

    record, layout = csv_row_recorder(header, profile)
    profile.layout = layout
    for row in rows:
        record(row)
