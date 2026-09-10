"""A4: judge whether a change at the payer boundary matters.

The manifest says *what* moved between two snapshots (#21, #23). This says
whether anyone should care. Those are different questions, and conflating them
is how a monitor becomes noise: a daily job that reports "eleven files changed"
without saying which of them invalidate a published figure gets read for a week
and ignored thereafter.

**The agent half is deliberately not built.** CLAUDE.md sets the build order --
deterministic implementation first, agent second, once the tables show where the
long tail is -- and the long tail here is empty. Every diff observed so far
reports no change at all. Writing an LLM classifier now would mean inventing a
distribution of hard cases rather than measuring one, and then evaluating it
against labels invented from the same imagination.

What is built instead is the shape the agent slots into, mirroring A2:
a deterministic assessor, a structured artifact per change rather than prose,
and an explicit review queue as the terminal path. When
:data:`Materiality.UNKNOWN` starts accumulating real entries, those entries are
the eval set, and an ``LlmAssessor`` implementing :class:`Assessor` is the next
step. Until then the queue is the honest answer.

The classification rests on one assumption worth stating: ``last_updated_on`` is
the payer's own reporting date, so a file whose vintage and row count are both
unchanged is the same source data written again, and a size difference is the
writer rather than the rates. That assumption is what makes a re-write cosmetic,
and it is why the reason string says so -- a snapshot pair taken with ``--hash``
settles it outright.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from payer.manifest import SNAPSHOT_GLOB, Manifest, ManifestDiff, diff


class Materiality(StrEnum):
    """Whether a change can have altered a number already reported."""

    #: A figure built before this change may now be wrong.
    MATERIAL = "material"
    #: The bytes moved; the data did not.
    COSMETIC = "cosmetic"
    #: The rules do not cover this combination. Goes to the review queue, and is
    #: the raw material for the agent that does not exist yet.
    UNKNOWN = "unknown"


class ChangeKind(StrEnum):
    FILE_ADDED = "file_added"
    FILE_REMOVED = "file_removed"
    STATE_CHANGED = "state_changed"
    SCHEMA_CHANGED = "schema_changed"
    VINTAGE_CHANGED = "vintage_changed"
    ROWS_CHANGED = "rows_changed"
    CONTENT_CHANGED = "content_changed"
    REWRITTEN = "rewritten"
    UNRECOGNISED = "unrecognised"


@dataclass(frozen=True)
class Assessment:
    """One judged change: a testable artifact, never prose.

    Guardrail 3 in CLAUDE.md -- every agent action produces something a test can
    check. This is that thing for A4, whether the assessor is rules or a model.
    """

    stem: str
    kind: ChangeKind
    materiality: Materiality
    reason: str
    fields: tuple[str, ...] = ()

    @property
    def needs_review(self) -> bool:
        return self.materiality is Materiality.UNKNOWN

    def describe(self) -> str:
        return f"[{self.materiality}] {self.stem}: {self.kind} -- {self.reason}"


class Assessor(Protocol):
    """What a rule set and a model would both have to implement."""

    def assess(self, diff: ManifestDiff) -> list[Assessment]: ...


def _field_names(entries: Iterable[str]) -> tuple[str, ...]:
    """``"rows: 20 -> 5"`` and friends reduced to just the field names."""
    return tuple(entry.split(":", 1)[0].strip() for entry in entries)


class RuleBasedAssessor:
    """The deterministic baseline, and currently the whole of A4.

    Every rule here is a claim about consequence, not about tidiness:

    * A **new or vanished file** changes what the dataset contains, so any figure
      computed across the boundary is now built on a different denominator. A
      vanished file is the sharper case -- a payer file that stops matching any
      target hospital simply stops being written, and without the previous
      snapshot its absence is invisible.
    * A **schema change** is contract business. It may quarantine the file at the
      next load (#22), so it is material regardless of the rows behind it.
    * A **vintage change** is material by the standing rule that vintage mismatch
      is structural, not incidental: hospital files update annually and payer
      files monthly, so a moved vintage is the single likeliest reason two
      disclosures disagree.
    * A **row count change** at a stable vintage means the same reporting month
      now yields a different number of rates. That is either an upstream fix or
      an upstream regression, and neither is cosmetic.
    * A **rewrite** -- size or chunking moved while rows, vintage, schema and
      state all held -- is the one cosmetic verdict, and it rests on the vintage
      assumption in the module docstring.
    """

    name = "rules"

    def assess(self, diff: ManifestDiff) -> list[Assessment]:
        out: list[Assessment] = [
            Assessment(
                stem=stem,
                kind=ChangeKind.FILE_ADDED,
                materiality=Materiality.MATERIAL,
                reason="new file at the boundary; figures computed before it excluded it",
            )
            for stem in diff.added
        ]
        out += [
            Assessment(
                stem=stem,
                kind=ChangeKind.FILE_REMOVED,
                materiality=Materiality.MATERIAL,
                reason="file no longer present; anything computed from it is unreproducible",
            )
            for stem in diff.removed
        ]
        for stem, entries in sorted(diff.changed.items()):
            out.append(self._judge(stem, entries))
        return out

    def _judge(self, stem: str, entries: Sequence[str]) -> Assessment:
        fields = _field_names(entries)
        detail = "; ".join(entries)
        moved = set(fields)

        if "state" in moved:
            return Assessment(
                stem,
                ChangeKind.STATE_CHANGED,
                Materiality.MATERIAL,
                f"the file's role in the dataset changed ({detail})",
                fields,
            )
        if "columns" in moved:
            return Assessment(
                stem,
                ChangeKind.SCHEMA_CHANGED,
                Materiality.MATERIAL,
                f"schema drift; the load gate may quarantine this file ({detail})",
                fields,
            )
        if "vintage" in moved:
            return Assessment(
                stem,
                ChangeKind.VINTAGE_CHANGED,
                Materiality.MATERIAL,
                f"new reporting month; vintage mismatch is structural, not incidental ({detail})",
                fields,
            )
        if "rows" in moved:
            return Assessment(
                stem,
                ChangeKind.ROWS_CHANGED,
                Materiality.MATERIAL,
                f"same vintage, different row count -- an upstream change in the data ({detail})",
                fields,
            )
        if "sha256" in moved:
            return Assessment(
                stem,
                ChangeKind.CONTENT_CHANGED,
                Materiality.MATERIAL,
                f"bytes differ while every footer field held; content moved unseen ({detail})",
                fields,
            )
        if moved and moved <= {"bytes", "row_groups"}:
            return Assessment(
                stem,
                ChangeKind.REWRITTEN,
                Materiality.COSMETIC,
                (
                    "rewritten at the same vintage and row count, so the writer changed "
                    f"rather than the rates; snapshot with --hash to be certain ({detail})"
                ),
                fields,
            )
        # Reaching here means the diff reported fields these rules do not model.
        # Better a queue entry than a guess: this is where the agent goes.
        return Assessment(
            stem,
            ChangeKind.UNRECOGNISED,
            Materiality.UNKNOWN,
            f"no rule covers this combination of changes ({detail})",
            fields,
        )


def validate_assessment(assessment: Assessment, diff: ManifestDiff) -> tuple[bool, str]:
    """Check an assessment against the diff it claims to describe.

    Guardrail 1: whatever proposes, deterministic code validates. The rules
    cannot fail this today, which is the point -- it exists so that an
    ``LlmAssessor`` cannot invent a file, a field, or a verdict outside the
    enum, and it is already under test when that arrives.
    """
    known = set(diff.added) | set(diff.removed) | set(diff.changed)
    if assessment.stem not in known:
        return False, f"{assessment.stem} is not in this diff"
    if assessment.materiality not in set(Materiality):
        return False, f"unknown materiality {assessment.materiality!r}"
    if assessment.kind not in set(ChangeKind):
        return False, f"unknown kind {assessment.kind!r}"
    if not assessment.reason.strip():
        return False, "an assessment with no reason is not reviewable"
    if assessment.stem in diff.changed:
        reported = set(_field_names(diff.changed[assessment.stem]))
        invented = set(assessment.fields) - reported
        if invented:
            return False, f"fields not in the diff: {sorted(invented)}"
    return True, ""


@dataclass
class Watch:
    """The outcome of monitoring one diff."""

    assessments: list[Assessment]
    invalid: list[tuple[Assessment, str]]

    @property
    def material(self) -> list[Assessment]:
        return [a for a in self.assessments if a.materiality is Materiality.MATERIAL]

    @property
    def review_queue(self) -> list[Assessment]:
        """Terminal path per guardrail 4, and the eval set the agent will need."""
        return [a for a in self.assessments if a.needs_review]

    @property
    def quiet(self) -> bool:
        """Nothing a person needs to look at."""
        return not (self.material or self.review_queue or self.invalid)

    def summary(self) -> dict[str, object]:
        return {
            "changes": len(self.assessments),
            "material": len(self.material),
            "cosmetic": sum(1 for a in self.assessments if a.materiality is Materiality.COSMETIC),
            "needs_review": len(self.review_queue),
            "invalid": [f"{a.stem}: {why}" for a, why in self.invalid],
            "quiet": self.quiet,
            "detail": [a.describe() for a in self.assessments],
        }


def monitor(diff: ManifestDiff, assessor: Assessor | None = None) -> Watch:
    """Judge a manifest diff, validating every assessment before returning it.

    An assessment that fails validation is not silently dropped: it is reported
    in ``invalid`` and its file lands in nobody's "all clear", because a monitor
    that quietly discards what it cannot verify is worse than one that says so.
    """
    assessor = assessor or RuleBasedAssessor()
    assessments: list[Assessment] = []
    invalid: list[tuple[Assessment, str]] = []
    for assessment in assessor.assess(diff):
        ok, why = validate_assessment(assessment, diff)
        if ok:
            assessments.append(assessment)
        else:
            invalid.append((assessment, why))
    return Watch(assessments=assessments, invalid=invalid)


def main(argv: list[str] | None = None) -> int:
    """Judge the two newest snapshots in a directory. Run by the daily task.

    Exits non-zero only with ``--fail-on-material``, which is off by default:
    a scheduled job that fails on every new payer month would be muted within a
    fortnight, and a muted monitor is indistinguishable from none.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--snapshot-dir", type=Path, required=True)
    parser.add_argument(
        "--fail-on-material", action="store_true", help="exit non-zero if anything material moved"
    )
    args = parser.parse_args(argv)

    snapshots = sorted(args.snapshot_dir.glob(SNAPSHOT_GLOB))
    if len(snapshots) < 2:
        print(
            json.dumps(
                {"compared": False, "reason": f"{len(snapshots)} snapshot(s); need two"}, indent=1
            )
        )
        return 0

    before, after = Manifest.read_json(snapshots[-2]), Manifest.read_json(snapshots[-1])
    watch = monitor(diff(before, after))
    print(
        json.dumps(
            {"compared": True, "before": snapshots[-2].name, "after": snapshots[-1].name}
            | watch.summary(),
            indent=1,
        )
    )
    if args.fail_on_material and (watch.material or watch.review_queue):
        return 1
    return 0


__all__ = [
    "Assessment",
    "Assessor",
    "ChangeKind",
    "Materiality",
    "RuleBasedAssessor",
    "Watch",
    "monitor",
    "validate_assessment",
]


if __name__ == "__main__":  # pragma: no cover - entrypoint
    sys.exit(main())
