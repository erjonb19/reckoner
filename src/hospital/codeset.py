"""The target code set -- the filter that makes "parse once, land curated" real.

An unfiltered ingest of the NY corpus is hundreds of GB of rows, nearly all of
them codes no cross-hospital comparison will use. docs/SPEC.md scopes the project
to 30-50 high-value services, and this applies that scope at parse time so the
volume never reaches storage.

Matching is on the code string alone. Hospitals disagree about code_type
spelling for the same code -- "MS-DRG" vs "MSDRG" vs "DRG", and CPT codes
labelled "HCPCS" -- so requiring a type match would silently drop real rows.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import yaml


def normalise_code(code: str | None) -> str:
    """Codes compare case-insensitively, with numeric codes unpadded.

    MS-DRG 064 and 64 are the same DRG; hospitals publish both spellings.
    """
    if not code:
        return ""
    stripped = code.strip().upper()
    if stripped.isdigit():
        return stripped.lstrip("0") or "0"
    return stripped


#: Which code-type strings belong to which family. A revenue code and an MS-DRG
#: can be the same digits, so a filter that ignores type over-matches wildly:
#: revenue 0450 (Emergency) would also admit every DRG 450 row.
FAMILY_OF_TYPE = {
    "RC": "revenue",
    "REV": "revenue",
    "REVENUE": "revenue",
    "REVCODE": "revenue",
    "CPT": "procedure",
    "HCPCS": "procedure",
    "APC": "procedure",
    "EAPG": "procedure",
    "MS-DRG": "drg",
    "MSDRG": "drg",
    "DRG": "drg",
    "APR-DRG": "drg",
    "APRDRG": "drg",
    "TRIS-DRG": "drg",
}


def family_of(code_type: str | None) -> str | None:
    return FAMILY_OF_TYPE.get((code_type or "").strip().upper().replace("_", "-"))


def normalise_revenue(code: str) -> str:
    """Revenue codes are four digits wide; pad rather than strip."""
    digits = code.strip().upper()
    return digits.zfill(4) if digits.isdigit() else digits


@dataclass(frozen=True)
class CodeSet:
    codes: frozenset[str]
    labels: dict[str, str]
    #: Family -> codes. When present, matching is type-aware and `codes` is
    #: used only as the untyped fallback for hand-written lists.
    by_family: dict[str, frozenset[str]] = field(default_factory=dict)

    def __contains__(self, code: object) -> bool:
        return isinstance(code, str) and normalise_code(code) in self.codes

    def __len__(self) -> int:
        return len(self.codes)

    def matches_any(self, codes: tuple[tuple[str, str], ...]) -> bool:
        """True if any code on the row is in scope.

        Filtering on the first code alone would drop a row whose revenue code is
        in scope but whose chargemaster id happens to be listed first. When the
        set carries families, a code only counts against its own family.
        """
        if not self.by_family:
            return any(code in self for code, _ in codes)
        for code, code_type in codes:
            family = family_of(code_type)
            if family is None:
                continue
            candidates = self.by_family.get(family)
            if not candidates:
                continue
            key = normalise_revenue(code) if family == "revenue" else code.strip().upper()
            if key in candidates:
                return True
        return False

    def label_for(self, code: str | None) -> str | None:
        return self.labels.get(normalise_code(code))

    @classmethod
    def from_yaml(cls, path: Path) -> CodeSet:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        codes: set[str] = set()
        labels: dict[str, str] = {}
        for group in payload.values():
            for entry in group or []:
                if isinstance(entry, str):
                    key = normalise_code(entry)
                elif isinstance(entry, dict) and entry.get("code"):
                    key = normalise_code(str(entry["code"]))
                    if entry.get("label"):
                        labels[key] = str(entry["label"])
                else:
                    continue
                if key:
                    codes.add(key)
        return cls(frozenset(codes), labels)

    @classmethod
    def from_service_sheet(cls, path: Path, corrections_path: Path | None = None) -> CodeSet:
        """Build the ingest filter from a contracting rate sheet.

        Scope comes from the sheet itself, so adding a service to the sheet is
        all it takes to widen the ingest -- no second list to keep in step.
        """
        from hospital.services import ServiceSheet, load_corrections

        corrections = load_corrections(corrections_path) if corrections_path else []
        sheet = ServiceSheet.from_xlsx(str(path), corrections=corrections)
        revenue: set[str] = set()
        procedure: set[str] = set()
        for rule in sheet.rules:
            revenue |= {normalise_revenue(c) for c in rule.revenue_codes}
            procedure |= {c.strip().upper() for c in rule.procedure_codes}
            procedure |= {c.strip().upper() for c in rule.excluded_procedure_codes}
        labels = {normalise_revenue(c): r.name for r in sheet.rules for c in r.revenue_codes}
        return cls(
            frozenset(revenue | procedure),
            labels,
            {"revenue": frozenset(revenue), "procedure": frozenset(procedure)},
        )

    @classmethod
    def everything(cls) -> CodeSet:
        """A set that matches every code, for an unfiltered run."""
        return _EverythingCodeSet(frozenset(), {})

    def fingerprint(self) -> str:
        """Stable identity of what this set admits.

        Recorded on every load so a re-run can tell "the same file, already
        loaded" from "the same file, but the last load only kept 45 codes of
        it". Without it a widened ingest re-downloads, re-parses and then
        discards its own output as a duplicate, reporting success -- which is
        how a 332,055 row file stayed in the lake as 1,119 rows.
        """
        payload = ",".join(sorted(self.codes))
        if self.by_family:
            payload += "|" + ";".join(
                f"{family}={','.join(sorted(codes))}"
                for family, codes in sorted(self.by_family.items())
            )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


class _EverythingCodeSet(CodeSet):
    def __contains__(self, code: object) -> bool:
        return True

    def matches_any(self, codes: tuple[tuple[str, str], ...]) -> bool:
        return True

    def __len__(self) -> int:
        return 0

    def fingerprint(self) -> str:
        return "all"
