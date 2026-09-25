"""A1's eval harness, including against the real label file and the real queue.

The label file ships empty on purpose. The tests that matter most here are the
ones that make an empty file say "not measured" rather than a number, and that
make a malformed label stop the run rather than be scored as a disagreement.
"""

from __future__ import annotations

import csv
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from agents.triage_agent import (
    KEY_FIELDS,
    Cause,
    Outcome,
    RuleTriager,
    item_key,
    repair_mojibake,
)
from agents.triage_evals import (
    LABEL_COLUMNS,
    LABELS,
    LabelError,
    append_result,
    gate,
    load_labels,
    load_queue,
    main,
    score,
    write_worksheet,
)

REPO = Path(__file__).resolve().parents[2]
QUEUE = REPO / "summary" / "triage_queue.csv"


def row(code: str, rule: str) -> dict[str, Any]:
    base = {name: f"{name}-value" for name in KEY_FIELDS}
    base.update({"code": code, "triage_rule": rule, "ratio": "2.0"})
    return base


def write_labels(path: Path, labels: list[tuple[dict[str, Any], str]]) -> Path:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=LABEL_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for finding, cause in labels:
            writer.writerow({**finding, "expected_cause": cause, "labelled_by": "reviewer"})
    return path


class TestTheShippedFiles:
    def test_the_label_file_starts_with_the_label_columns(self):
        """A filled worksheet: the label columns, then context the scorer ignores."""
        path = REPO / LABELS
        with path.open(encoding="utf-8-sig") as handle:
            header = handle.readline().strip().split(",")

        assert header[: len(LABEL_COLUMNS)] == list(LABEL_COLUMNS)

    def test_every_label_matches_a_finding_in_the_real_queue(self):
        """The 25 Montefiore labels once went unmatched: the queue carries the
        hospital's mis-encoded name and the label file the repaired one."""
        labels = load_labels(REPO / LABELS)
        keys = {item_key(r) for r in load_queue(QUEUE)}

        assert labels, "the labels are in"
        assert [label.key for label in labels if label.key not in keys] == []

    def test_every_finding_in_the_real_queue_has_a_distinct_key(self):
        """A label identifies its finding by these fields; two sharing one would
        make a label ambiguous."""
        queue = load_queue(QUEUE)
        keys = [item_key(r) for r in queue]

        assert len(keys) == len(set(keys)) == len(queue)

    def test_a_worksheet_from_the_real_queue_loads_as_zero_labels(self, tmp_path: Path):
        queue = load_queue(QUEUE)
        path = tmp_path / "worksheet.csv"

        written = write_worksheet(queue, path)

        assert written == len(queue)
        assert load_labels(path) == [], "the answer column starts blank"

    def test_a_filled_worksheet_row_scores_against_the_real_queue(self, tmp_path: Path):
        """The round trip a labeller will actually do: fill a row, score it."""
        queue = load_queue(QUEUE)
        path = tmp_path / "worksheet.csv"
        write_worksheet(queue, path)
        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        rows[0]["expected_cause"] = Cause.CONTRACT_OFFSET
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

        result = score(RuleTriager(), queue, load_labels(path))

        assert result.status == "scored"
        assert result.scored == 1
        assert result.unmatched_labels == 0


