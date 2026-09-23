"""The analyst page's logic: what it shows, computed without a UI.

One question drives it: *where does my hospital's published rate disagree most
with what the payer published, and why?* Every function here answers part of
that from the published dataset, and imports no Streamlit, so what the page
claims is covered by tests rather than by looking at it.

Two kinds of data. The ``summary/`` CSVs are committed and small. Code lookup's
rows run to millions, so they are a monthly GitHub Release whose checksums
``run.json`` records. :func:`fetch_release_file` downloads them once, verifies
them, caches them, and says plainly when it cannot.
"""

from __future__ import annotations

import csv
import hashlib
import io
import os
import shutil
import tempfile
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, fields
from pathlib import Path
from statistics import median
from typing import Any, BinaryIO

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

ALL = "All"
REPOSITORY = "erjonb19/reckoner"
RELEASE_URL = "https://github.com/{repository}/releases/download/{tag}/{name}"

#: Tables the analyst page reads from ``summary/``.
TABLES = (
    "coverage",
    "pairs",
    "outcomes",
    "residual",
    "refusals",
    "vintage_alignment",
)

NUMERIC = frozenset(
    {
        "hospital_rates",
        "payer_rates",
        "candidates",
        "pairs_formed",
        "comparable_share",
        "like_class_candidates",
        "like_class_share",
        "material",
        "unexplained_and_material",
        "facilities",
        "carriers",
        "systematic_offsets",
        "compared",
        "unexplained_material",
        "median_signed_gap",
        "summed_abs_gap_usd",
        "inside_range_share",
        "pairs",
        "material_pairs",
        "hospital_rate",
        "payer_rate",
        "payer_min",
        "payer_max",
        "payer_count",
        "difference",
        "ratio",
        "relative_difference",
        "median_gap_days",
        "min_gap_days",
        "p90_gap_days",
        "max_gap_days",
        "unknown_vintage_pairs",
        "beyond_limit",
    }
)

#: Every refusal reason the comparability layer can emit, in plain language.
#: ``test_every_reason_the_pipeline_emits_has_words`` holds this against the
#: code, so a new reason cannot reach the page unlabelled.
REASONS: dict[str, tuple[str, str]] = {
    "tic_exempt_product": (
        "Exempt from the insurer rule",
        "Medicare Advantage and Medicaid rates exist only in hospital files: "
        "Transparency in Coverage does not require insurers to publish them.",
    ),
    "no payer-side counterpart": (
        "No insurer rate",
        "The insurer publishes no rate for this code at this facility, or the "
        "hospital names an insurer whose files are not in this dataset.",
    ),
    "different_billing_class": (
        "Other billing class only",
        "The insurer's rates for this code are the other billing class: a "
        "facility charge cannot be compared with a professional fee.",
    ),
    "billing_class_unstated": (
        "Billing class not stated",
        "The hospital did not say whether the rate is a facility or professional "
        "charge, and its file publishes both kinds, so it cannot be assumed.",
    ),
    "mixed_rate_kind": (
        "Percentage against dollars",
        "One side states a percentage of charges and the other a dollar amount.",
    ),
    "not_dollar_denominated": (
        "Not in dollars",
        "Both sides state a percentage or formula rather than an amount.",
    ),
    "incompatible_methodology": (
        "Different payment method",
        "A per diem, a case rate and a fee schedule are not the same kind of "
        "price, so their dollar amounts are not comparable.",
    ),
    "zero_rate": (
        "Rate of $0",
        "One side publishes $0, a placeholder rather than a price.",
    ),
    "missing_rate": ("No amount", "One side publishes no amount at all."),
    "vintage_too_far_apart": (
        "Files too far apart in time",
        "The two files are more than 400 days apart, too far for the rates to "
        "describe the same contract terms.",
    ),
    "different_setting": ("Different setting", "Inpatient against outpatient."),
    "different_code": ("Different code", "The two rates are for different services."),
    "different_code_type": (
        "Different code system",
        "The same digits in two code systems are different services.",
    ),
}

