"""Contract tests, built by breaking a real file one column at a time.

The base table is ``Emblem_HIPHOSH00687``, twenty rows copied verbatim out of
real ``mrf_pipeline`` output, so "valid" means what the pipeline actually
produces rather than what a spec says it should. Each test then damages exactly
one thing and asserts the contract names it.

The point of the exercise is the invariants live code already assumes. #16 reads
``last_updated_on`` from the first row group and treats it as the whole file's
vintage; ``discover_payer_files`` reads carrier and network out of the filename.
Both were true, neither was checked, and the first of them had already been
quietly wrong in the opposite direction for months.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from payer.contract import (
    CONTRACT_VERSION,
    TIC_BILLING_CLASSES,
    TIC_RATE_TYPES,
    ContractReport,
    Rule,
    Severity,
    Violation,
    validate_file,
    validate_root,
)

REAL = Path(__file__).parent.parent / "fixtures" / "payer_parquet" / "Emblem_HIPHOSH00687.parquet"


@pytest.fixture
def base() -> pa.Table:
    return pq.read_table(REAL)


def write(table: pa.Table, root: Path, stem: str = "Emblem_HIPHOSH00687") -> Path:
    path = root / f"{stem}.parquet"
    pq.write_table(table, path)
    return path


def replace(table: pa.Table, name: str, values: list[object], kind: pa.DataType) -> pa.Table:
    """Swap one column for another, keeping position and every other column."""
    index = table.schema.get_field_index(name)
    return table.set_column(index, name, pa.array(values, type=kind))


def rules(violations: list) -> set[Rule]:
    return {v.rule for v in violations}


class TestTheRealFileIsClean:
    def test_a_real_file_passes(self):
        """If this fails the contract describes something other than the data."""
        violations, rows = validate_file(REAL)

        assert rows == 20
        assert violations == []

    def test_the_contract_is_not_vacuous(self, base, tmp_path):
        """A file broken in an obvious way must fail, or the test above proves nothing."""
        path = write(base.drop_columns(["billing_class"]), tmp_path)

        violations, _ = validate_file(path)

        assert Rule.MISSING_COLUMN in rules(violations)


class TestShape:
    def test_large_string_and_string_are_the_same_logical_type(self, base, tmp_path):
        """104 files use one and 16 the other; that is PyArrow's choice, not the data's."""
        as_small = base.cast(
            pa.schema(
                [
                    pa.field(f.name, pa.string() if pa.types.is_large_string(f.type) else f.type)
                    for f in base.schema
                ]
            )
        )
        path = write(as_small, tmp_path)

        violations, _ = validate_file(path)

        assert Rule.WRONG_TYPE not in rules(violations)
        assert violations == []

    def test_a_wrong_type_is_caught(self, base, tmp_path):
        n = base.num_rows
        path = write(replace(base, "negotiated_rate", ["nope"] * n, pa.string()), tmp_path)

        violations, _ = validate_file(path)

        assert Rule.WRONG_TYPE in rules(violations)

    def test_an_unreadable_file_is_reported_not_raised(self, tmp_path):
        """A .part is an open writer handle. The contract must not die on one."""
        broken = tmp_path / "Emblem_X.parquet"
        broken.write_bytes(b"not a parquet file")

        violations, rows = validate_file(broken)

        assert rules(violations) == {Rule.UNREADABLE}
        assert rows == 0


class TestDomains:
    def test_a_billing_class_outside_the_federal_enum_is_an_error(self, base, tmp_path):
        """TiC defines exactly two. A third means a payer left the spec."""
        n = base.num_rows
        path = write(replace(base, "billing_class", ["outpatient"] * n, pa.string()), tmp_path)

        violations, _ = validate_file(path)

        assert Rule.VALUE_NOT_ALLOWED in rules(violations)
        assert any(v.severity is Severity.ERROR for v in violations)
        assert "outpatient" not in TIC_BILLING_CLASSES

    def test_a_rate_type_outside_the_federal_enum_is_an_error(self, base, tmp_path):
        n = base.num_rows
        path = write(replace(base, "rate_type", ["haggled"] * n, pa.string()), tmp_path)

        assert Rule.VALUE_NOT_ALLOWED in rules(validate_file(path)[0])
        assert "haggled" not in TIC_RATE_TYPES

    def test_an_unseen_code_type_warns_rather_than_errors(self, base, tmp_path):
        """TiC lets a payer name its own code system, so this is news, not a breach."""
        n = base.num_rows
        path = write(replace(base, "code_type", ["NDC"] * n, pa.string()), tmp_path)

        violations, _ = validate_file(path)

        assert Rule.UNKNOWN_CODE_TYPE in rules(violations)
        assert all(v.severity is Severity.WARNING for v in violations)

    def test_a_negative_rate_is_an_error_but_zero_is_not(self, base, tmp_path):
        """880,612 real rows are zero placeholders the comparability layer refuses."""
        n = base.num_rows
        zeros = write(replace(base, "negotiated_rate", [0.0] * n, pa.float64()), tmp_path, "Zero_A")
        negatives = write(
            replace(base, "negotiated_rate", [-1.0] * n, pa.float64()), tmp_path, "Neg_A"
        )

        assert Rule.OUT_OF_RANGE not in rules(validate_file(zeros)[0])
        assert Rule.OUT_OF_RANGE in rules(validate_file(negatives)[0])

    def test_a_null_where_none_is_allowed_is_caught(self, base, tmp_path):
        n = base.num_rows
        path = write(replace(base, "billing_class", [None] * n, pa.string()), tmp_path)

        assert Rule.UNEXPECTED_NULL in rules(validate_file(path)[0])


class TestEmptyStringSeverityFollowsConsequence:
    def test_an_empty_billing_code_warns(self, base, tmp_path):
        """It cannot mis-join: the hospital side has no empty codes, so it never matches."""
        codes = base.column("billing_code").to_pylist()
        codes[0] = ""
        path = write(replace(base, "billing_code", codes, pa.string()), tmp_path)

        violations, _ = validate_file(path)

        empty = [v for v in violations if v.rule is Rule.EMPTY_STRING]
        assert [v.severity for v in empty] == [Severity.WARNING]
        assert empty[0].rows == 1

    def test_an_empty_billing_class_errors(self, base, tmp_path):
        """This one mis-groups silently rather than failing to match."""
        classes = base.column("billing_class").to_pylist()
        classes[0] = ""
        path = write(replace(base, "billing_class", classes, pa.string()), tmp_path)

        violations, _ = validate_file(path)

        empty = [v for v in violations if v.rule is Rule.EMPTY_STRING]
        assert [v.severity for v in empty] == [Severity.ERROR]


class TestFileInvariants:
    def test_two_vintages_in_one_file_is_an_error(self, base, tmp_path):
        """_file_vintage reads row one and calls it the file's. This is that assumption."""
        vintages = base.column("last_updated_on").to_pylist()
        vintages[-1] = "2020-01-01"
        path = write(replace(base, "last_updated_on", vintages, pa.string()), tmp_path)

        violations, _ = validate_file(path)

        assert Rule.NOT_CONSTANT_IN_FILE in rules(violations)

    def test_a_payer_column_disagreeing_with_the_filename_is_an_error(self, base, tmp_path):
        """discover_payer_files reads carrier and network out of the filename."""
        n = base.num_rows
        path = write(replace(base, "payer", ["Somebody_Else"] * n, pa.string()), tmp_path)

        assert Rule.LABEL_DISAGREES_WITH_FILENAME in rules(validate_file(path)[0])

    def test_a_malformed_vintage_is_an_error(self, base, tmp_path):
        n = base.num_rows
        path = write(replace(base, "last_updated_on", ["9/4/2026"] * n, pa.string()), tmp_path)

        assert Rule.MALFORMED_DATE in rules(validate_file(path)[0])

    def test_a_row_belonging_to_no_system_is_an_error(self, base, tmp_path):
        n = base.num_rows
        path = write(replace(base, "system_count", [0] * n, pa.int64()), tmp_path)

        assert Rule.OUT_OF_RANGE in rules(validate_file(path)[0])


class TestReport:
    def test_warnings_do_not_fail_the_contract(self):
        report = ContractReport(
            violations=[Violation("f", "billing_code", Rule.EMPTY_STRING, Severity.WARNING, 3)]
        )

        assert report.ok
        assert report.warnings and not report.errors

    def test_one_error_fails_the_contract(self):
        report = ContractReport(
            violations=[Violation("f", "billing_class", Rule.VALUE_NOT_ALLOWED, Severity.ERROR)]
        )

        assert not report.ok

    def test_the_report_records_which_contract_checked_it(self, tmp_path, base):
        write(base, tmp_path)

        report = validate_root(tmp_path)

        assert report.version == CONTRACT_VERSION
        assert report.files == 1
        assert report.rows == 20
        assert report.ok

    def test_a_missing_root_is_an_error_not_an_empty_pass(self, tmp_path):
        """An empty report would read as "everything is fine"."""
        with pytest.raises(FileNotFoundError):
            validate_root(tmp_path / "nope")