class TestNoLabelsIsNotAScore:
    def test_it_is_not_measured(self, tmp_path: Path):
        result = score(RuleTriager(), [row("A", "unexplained")], [])

        assert result.status == "no_labels"
        assert result.precision is None
        assert "not measured" in result.summary()

    def test_the_gate_holds(self):
        result = score(RuleTriager(), [], [])

        passed, why = gate(result)

        assert not passed
        assert "not measured" in why

    def test_the_cli_says_so(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
        queue = tmp_path / "queue.csv"
        write_worksheet([row("A", "unexplained")], queue)
        labels = write_labels(tmp_path / "labels.csv", [])

        main(["--queue", str(queue), "--labels", str(labels)])

        out = capsys.readouterr().out
        assert "not measured" in out
        assert "gate: hold" in out

    def test_a_missing_label_file_is_no_labels(self, tmp_path: Path):
        assert load_labels(tmp_path / "absent.csv") == []


class TestScoring:
    def test_right_wrong_and_abstained_are_separate(self, tmp_path: Path):
        queue = [
            row("A", "vintage_artifact"),
            row("B", "implausible"),
            row("C", "unexplained"),
        ]
        labels = load_labels(
            write_labels(
                tmp_path / "l.csv",
                [
                    (queue[0], Cause.VINTAGE_ARTIFACT),
                    (queue[1], Cause.DATA_ERROR),
                    (queue[2], Cause.GENUINE_DISAGREEMENT),
                ],
            )
        )

        result = score(RuleTriager(), queue, labels)

        assert (result.correct, result.wrong, result.abstained) == (1, 1, 1)
        assert result.precision == pytest.approx(0.5)
        assert result.coverage == pytest.approx(2 / 3)
        assert result.confusion[Cause.DATA_ERROR] == {Cause.UNITS_OR_METHODOLOGY: 1}

    def test_abstaining_costs_coverage_not_precision(self, tmp_path: Path):
        queue = [row("A", "vintage_artifact"), row("B", "unexplained")]
        labels = load_labels(
            write_labels(
                tmp_path / "l.csv",
                [(queue[0], Cause.VINTAGE_ARTIFACT), (queue[1], Cause.DATA_ERROR)],
            )
        )

        result = score(RuleTriager(), queue, labels)

        assert result.precision == 1.0
        assert result.coverage == 0.5

    def test_only_labelled_findings_are_sent_to_the_triager(self, tmp_path: Path):
        """An LLM is billed per finding; scoring must not triage the whole queue."""
        seen: list[int] = []

        class Counting(RuleTriager):
            def triage(self, rows: Sequence[dict[str, Any]]) -> list[Outcome]:
                seen.append(len(rows))
                return super().triage(rows)

        queue = [row(c, "unexplained") for c in "ABCDE"]
        labels = load_labels(write_labels(tmp_path / "l.csv", [(queue[0], Cause.DATA_ERROR)]))

        score(Counting(), queue, labels)

        assert seen == [1]

    def test_a_label_whose_finding_left_the_queue_is_unmatched(self, tmp_path: Path):
        labels = load_labels(
            write_labels(tmp_path / "l.csv", [(row("gone", "unexplained"), Cause.DATA_ERROR)])
        )

        result = score(RuleTriager(), [row("A", "unexplained")], labels)

        assert result.status == "no_labels"
        assert result.unmatched_labels == 1

    def test_low_precision_holds_the_gate(self, tmp_path: Path):
        queue = [row("A", "implausible")]
        labels = load_labels(write_labels(tmp_path / "l.csv", [(queue[0], Cause.DATA_ERROR)]))

        passed, why = gate(score(RuleTriager(), queue, labels))

        assert not passed
        assert "precision 0.000" in why


class TestMalformedLabelsStopTheRun:
    def test_a_cause_outside_the_vocabulary_names_the_line(self, tmp_path: Path):
        path = write_labels(tmp_path / "l.csv", [(row("A", "x"), "units mismatch")])

        with pytest.raises(LabelError, match=r"l\.csv:2"):
            load_labels(path)

    def test_the_same_finding_labelled_twice(self, tmp_path: Path):
        finding = row("A", "x")
        path = write_labels(
            tmp_path / "l.csv", [(finding, Cause.DATA_ERROR), (finding, Cause.PLAN_MISMATCH)]
        )

        with pytest.raises(LabelError, match="twice"):
            load_labels(path)

    def test_a_missing_column(self, tmp_path: Path):
        path = tmp_path / "l.csv"
        path.write_text("code,expected_cause\nA,data_error\n", encoding="utf-8")

        with pytest.raises(LabelError, match="missing columns"):
            load_labels(path)


class TestHistory:
    def test_results_are_appended(self, tmp_path: Path):
        queue = [row("A", "vintage_artifact")]
        labels = load_labels(write_labels(tmp_path / "l.csv", [(queue[0], Cause.VINTAGE_ARTIFACT)]))
        path = tmp_path / "results.jsonl"

        for _ in range(2):
            append_result(score(RuleTriager(), queue, labels), path)

        assert len(path.read_text(encoding="utf-8").splitlines()) == 2


class TestMisEncodedText:
    """Montefiore publishes its en dash and apostrophe as UTF-8 read as cp1252."""

    PUBLISHED = "Center â€“ Childrenâ€™s"
    REPAIRED = "Center – Children’s"  # noqa: RUF001

    def test_the_published_form_and_the_repaired_form_are_one_finding(self):
        published = {**row("72285", "unexplained"), "facility": self.PUBLISHED}
        repaired = {**published, "facility": self.REPAIRED}

        assert item_key(published) == item_key(repaired)

    def test_the_repair_is_the_published_bytes_decoded_properly(self):
        assert repair_mojibake(self.PUBLISHED) == self.REPAIRED

    def test_a_right_double_quote_whose_byte_cp1252_leaves_undefined(self):
        assert repair_mojibake("â€\u009d") == "”"

    @pytest.mark.parametrize("text", ["Café", "â la carte", "plain", "Children's"])
    def test_real_text_is_left_alone(self, text):
        assert repair_mojibake(text) == text
