"""The summary dataset as the published page reads it.

All of the page's logic lives here and none of it imports Streamlit, for two
reasons. It is testable in CI without a UI dependency, and the one thing that
would be embarrassing on a public page -- a filter that appears to work and does
not -- is a pure function that can be held down by a test.

**The page reads CSV from the repository and makes no network calls.** That is
the strongest available form of "no ADLS credentials in the app": there is
nothing to authenticate with because there is nothing to reach.

**Refusals carry carrier and code type, except where gold predates that.**
They were counted at system grain until #63. A system whose gold has not been
rebuilt since still has blank carriers, and a carrier filter drops those rows.
The page names the systems it dropped rather than showing a smaller total as
if it were the answer, because a filter that looks applied and is not is the
failure this project has catalogued more than ten times.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: The tables the page expects, in the order it shows them.
TABLES = ("coverage", "outcomes", "magnitude", "exemplars", "refusals")

ALL = "All"

#: Said out loud on the refusals view. Not a footnote: the controls are visibly
#: present and two of them do nothing there.
REFUSALS_GRAIN_NOTE = (
    "Refusals are counted at system grain. The comparability layer records why a "
    "candidate was refused without keeping its carrier or code type, so the "
    "carrier and code-type filters do not apply to this view."
)

#: Columns that are numbers, so the page can right-align and format them without
#: guessing from the values -- a column of digits that is really a code should
#: not be summed.
NUMERIC = {
    "hospital_rates",
    "payer_rates",
    "candidates",
    "pairs_formed",
    "material",
    "unexplained_and_material",
    "facilities",
    "carriers",
    "systematic_offsets",
    "pairs",
    "material_pairs",
    "residual_pairs",
    "implausible_pairs",
    "hospital_rate",
    "payer_rate",
    "difference",
    "ratio",
    "comparable_share",
    "median_relative_difference",
    "p90_relative_difference",
    "median_abs_difference_usd",
    "relative_difference",
}


@dataclass
class Dataset:
    """Every table plus the provenance record, as read from ``summary/``."""

    tables: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.tables.get("coverage")

    def table(self, name: str) -> list[dict[str, Any]]:
        return self.tables.get(name, [])


def _coerce(value: str, column: str) -> str | int | float:
    """Numbers as numbers, everything else as text.

    CSV has no types. Leaving a count as a string would sort 10 before 9 and
    chart it as a category, which looks like a data problem rather than a
    reading problem.
    """
    if column not in NUMERIC or value == "":
        return value
    try:
        number = float(value)
    except ValueError:
        return value
    return int(number) if number.is_integer() and abs(number) < 2**53 else number


def load(directory: Path) -> Dataset:
    """Read the dataset, reporting what is absent rather than failing.

    A missing table is recorded and the page renders without that view. The
    alternative -- refusing to start -- turns one absent file into a blank page
    with no way to tell which file it was.
    """
    dataset = Dataset()
    for name in TABLES:
        path = directory / f"{name}.csv"
        if not path.exists() or not path.stat().st_size:
            dataset.missing.append(name)
            dataset.tables[name] = []
            continue
        with path.open(encoding="utf-8", newline="") as handle:
            dataset.tables[name] = [
                {column: _coerce(value, column) for column, value in row.items()}
                for row in csv.DictReader(handle)
            ]
    run = directory / "run.json"
    if run.exists():
        try:
            dataset.metadata = json.loads(run.read_text(encoding="utf-8"))
        except ValueError:
            dataset.missing.append("run.json")
    else:
        dataset.missing.append("run.json")
    return dataset


def options(dataset: Dataset) -> dict[str, list[str]]:
    """Filter choices, taken from the data rather than hardcoded.

    Drawn from ``outcomes``, which is the only table carrying all three
    dimensions. A hardcoded list would drift the moment a carrier is added and
    the page would quietly stop offering it.
    """
    rows = dataset.table("outcomes")
    return {
        "system": [ALL, *sorted({str(r["system"]) for r in rows if r.get("system")})],
        "carrier": [ALL, *sorted({str(r["carrier"]) for r in rows if r.get("carrier")})],
        "code_type": [ALL, *sorted({str(r["code_type"]) for r in rows if r.get("code_type")})],
    }


def apply_filters(
    rows: list[dict[str, Any]],
    *,
    system: str = ALL,
    carrier: str = ALL,
    code_type: str = ALL,
) -> list[dict[str, Any]]:
    """Keep rows matching every selected filter that the rows can answer.

    A filter is skipped when the column is absent, which is how one function
    serves tables at three different grains. The page is responsible for saying
    where that happens -- see :data:`REFUSALS_GRAIN_NOTE` -- because silently
    skipping is exactly what makes a filter look applied when it is not.
    """
    chosen = {"system": system, "carrier": carrier, "code_type": code_type}
    out = rows
    for column, value in chosen.items():
        if value == ALL:
            continue
        out = [row for row in out if column not in row or str(row[column]) == value]
    return out


def inapplicable_filters(rows: list[dict[str, Any]], **chosen: str) -> list[str]:
    """Which of the selected filters this table cannot answer.

    Returned so the page can name them. ``carrier`` selected against a table
    with no carrier column is not an error and not a no-op to hide; it is
    something the reader has to be told.
    """
    if not rows:
        return []
    columns = set(rows[0])
    return sorted(name for name, value in chosen.items() if value != ALL and name not in columns)


def unattributed_excluded(rows: list[dict[str, Any]], **chosen: str) -> dict[str, int]:
    """Candidates a filter dropped because the filtered column is blank, by system.

    A blank carrier is not "no carrier"; it is a refusal recorded before carrier
    grain existed. Filtering on Aetna rightly excludes it, and the reader has to
    be told that the total they are looking at excludes it too.
    """
    active = [name for name, value in chosen.items() if value != ALL and name != "system"]
    if not active:
        return {}
    dropped: dict[str, int] = {}
    for row in rows:
        if chosen.get("system", ALL) not in (ALL, str(row.get("system"))):
            continue
        if any(name in row and not str(row[name] or "").strip() for name in active):
            key = str(row.get("system"))
            dropped[key] = dropped.get(key, 0) + int(float(row.get("candidates") or 0))
    return dict(sorted(dropped.items()))


def funnel(dataset: Dataset, *, system: str = ALL) -> list[dict[str, Any]]:
    """The coverage view: rows read, pairs formed, what survived."""
    return apply_filters(dataset.table("coverage"), system=system)


def outcomes_chart(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pairs by explanation, which is the one chart the outcomes view needs."""
    totals: dict[str, int] = {}
    for row in rows:
        key = str(row.get("explanation", "?"))
        totals[key] = totals.get(key, 0) + int(row.get("pairs") or 0)
    return [
        {"explanation": key, "pairs": value}
        for key, value in sorted(totals.items(), key=lambda kv: -kv[1])
    ]


