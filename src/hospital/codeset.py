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

from dataclasses import dataclass
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


@dataclass(frozen=True)
class CodeSet:
    codes: frozenset[str]
    labels: dict[str, str]

    def __contains__(self, code: object) -> bool:
        return isinstance(code, str) and normalise_code(code) in self.codes

    def __len__(self) -> int:
        return len(self.codes)

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
    def everything(cls) -> CodeSet:
        """A set that matches every code, for an unfiltered run."""
        return _EverythingCodeSet(frozenset(), {})


class _EverythingCodeSet(CodeSet):
    def __contains__(self, code: object) -> bool:
        return True

    def __len__(self) -> int:
        return 0
