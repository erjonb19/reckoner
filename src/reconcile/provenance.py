"""What a number rests on.

No finance lead acts on a figure they cannot defend in the room, and the
questions are always the same: how many rows is that, from when, what did you
exclude, and does it cover enough of our book to matter. A number without those
answers is an opinion.

So every reported figure carries a Provenance, and figures built from sources of
different vintages carry the caveat automatically rather than relying on someone
remembering. docs/SPEC.md names vintage mismatch as structural -- hospital files
update annually, payer files monthly, SPARCS runs years behind -- and the only
honest handling is to surface the span, not to average across it silently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

#: Beyond this, two sources describe different worlds and a variance between
#: them is at least partly a timing artifact rather than a price difference.
COMPARABLE_VINTAGE_DAYS = 400


def parse_vintage(value: str | None) -> date | None:
    """Parse the date formats hospitals actually publish."""
    if not value:
        return None
    text = value.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d", "%d-%m-%Y", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


@dataclass(frozen=True)
class Source:
    """One input that a number depends on."""

    name: str
    vintage: str = ""
    rows: int = 0
    note: str = ""

    @property
    def vintage_date(self) -> date | None:
        return parse_vintage(self.vintage)


@dataclass
class Provenance:
    """The defensibility envelope around a reported figure."""

    rows: int = 0
    hospitals: int = 0
    sources: list[Source] = field(default_factory=list)
    #: Reason code -> rows removed. What was dropped is part of the finding.
    excluded: dict[str, int] = field(default_factory=dict)
    #: Share of the hospital's total book this figure covers, where known.
    coverage: float | None = None
    extra_caveats: list[str] = field(default_factory=list)

    @property
    def vintages(self) -> list[date]:
        return [d for d in (s.vintage_date for s in self.sources) if d is not None]

    @property
    def vintage_span_days(self) -> int:
        dates = self.vintages
        return (max(dates) - min(dates)).days if len(dates) > 1 else 0

    @property
    def excluded_rows(self) -> int:
        return sum(self.excluded.values())

    @property
    def included_share(self) -> float:
        total = self.rows + self.excluded_rows
        return self.rows / total if total else 0.0

    @property
    def is_comparable(self) -> bool:
        """False when the inputs are too far apart in time to compare directly."""
        return self.vintage_span_days <= COMPARABLE_VINTAGE_DAYS

    @property
    def caveats(self) -> list[str]:
        """Everything a reader must be told, assembled rather than remembered."""
        notes = list(self.extra_caveats)
        if not self.is_comparable:
            oldest = min(self.vintages).isoformat()
            newest = max(self.vintages).isoformat()
            notes.append(
                f"sources span {self.vintage_span_days} days ({oldest} to {newest}); "
                "differences may be a timing artifact rather than a price difference"
            )
        if self.rows == 0:
            notes.append("no rows behind this figure")
        elif self.rows < 30:
            notes.append(f"only {self.rows} rows; not a stable estimate")
        if self.excluded_rows and self.included_share < 0.5:
            notes.append(
                f"{self.included_share:.0%} of candidate rows survived filtering "
                f"({self.excluded_rows:,} excluded)"
            )
        if self.coverage is not None and self.coverage < 0.25:
            notes.append(f"covers only {self.coverage:.0%} of the hospital's book")
        if self.hospitals == 1:
            notes.append("single hospital; not a market view")
        return notes

    @property
    def is_reportable(self) -> bool:
        """Whether this figure can stand on its own without a caveat attached."""
        return self.rows >= 30 and self.is_comparable

    def exclude(self, reason: str, count: int = 1) -> None:
        self.excluded[reason] = self.excluded.get(reason, 0) + count

    def add_source(self, name: str, vintage: str = "", rows: int = 0, note: str = "") -> None:
        self.sources.append(Source(name, vintage, rows, note))

    def summary(self) -> str:
        parts = [f"{self.rows:,} rows", f"{self.hospitals} hospitals"]
        if self.sources:
            parts.append(
                "sources: " + ", ".join(f"{s.name}@{s.vintage or '?'}" for s in self.sources)
            )
        if self.excluded_rows:
            parts.append(f"{self.excluded_rows:,} excluded")
        return " | ".join(parts)


def merge(*provenances: Provenance) -> Provenance:
    """Combine the envelopes of several inputs into one."""
    merged = Provenance()
    for item in provenances:
        merged.rows += item.rows
        merged.hospitals = max(merged.hospitals, item.hospitals)
        merged.sources.extend(item.sources)
        for reason, count in item.excluded.items():
            merged.exclude(reason, count)
        merged.extra_caveats.extend(item.extra_caveats)
    coverages = [p.coverage for p in provenances if p.coverage is not None]
    merged.coverage = min(coverages) if coverages else None
    return merged
