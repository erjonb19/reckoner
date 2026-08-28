"""Service definitions from a contracting rate sheet.

A contracting sheet does not define services as a list of codes. It defines them
as predicates over a claim line:

    Insertion of Permanent Pacemaker
        revenue 0360-0361, 0369, 0481, 0490, 0499, 0750, 0790
        with CPT 33206-33208, 33212-33214, 33221, 33224-33225, 33227-33229

    Radiation Therapy
        revenue 0330, 0339, 0333
        excluding CPT 61796-61800, 77371-77373

Both conditions must hold on the *same* MRF row, which is why the parser has to
keep every code on a row rather than the first.

Two normalisation rules matter:

* Revenue codes are four digits. Sheets write them three ways -- ``172``,
  ``0172``, ``00172`` -- and all mean revenue code 0172. They are left-padded,
  never zero-stripped: revenue 0470 and MS-DRG 470 are different services.
* Ranges are inclusive and expanded at the endpoints' own width, so
  ``0360-0361`` yields two four-digit codes, not 360 and 361.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

#: Clause markers, in the order they must be tested. Exclusion first: a string
#: can contain both "with" and "excluding", and the exclusion wins the tail.
_EXCLUDE_MARKERS = ("excluding", "without", "not including")
_PROCEDURE_MARKERS = (";", " with ", "(with", "cpt code", "cpt codes", "hcpcs code")

# Rate sheets pasted out of Word use en and em dashes for ranges as often as
# hyphens, so all three are accepted. Written as escapes to keep the intent
# visible -- the characters are near-indistinguishable in source.
_DASH = "[-–—]"  # noqa: RUF001 - en/em dashes are intentional here
_CODE = re.compile(rf"\b([A-Z]?\d{{3,5}})\s*{_DASH}\s*([A-Z]?\d{{3,5}})\b|\b([A-Z]?\d{{3,5}})\b")
_REVENUE_WIDTH = 4


class RuleWarning(str):
    """A defect in the source sheet, surfaced rather than silently repaired."""


def normalise_revenue(code: str) -> str:
    """Left-pad a revenue code to four digits, preserving any leading zeros."""
    digits = code.strip().upper()
    return digits.zfill(_REVENUE_WIDTH) if digits.isdigit() else digits


def expand_range(start: str, end: str) -> tuple[list[str], list[RuleWarning]]:
    """Expand an inclusive code range at the endpoints' own width."""
    prefix = ""
    if start[:1].isalpha() and end[:1].isalpha():
        if start[0] != end[0]:
            return [], [RuleWarning(f"range spans two code prefixes: {start}-{end}")]
        prefix, start, end = start[0], start[1:], end[1:]
    if not (start.isdigit() and end.isdigit()):
        return [], [RuleWarning(f"non-numeric range: {start}-{end}")]
    if len(start) != len(end):
        # e.g. "6320-63621" in the source sheet: almost certainly a typo for
        # 63620-63621, but guessing would invent codes.
        return [], [RuleWarning(f"range endpoints differ in width: {start}-{end}")]
    low, high = int(start), int(end)
    if high < low:
        return [], [RuleWarning(f"descending range: {start}-{end}")]
    width = len(start)
    return [f"{prefix}{value:0{width}d}" for value in range(low, high + 1)], []


def parse_codes(text: str) -> tuple[list[str], list[RuleWarning]]:
    """Pull every code and range out of a fragment of sheet text."""
    codes: list[str] = []
    warnings: list[RuleWarning] = []
    for match in _CODE.finditer(text.upper()):
        start, end, single = match.groups()
        if single:
            codes.append(single)
            continue
        expanded, problems = expand_range(start, end)
        codes.extend(expanded)
        warnings.extend(problems)
    return codes, warnings


