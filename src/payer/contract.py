"""The declared shape of the payer Parquet, checked against the files themselves.

``mrf_pipeline`` writes the Parquet that :mod:`payer.curated` reads, and until now
that interface existed only as whatever the reader happened to assume. An
undeclared interface fails quietly: the parser had been stamping
``last_updated_on`` onto every row for months while the reader dated 12 of 120
files from a hardcoded map, and nothing was wrong enough to break, so nothing
broke. This module is that interface written down, in code rather than prose, so
a drift is a named violation instead of a number that goes subtly wrong.

Three kinds of rule, and the third is the one that earns its keep:

* **Shape** -- the columns that must exist and their logical types. ``string`` and
  ``large_string`` are the same logical type and both are accepted; 104 files use
  one and 16 use the other, which is a difference in how PyArrow happened to
  write them rather than a difference in the data.
* **Domain** -- what a column may contain. For ``billing_class`` and ``rate_type``
  these are not observations, they are the enums the federal Transparency in
  Coverage schema defines, so a new value means a payer has departed from the
  spec rather than that our list was short.
* **File invariants** -- what must be constant within one file. These exist
  because live code already depends on them. ``_file_vintage`` reads
  ``last_updated_on`` from the first row group and treats it as the file's
  vintage, which is only sound if the file carries one. ``discover_payer_files``
  splits the filename into carrier and network, which is only sound if the
  ``payer`` column agrees with the filename. Both assumptions were true when
  written and neither was checked.

Nothing here raises on a violation. Rows failing validation are quarantined with
a reason rather than aborting a load (CLAUDE.md architecture rule 4), and a
contract that stopped the pipeline the first time a payer added a code system
would be worse than the silence it replaces. Callers get a report and decide.

    python -m payer.contract --payer-root ../mrf_pipeline/payer_parquet
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

#: Bumped when a rule changes meaning, so a stored report says what it was
#: checked against. A new *optional* column does not bump it; a changed type,
#: a narrowed domain or a new invariant does.
CONTRACT_VERSION = 1

#: The two enums Transparency in Coverage actually defines. Worth stating
#: separately from the rest: a value outside these is a payer diverging from the
#: federal schema, not a gap in our observation.
TIC_BILLING_CLASSES = frozenset({"professional", "institutional"})
TIC_RATE_TYPES = frozenset({"negotiated", "derived", "fee schedule", "percentage", "per diem"})

#: Code systems seen across the 120 files on hand. Unlike the two above this is
#: an observation, not a spec: TiC lets a payer name its own code type, and
#: LOCAL and CSTM-ALL are exactly that. A new entry here is information, not
#: necessarily an error -- which is why it is reported at a lower severity.
KNOWN_CODE_TYPES = frozenset(
    {
        "CPT",
        "HCPCS",
        "MS-DRG",
        "APR-DRG",
        "RC",
        "LOCAL",
        "CDT",
        "CSTM-ALL",
        "ICD",
        "HIPPS",
        "PROC",
    }
)

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ContractCheck(StrEnum):
    """How deeply to check a file on the way in.

    ``SCHEMA`` reads the Parquet footer only and costs 0.03s across 120 files;
    ``FULL`` reads every column and costs roughly two minutes. The gap is why
    the load gate defaults to the former and the CLI to the latter.
    """

    NONE = "none"
    SCHEMA = "schema"
    FULL = "full"


class Severity(StrEnum):
    """How much a violation should worry the caller.

    ``ERROR`` means something downstream will be wrong: a missing column, a type
    the reader cannot handle, a broken invariant live code depends on.
    ``WARNING`` means the data moved in a way worth a human look but which the
    reader survives -- a new code system, a handful of blank codes.
    """

    ERROR = "error"
    WARNING = "warning"


class Rule(StrEnum):
    MISSING_COLUMN = "missing_column"
    WRONG_TYPE = "wrong_type"
    UNEXPECTED_NULL = "unexpected_null"
    VALUE_NOT_ALLOWED = "value_not_allowed"
    UNKNOWN_CODE_TYPE = "unknown_code_type"
    OUT_OF_RANGE = "out_of_range"
    EMPTY_STRING = "empty_string"
    NOT_CONSTANT_IN_FILE = "not_constant_in_file"
    LABEL_DISAGREES_WITH_FILENAME = "label_disagrees_with_filename"
    MALFORMED_DATE = "malformed_date"
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class Violation:
    """One rule broken in one file, with the number of rows behind it."""

    file: str
    column: str
    rule: Rule
    severity: Severity
    rows: int = 0
    detail: str = ""

    def describe(self) -> str:
        where = f"{self.file}.{self.column}" if self.column else self.file
        count = f" ({self.rows:,} rows)" if self.rows else ""
        return f"[{self.severity}] {where}: {self.rule}{count} {self.detail}".rstrip()


def _logical(kind: pa.DataType) -> str:
    """Collapse the storage type to the logical one the contract cares about.

    ``string`` and ``large_string`` differ only in offset width. Treating them as
    distinct would report 16 of 120 files as violations for a choice PyArrow made
    on their behalf.
    """
    if pa.types.is_string(kind) or pa.types.is_large_string(kind):
        return "string"
    if pa.types.is_integer(kind):
        return "integer"
    if pa.types.is_floating(kind):
        return "floating"
    return str(kind)


@dataclass(frozen=True)
class ColumnSpec:
    """What one column must look like."""

    name: str
    logical: str
    #: Values the column may take. ``None`` means unconstrained.
    allowed: frozenset[str] | None = None
    #: Reported as WARNING rather than ERROR when `allowed` is violated.
    allowed_is_observation: bool = False
    nullable: bool = False
    #: Severity to report an empty string at, or ``None`` to allow it. A severity
    #: rather than a flag because the consequence differs by column: an empty
    #: ``billing_code`` is inert, since the code is the join key and the hospital
    #: side has none, so the row simply never matches. An empty ``billing_class``
    #: or ``payer`` would instead be silently mis-grouped.
    non_empty: Severity | None = None
    minimum: float | None = None
    #: Must hold exactly one distinct value across the whole file.
    constant_in_file: bool = False
    #: Must parse as YYYY-MM-DD.
    iso_date: bool = False
    #: The reader projects this column, so its absence makes the read raise.
    #: Absence of any other column is drift worth reporting but not worth
    #: dropping a readable file over -- the load gate quarantines on errors, and
    #: over-strictness there costs real data. Kept in step with
    #: ``curated.NEEDED_COLUMNS`` by test.
    required_for_read: bool = False


COLUMNS: tuple[ColumnSpec, ...] = (
    # 196 rows across 49 Emblem files carry no code. They cannot mis-join -- the
    # hospital side has zero empty codes -- so they are dead weight rather than a
    # correctness risk, and a contract that fails permanently on them is one
    # people learn to ignore.
    ColumnSpec("billing_code", "string", non_empty=Severity.WARNING, required_for_read=True),
    ColumnSpec(
        "code_type",
        "string",
        required_for_read=True,
        allowed=KNOWN_CODE_TYPES,
        allowed_is_observation=True,
        non_empty=Severity.ERROR,
    ),
    ColumnSpec("description", "string"),
    # Zero is legal and load-bearing: 880,612 rows carry it as a placeholder and
    # the comparability layer refuses them by name. Negative is not.
    ColumnSpec("negotiated_rate", "floating", minimum=0.0, required_for_read=True),
    ColumnSpec(
        "rate_type",
        "string",
        allowed=TIC_RATE_TYPES,
        non_empty=Severity.ERROR,
        required_for_read=True,
    ),
    ColumnSpec(
        "billing_class",
        "string",
        allowed=TIC_BILLING_CLASSES,
        non_empty=Severity.ERROR,
        required_for_read=True,
    ),
    ColumnSpec("service_codes", "string", required_for_read=True),
    ColumnSpec("expiration_date", "string"),
    ColumnSpec("matched_npis", "string"),
    ColumnSpec("matched_tins", "string"),
    # Fan-out width, not a count of anything owned. Runs to 514,491.
    ColumnSpec("group_tins", "integer", minimum=1, required_for_read=True),
    ColumnSpec("network_names", "string"),
    ColumnSpec(
        "payer", "string", non_empty=Severity.ERROR, constant_in_file=True, required_for_read=True
    ),
    ColumnSpec("reporting_entity_name", "string", constant_in_file=True),
    ColumnSpec(
        "last_updated_on",
        "string",
        non_empty=Severity.ERROR,
        constant_in_file=True,
        iso_date=True,
    ),
    ColumnSpec("schema_version", "string", constant_in_file=True),
    ColumnSpec("systems", "string", non_empty=Severity.ERROR, required_for_read=True),
    # A row attributed to no system should not have been written at all.
    ColumnSpec("system_count", "integer", minimum=1, required_for_read=True),
)


@dataclass
class ContractReport:
    """What was checked and what it found."""

    version: int = CONTRACT_VERSION
    files: int = 0
    rows: int = 0
    violations: list[Violation] = field(default_factory=list)

    @property
    def errors(self) -> list[Violation]:
        return [v for v in self.violations if v.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Violation]:
        return [v for v in self.violations if v.severity is Severity.WARNING]

    @property
    def ok(self) -> bool:
        """No errors. Warnings do not fail a contract."""
        return not self.errors

    def summary(self) -> dict[str, Any]:
        by_rule: dict[str, int] = {}
        for v in self.violations:
            by_rule[str(v.rule)] = by_rule.get(str(v.rule), 0) + 1
        return {
            "contract_version": self.version,
            "files": self.files,
            "rows": self.rows,
            "ok": self.ok,
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "by_rule": dict(sorted(by_rule.items(), key=lambda kv: -kv[1])),
            "violations": [v.describe() for v in self.violations],
        }


def _check_column(stem: str, spec: ColumnSpec, column: pa.ChunkedArray) -> list[Violation]:
    """Every rule for one column, on one file's worth of it."""
    found: list[Violation] = []
    combined = column.combine_chunks()

    if not spec.nullable and column.null_count:
        found.append(
            Violation(stem, spec.name, Rule.UNEXPECTED_NULL, Severity.ERROR, column.null_count)
        )

    if spec.non_empty is not None and _logical(column.type) == "string":
        blank = pc.sum(pc.equal(pc.utf8_length(combined.cast(pa.string())), 0)).as_py() or 0
        if blank:
            found.append(Violation(stem, spec.name, Rule.EMPTY_STRING, spec.non_empty, int(blank)))

    if spec.minimum is not None:
        below = pc.sum(pc.less(combined, spec.minimum)).as_py() or 0
        if below:
            found.append(
                Violation(
                    stem,
                    spec.name,
                    Rule.OUT_OF_RANGE,
                    Severity.ERROR,
                    int(below),
                    f"below minimum {spec.minimum}",
                )
            )

    distinct = pc.unique(combined).to_pylist() if _logical(column.type) == "string" else []

    if spec.allowed is not None:
        unexpected = {v for v in distinct if v is not None and v not in spec.allowed}
        if unexpected:
            rule = Rule.UNKNOWN_CODE_TYPE if spec.allowed_is_observation else Rule.VALUE_NOT_ALLOWED
            severity = Severity.WARNING if spec.allowed_is_observation else Severity.ERROR
            found.append(
                Violation(
                    stem, spec.name, rule, severity, detail=f"unexpected {sorted(unexpected)[:5]}"
                )
            )

    if spec.constant_in_file and len(distinct) > 1:
        found.append(
            Violation(
                stem,
                spec.name,
                Rule.NOT_CONSTANT_IN_FILE,
                Severity.ERROR,
                detail=f"{len(distinct)} distinct values; code reads row one as the file's",
            )
        )

    if spec.iso_date:
        malformed = sorted(v for v in distinct if not (v and _ISO_DATE.match(str(v))))
        if malformed:
            found.append(
                Violation(
                    stem, spec.name, Rule.MALFORMED_DATE, Severity.ERROR, detail=f"{malformed[:3]}"
                )
            )

    return found