#: Every explanation the variance mart assigns, in plain language.
EXPLANATIONS: dict[str, tuple[str, str]] = {
    "unexplained": (
        "Unexplained",
        "Differs from the insurer's median by 5% or more, and no rule accounts for "
        "it. This is the finding.",
    ),
    "within_payer_range": (
        "Inside the insurer's range",
        "The hospital's rate is one of the prices the insurer itself publishes "
        "for this code: inside its lowest and highest.",
    ),
    "plan_unresolved": (
        "Plan not matched",
        "The hospital's plan name matches none of the insurer's networks, so the "
        "two may not be the same contract.",
    ),
    "vintage_artifact": (
        "Timing",
        "The files are months apart and the difference is one a contract could "
        "move by in that time.",
    ),
    "granularity_mismatch": (
        "Plan grouping",
        "One side publishes one rate for several plans, the other plan by plan.",
    ),
    "systematic_offset": (
        "Contract-wide offset",
        "The same ratio across many codes in this contract: one fact about two "
        "base rates, not a finding per code.",
    ),
    "entity_resolution_suspect": (
        "Ten times apart",
        "So far apart the two rates are probably not the same thing: a unit or "
        "method mismatch, or a mismatched contract.",
    ),
}


def reason_label(reason: str) -> str:
    return REASONS.get(reason, (reason.replace("_", " ").capitalize(), ""))[0]


def explanation_label(explanation: str) -> str:
    return EXPLANATIONS.get(explanation, (explanation.replace("_", " ").capitalize(), ""))[0]


# --- filters -----------------------------------------------------------------


@dataclass(frozen=True)
class Filters:
    """What the analyst has narrowed to. Persisted in the URL, so a view can be sent."""

    system: str = ALL
    facility: str = ALL
    carrier: str = ALL
    code_type: str = ALL

    @classmethod
    def from_query(cls, query: dict[str, Any]) -> Filters:
        names = {f.name for f in fields(cls)}
        return cls(**{k: str(v) for k, v in query.items() if k in names and v})

    def to_query(self) -> dict[str, str]:
        return {f.name: getattr(self, f.name) for f in fields(self) if getattr(self, f.name) != ALL}

    def admits(self, row: dict[str, Any]) -> bool:
        """True when every chosen filter matches, or the row has no such column."""
        for name in ("system", "facility", "carrier", "code_type"):
            value = getattr(self, name)
            if value != ALL and name in row and str(row.get(name) or "") != value:
                return False
        return True


def apply(rows: Iterable[dict[str, Any]], filters: Filters) -> list[dict[str, Any]]:
    return [row for row in rows if filters.admits(row)]


# --- loading -------------------------------------------------------------------


@dataclass
class Dataset:
    tables: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)

    def table(self, name: str) -> list[dict[str, Any]]:
        return self.tables.get(name, [])


def _coerce(value: str, column: str) -> Any:  # noqa: ANN401 - CSV cells are untyped
    if value == "":
        return None
    if column in NUMERIC:
        try:
            number = float(value)
        except ValueError:
            return value
        return int(number) if number.is_integer() and "share" not in column else number
    if value in ("True", "False"):
        return value == "True"
    return value


def load(directory: Path) -> Dataset:
    import json

    data = Dataset()
    for name in TABLES:
        path = directory / f"{name}.csv"
        if not path.exists() or not path.stat().st_size:
            data.missing.append(name)
            continue
        with path.open(newline="", encoding="utf-8") as handle:
            data.tables[name] = [
                {k: _coerce(v, k) for k, v in row.items()} for row in csv.DictReader(handle)
            ]
    run = directory / "run.json"
    if run.exists():
        data.metadata = json.loads(run.read_text(encoding="utf-8"))
    return data


def options(data: Dataset, filters: Filters) -> dict[str, list[str]]:
    """Choices for each filter, narrowed by the ones already chosen above it."""
    pairs = data.table("pairs")
    systems = sorted({str(r["system"]) for r in data.table("coverage")})
    in_system = [r for r in pairs if filters.system in (ALL, r["system"])]
    facilities = sorted({str(r["facility"]) for r in in_system})
    carriers = sorted({str(r["carrier"]) for r in in_system})
    code_types = sorted({str(r["code_type"]) for r in data.table("outcomes") if r.get("code_type")})
    return {
        "system": [ALL, *systems],
        "facility": [ALL, *facilities],
        "carrier": [ALL, *carriers],
        "code_type": [ALL, *code_types],
    }


# --- rankings ------------------------------------------------------------------

RANKINGS = {
    "unexplained": ("unexplained_material", "Unexplained rates"),
    "gap": ("summed_abs_gap_usd", "Summed price gap"),
    "median": ("median_signed_gap", "Median gap"),
}