@dataclass(frozen=True)
class ServiceRule:
    """One row of the rate sheet, as a predicate over an MRF row."""

    category: str
    name: str
    revenue_codes: frozenset[str] = frozenset()
    procedure_codes: frozenset[str] = frozenset()
    excluded_procedure_codes: frozenset[str] = frozenset()
    warnings: tuple[RuleWarning, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (self.revenue_codes or self.procedure_codes)

    def matches(self, row_codes: dict[str, set[str]]) -> bool:
        """True when a row satisfies every clause of this service.

        ``row_codes`` maps a family ("revenue", "procedure") to the codes of
        that family present on the row. All clauses are ANDed, because the
        sheet's "revenue X with CPT Y" means both on the same line.
        """
        revenue = row_codes.get("revenue", set())
        procedure = row_codes.get("procedure", set())

        if self.revenue_codes and not (self.revenue_codes & revenue):
            return False
        if self.procedure_codes and not (self.procedure_codes & procedure):
            return False
        if self.excluded_procedure_codes & procedure:
            return False
        return not self.is_empty


def parse_rule(category: str, name: str, spec: str) -> ServiceRule:
    """Turn one sheet cell into a ServiceRule."""
    text = " ".join(spec.split())
    lowered = text.lower()
    warnings: list[RuleWarning] = []

    excluded_text = ""
    for marker in _EXCLUDE_MARKERS:
        index = lowered.find(marker)
        if index >= 0:
            excluded_text = text[index + len(marker) :]
            text, lowered = text[:index], lowered[:index]
            break

    procedure_text = ""
    for marker in _PROCEDURE_MARKERS:
        index = lowered.find(marker)
        if index >= 0:
            procedure_text = text[index + len(marker) :]
            text = text[:index]
            break

    revenue_raw, revenue_warnings = parse_codes(text)
    procedure_raw, procedure_warnings = parse_codes(procedure_text)
    excluded_raw, excluded_warnings = parse_codes(excluded_text)
    warnings += revenue_warnings + procedure_warnings + excluded_warnings

    return ServiceRule(
        category=category,
        name=name,
        revenue_codes=frozenset(normalise_revenue(c) for c in revenue_raw),
        procedure_codes=frozenset(procedure_raw),
        excluded_procedure_codes=frozenset(excluded_raw),
        warnings=tuple(warnings),
    )


@dataclass(frozen=True)
class Correction:
    """A confirmed repair to the source sheet, applied at parse time."""

    service: str
    find: str
    replace: str
    reason: str = ""
    confirmed_by: str = ""

    def apply(self, service: str, spec: str) -> tuple[str, bool]:
        if service.strip().casefold() != self.service.strip().casefold():
            return spec, False
        if self.find not in spec:
            return spec, False
        return spec.replace(self.find, self.replace), True


def load_corrections(path: Path) -> list[Correction]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [
        Correction(
            service=str(entry["service"]),
            find=str(entry["find"]),
            replace=str(entry["replace"]),
            reason=str(entry.get("reason", "")).strip(),
            confirmed_by=str(entry.get("confirmed_by", "")),
        )
        for entry in payload.get("corrections", [])
        if entry.get("service") and entry.get("find")
    ]


def row_codes(all_codes: str) -> dict[str, set[str]]:
    """Build the match input for one curated row from its stored code list.

    Normalisation happens here, not at ingest: the curated row keeps codes
    exactly as the hospital published them, and hospitals publish revenue codes
    three digits wide ("490") as often as four ("0490").
    """
    import json as _json

    try:
        pairs = _json.loads(all_codes or "[]")
    except ValueError:
        return {"revenue": set(), "procedure": set()}

    revenue: set[str] = set()
    procedure: set[str] = set()
    for entry in pairs:
        if not isinstance(entry, list) or len(entry) != 2:
            continue
        code, code_type = str(entry[0]).strip().upper(), str(entry[1]).strip().upper()
        family = code_type.replace("_", "-")
        if family in {"RC", "REV", "REVENUE", "REVCODE"}:
            revenue.add(normalise_revenue(code))
        elif family in {"CPT", "HCPCS", "APC", "EAPG"}:
            procedure.add(code)
    return {"revenue": revenue, "procedure": procedure}


@dataclass
class ServiceSheet:
    rules: list[ServiceRule] = field(default_factory=list)
    applied_corrections: list[str] = field(default_factory=list)

    @property
    def warnings(self) -> list[tuple[str, RuleWarning]]:
        return [(rule.name, w) for rule in self.rules for w in rule.warnings]

    def all_codes(self) -> set[str]:
        """Every code the sheet references, for use as an ingest filter.

        Revenue codes are emitted in both the padded and unpadded form, because
        the filter runs against codes exactly as published.
        """
        codes: set[str] = set()
        for rule in self.rules:
            for code in rule.revenue_codes:
                codes.add(code)
                codes.add(code.lstrip("0") or "0")
            codes |= rule.procedure_codes
            codes |= rule.excluded_procedure_codes
        return codes

    def classify(self, row_codes: dict[str, set[str]]) -> list[ServiceRule]:
        """Every service a row satisfies. More than one is possible and real."""
        return [rule for rule in self.rules if rule.matches(row_codes)]

    @classmethod
    def from_xlsx(
        cls,
        path: str,
        sheet: str | None = None,
        corrections: list[Correction] | None = None,
    ) -> ServiceSheet:
        import openpyxl

        book = openpyxl.load_workbook(path, read_only=True, data_only=True)
        worksheet = book[sheet] if sheet else book[book.sheetnames[0]]

        rules: list[ServiceRule] = []
        applied: list[str] = []
        category = ""
        for index, row in enumerate(worksheet.iter_rows(values_only=True)):
            if index == 0:
                continue
            name = str(row[0]).strip() if row[0] else ""
            spec = str(row[1]).strip() if len(row) > 1 and row[1] else ""
            if not name:
                continue
            if not spec:
                # A label with no codes is a section heading in this layout.
                category = name
                continue
            for correction in corrections or []:
                spec, did = correction.apply(name, spec)
                if did:
                    applied.append(f"{name}: {correction.find} -> {correction.replace}")
            rules.append(parse_rule(category, name, spec))
        return cls(rules, applied)