def validate_schema(path: Path) -> tuple[list[Violation], int]:
    """The rules answerable from the Parquet footer alone. Reads no data.

    Split out from the full check because it is effectively free -- 0.03s across
    all 120 files, against roughly two minutes to read every column -- and
    because it covers exactly the failures that break the reader rather than
    merely dirty it. A missing column or a wrong type means a downstream
    ``to_table`` raises; a blank code in one row does not.

    That difference is what makes a default-on load gate affordable. See
    :func:`payer.curated.discover_payer_files`.
    """
    stem = path.stem
    try:
        handle = pq.ParquetFile(path)
        present = set(handle.schema_arrow.names)
        rows = handle.metadata.num_rows
    except (OSError, pa.ArrowInvalid) as exc:
        return [Violation(stem, "", Rule.UNREADABLE, Severity.ERROR, detail=str(exc))], 0

    found: list[Violation] = []
    for spec in COLUMNS:
        if spec.name not in present:
            severity = Severity.ERROR if spec.required_for_read else Severity.WARNING
            found.append(Violation(stem, spec.name, Rule.MISSING_COLUMN, severity))
            continue
        actual = _logical(handle.schema_arrow.field(spec.name).type)
        if actual != spec.logical:
            found.append(
                Violation(
                    stem,
                    spec.name,
                    Rule.WRONG_TYPE,
                    Severity.ERROR,
                    detail=f"expected {spec.logical}, found {actual}",
                )
            )
    return found, rows