def widest(rows: list[dict[str, Any]], limit: int = 25) -> list[dict[str, Any]]:
    """The largest disagreements first, which is what anyone scrolls for."""
    return sorted(rows, key=lambda r: -float(r.get("relative_difference") or 0))[:limit]


def staleness(metadata: dict[str, Any]) -> dict[str, str]:
    """The vintages the page shows above the fold.

    The committed dataset is a snapshot of an artifact in ADLS, and a stale
    snapshot looks exactly like a fresh one. Rendering these is the only thing
    standing between a reader and a months-old number they believe.
    """
    return {
        "Built": str(metadata.get("built_at") or "unknown"),
        "Bronze ingest": str(metadata.get("bronze_ingest_date") or "unknown"),
        "Hospital silver": str(metadata.get("silver_hospital_published_at") or "unknown"),
        "Payer silver": str(metadata.get("silver_payer_published_at") or "unknown"),
        "Gold": str(metadata.get("gold_published_at") or "unknown"),
    }


def caveats(metadata: dict[str, Any]) -> list[str]:
    return [str(c) for c in metadata.get("caveats", [])]


__all__ = [
    "ALL",
    "NUMERIC",
    "REFUSALS_GRAIN_NOTE",
    "TABLES",
    "Dataset",
    "apply_filters",
    "caveats",
    "funnel",
    "inapplicable_filters",
    "load",
    "options",
    "outcomes_chart",
    "staleness",
    "unattributed_excluded",
    "widest",
]