def rankings(data: Dataset, filters: Filters, by: str = "unexplained") -> list[dict[str, Any]]:
    """Facility x carrier pairs, most disagreement first."""
    column = RANKINGS[by][0]

    def key(row: dict[str, Any]) -> tuple[float, str, str]:
        value = row.get(column)
        size = abs(value) if isinstance(value, (int, float)) else -1.0
        return (-size, str(row["facility"]), str(row["carrier"]))

    rows = sorted(apply(data.table("pairs"), filters), key=key)
    return [{"rank": i + 1, **row} for i, row in enumerate(rows)]


def headline(data: Dataset, filters: Filters) -> dict[str, int]:
    coverage = apply(data.table("coverage"), filters)
    return {
        "systems": len(coverage),
        "hospital_rates": sum(int(r.get("candidates") or 0) for r in coverage),
        "compared": sum(int(r.get("pairs_formed") or 0) for r in coverage),
        "unexplained": sum(int(r.get("unexplained_and_material") or 0) for r in coverage),
    }


# --- pair detail ------------------------------------------------------------------


@dataclass
class PairDetail:
    facility: str
    carrier: str
    summary: dict[str, Any]
    explanations: list[dict[str, Any]]
    residual: list[dict[str, Any]]
    refusals: list[dict[str, Any]]


def pair_detail(data: Dataset, facility: str, carrier: str) -> PairDetail | None:
    pair = next(
        (r for r in data.table("pairs") if r["facility"] == facility and r["carrier"] == carrier),
        None,
    )
    if pair is None:
        return None
    counts: dict[str, int] = {}
    for row in data.table("outcomes"):
        if row.get("facility") == facility and row.get("carrier") == carrier:
            counts[str(row["explanation"])] = counts.get(str(row["explanation"]), 0) + int(
                row["pairs"]
            )
    total = sum(counts.values())
    explanations = [
        {
            "explanation": name,
            "label": explanation_label(name),
            "rates": n,
            "share": n / total if total else 0.0,
        }
        for name, n in sorted(counts.items(), key=lambda kv: -kv[1])
    ]
    refused: dict[str, int] = {}
    for row in data.table("refusals"):
        if row.get("facility") == facility and row.get("carrier") == carrier:
            refused[str(row["reason"])] = refused.get(str(row["reason"]), 0) + int(
                row["candidates"]
            )
    refusals = [
        {"reason": name, "label": reason_label(name), "rates": n}
        for name, n in sorted(refused.items(), key=lambda kv: -kv[1])
    ]
    residual = sorted(
        (
            r
            for r in data.table("residual")
            if r.get("facility") == facility and r.get("carrier") == carrier
        ),
        key=lambda r: -abs(float(r.get("difference") or 0.0)),
    )
    return PairDetail(facility, carrier, pair, explanations, residual, refusals)


def signed_gap(hospital: float | None, payer: float | None) -> float | None:
    """How far the insurer's median sits from the hospital's rate: + means higher."""
    if not hospital or payer is None:
        return None
    return payer / hospital - 1.0


# --- the release: code lookup's rows ----------------------------------------------


@dataclass(frozen=True)
class ReleaseFile:
    """A verified local copy, or the reason there is none. Never a silent maybe."""

    path: Path | None
    reason: str = ""

    @property
    def available(self) -> bool:
        return self.path is not None


Opener = Callable[[str], BinaryIO]


def _open(url: str) -> BinaryIO:
    return urllib.request.urlopen(url, timeout=60)  # type: ignore[no-any-return]


def fetch_release_file(
    metadata: dict[str, Any],
    name: str,
    cache: Path | None = None,
    opener: Opener = _open,
) -> ReleaseFile:
    """Download one release file once, verify it against run.json, and cache it.

    A file whose checksum does not match is refused, not shown: code lookup
    showing the wrong month's rates beside this month's summary would be worse
    than showing none.
    """
    release = metadata.get("release") or {}
    tag = release.get("tag")
    expected = (release.get("files") or {}).get(name)
    if not tag or not expected:
        return ReleaseFile(None, "this dataset names no release for code lookup")
    cache = cache or Path(tempfile.gettempdir()) / "reckoner-release"
    target = cache / tag / name
    if target.exists() and _sha256(target) == expected["sha256"]:
        return ReleaseFile(target)
    url = RELEASE_URL.format(repository=REPOSITORY, tag=tag, name=name)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    try:
        with opener(url) as response, partial.open("wb") as out:
            shutil.copyfileobj(response, out)
    except Exception as exc:  # the network is the one thing this page cannot control
        partial.unlink(missing_ok=True)
        return ReleaseFile(None, f"could not download {name}: {type(exc).__name__}")
    digest = _sha256(partial)
    if digest != expected["sha256"]:
        partial.unlink(missing_ok=True)
        return ReleaseFile(None, f"{name} did not match its recorded checksum")
    os.replace(partial, target)
    return ReleaseFile(target)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_rates(path: Path) -> pa.Table:
    return pq.read_table(path)