def validate_file(path: Path) -> tuple[list[Violation], int]:
    """Check one payer Parquet file completely. Returns violations and row count.

    Columns are read one at a time rather than all eighteen at once. The largest
    file is 6.6M rows, and eighteen wide string columns of it materialised
    together is the shape of problem that took a terminal down; one column is
    bounded and the checks are per-column anyway.
    """
    stem = path.stem
    found, rows = validate_schema(path)
    broken = {v.column for v in found}
    if any(v.rule is Rule.UNREADABLE for v in found):
        return found, rows

    handle = pq.ParquetFile(path)
    present = set(handle.schema_arrow.names)
    for spec in COLUMNS:
        # A column the schema pass already rejected cannot be read for content.
        if spec.name in broken or spec.name not in present:
            continue
        column = pq.read_table(path, columns=[spec.name]).column(spec.name)
        found.extend(_check_column(stem, spec, column))
        del column

    # The filename is load-bearing: discover_payer_files splits it into carrier
    # and network, so a file whose payer column disagrees is mislabelled in
    # every downstream grouping.
    if "payer" in present:
        labels = pc.unique(
            pq.read_table(path, columns=["payer"]).column("payer").combine_chunks()
        ).to_pylist()
        if labels and labels[0] != stem:
            found.append(
                Violation(
                    stem,
                    "payer",
                    Rule.LABEL_DISAGREES_WITH_FILENAME,
                    Severity.ERROR,
                    detail=f"column says {labels[0]!r}",
                )
            )

    return found, rows


def validate_root(root: Path, *, limit: int | None = None) -> ContractReport:
    """Check every completed payer file under ``root``.

    ``.part`` files are skipped rather than reported: an in-flight parse is not
    a contract breach, and its open writer handle cannot be read at all.
    """
    if not root.exists():
        raise FileNotFoundError(f"no payer parquet directory at {root}")

    report = ContractReport()
    paths = sorted(root.glob("*.parquet"))
    if limit is not None:
        paths = paths[:limit]
    for path in paths:
        violations, rows = validate_file(path)
        report.files += 1
        report.rows += rows
        report.violations.extend(violations)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--payer-root", type=Path, required=True)
    parser.add_argument("--limit", type=int, help="check only the first N files")
    parser.add_argument("--json", type=Path, help="write the report here as well as printing it")
    parser.add_argument(
        "--strict", action="store_true", help="exit non-zero on warnings as well as errors"
    )
    args = parser.parse_args(argv)

    report = validate_root(args.payer_root, limit=args.limit)
    summary = report.summary()
    print(json.dumps(summary, indent=1))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(summary, indent=1), encoding="utf-8")
        print(f"\nwritten to {args.json}")

    if report.errors or (args.strict and report.warnings):
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - entrypoint
    sys.exit(main())