def normalise_code(text: str) -> str:
    return text.strip().upper()


def code_lookup(rates: pa.Table, code: str, filters: Filters) -> list[dict[str, Any]]:
    """Every facility and carrier's rate for one code, compared or not."""
    wanted = normalise_code(code)
    if not wanted:
        return []
    mask = pc.equal(rates.column("code"), pa.scalar(wanted))
    rows = rates.filter(mask).to_pylist()
    out = []
    for row in apply(rows, filters):
        gap = signed_gap(row.get("hospital_rate"), row.get("payer_median"))
        why = (
            explanation_label(str(row["explanation_before_offsets"]))
            if row.get("compared")
            else reason_label(str(row.get("refusal") or ""))
        )
        out.append({**row, "gap": gap, "why": why})
    return sorted(
        out,
        key=lambda r: (r["gap"] is None, -abs(r["gap"] or 0.0), r["facility"], r["carrier"]),
    )


def lookup_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    hospital = [r["hospital_rate"] for r in rows if r.get("hospital_rate") is not None]
    payer = [r["payer_median"] for r in rows if r.get("payer_median") is not None]
    return {
        "facilities": len({r["facility"] for r in rows}),
        "carriers": len({r["carrier"] for r in rows}),
        "compared": sum(1 for r in rows if r.get("compared")),
        "hospital_range": (min(hospital), max(hospital)) if hospital else None,
        "payer_range": (min(payer), max(payer)) if payer else None,
        "hospital_median": median(hospital) if hospital else None,
    }


def describe(codes: pa.Table | None, code: str) -> str:
    if codes is None:
        return ""
    wanted = normalise_code(code)
    match = codes.filter(pc.equal(codes.column("code"), pa.scalar(wanted)))
    return str(match.column("description")[0].as_py()) if match.num_rows else ""


def suggest(codes: pa.Table | None, text: str, limit: int = 12) -> list[tuple[str, str, str]]:
    """Codes whose number starts with, or whose description contains, the text."""
    if codes is None or len(text.strip()) < 2:
        return []
    query = text.strip()
    by_code = pc.starts_with(codes.column("code"), query.upper())
    by_text = pc.match_substring(pc.utf8_lower(codes.column("description")), query.lower())
    hits = codes.filter(pc.or_(by_code, by_text)).slice(0, limit).to_pylist()
    return [(r["code"], r["code_type"], r["description"]) for r in hits]


# --- coverage and data quality -------------------------------------------------------


def refusal_summary(data: Dataset, filters: Filters) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for row in apply(data.table("refusals"), filters):
        counts[str(row["reason"])] = counts.get(str(row["reason"]), 0) + int(row["candidates"])
    total = sum(counts.values())
    return [
        {
            "reason": name,
            "label": reason_label(name),
            "sentence": REASONS.get(name, ("", ""))[1],
            "rates": n,
            "share": n / total if total else 0.0,
        }
        for name, n in sorted(counts.items(), key=lambda kv: -kv[1])
    ]


def freshness(metadata: dict[str, Any]) -> str:
    built = str(metadata.get("built_at") or "")[:10] or "unknown"
    gold = str(metadata.get("gold_published_at") or "")[:10] or "unknown"
    return f"Data built {built} · reconciled {gold}"


def to_csv(rows: list[dict[str, Any]], columns: list[str] | None = None) -> bytes:
    """A table as CSV bytes, for a download button: the rows on screen, filtered."""
    if not rows:
        return b""
    names = columns or list(rows[0])
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=names, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


__all__ = [
    "ALL",
    "EXPLANATIONS",
    "RANKINGS",
    "REASONS",
    "TABLES",
    "Dataset",
    "Filters",
    "PairDetail",
    "ReleaseFile",
    "apply",
    "code_lookup",
    "describe",
    "explanation_label",
    "fetch_release_file",
    "freshness",
    "headline",
    "load",
    "lookup_summary",
    "options",
    "pair_detail",
    "rankings",
    "read_rates",
    "reason_label",
    "refusal_summary",
    "signed_gap",
    "suggest",
    "to_csv",
]
