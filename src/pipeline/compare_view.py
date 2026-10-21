"""The Compare page's logic: hospitals side by side, for one insurer.

The analyst pages ask where a hospital and an insurer disagree. This asks the
question leadership asks first: *for the same service and the same insurer, what
does each hospital charge?* It reads the monthly rate release that code lookup
already downloads (one row per facility, insurer and code), so it needs no new
pipeline stage.

**What a rate here is, and is not.** ``hospital_rate`` is the median of every rate
the hospital lists for that insurer and code. That pools commercial plans with
Medicare Advantage and Medicaid plans, and can pool inpatient with outpatient and
facility with professional fees. Gaps between hospitals are therefore direction,
not a contract-grade price. The page says so on every view, and flags any rate far
from the group, so a pooled outlier is something to check rather than a finding.
A table that keeps those apart is the plan's Step 1 (``docs/compare-page-plan.md``).

Like ``analyst_view`` this imports no Streamlit: what the page claims is covered by
tests rather than by looking at it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from statistics import median
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

# --- hospital names ----------------------------------------------------------------

# UTF-8 read as Windows-1252 somewhere upstream: Montefiore's own file names come
# through as "Montefiore Medical Center â€“ Childrenâ€™s Hospital".
MOJIBAKE = {
    "\u00e2\u20ac\u201c": "\u2013",  # en dash
    "\u00e2\u20ac\u201d": "\u2014",  # em dash
    "\u00e2\u20ac\u2122": "\u2019",  # right single quote
    "\u00e2\u20ac\u02dc": "\u2018",  # left single quote
    "\u00e2\u20ac\u0153": "\u201c",  # left double quote
    "\u00e2\u20ac\u009d": "\u201d",  # right double quote
}

MONTEFIORE_BRONX = "Montefiore Medical Center (Bronx campuses)"

# Names a rule cannot derive. The Bronx file lists seven campuses under one set of
# rates; "Montefiore Health System" is where a rate names no facility at all.
LABELS = {
    "Montefiore Medical Center": MONTEFIORE_BRONX,
    "Montefiore Health System": "Montefiore (location not stated)",
}


def fix_text(text: str) -> str:
    for broken, right in MOJIBAKE.items():
        text = text.replace(broken, right)
    return re.sub(r"\b(\w+) \1\b", r"\1", text)  # "Zucker Hillside Hospital Hospital"


def locations(facility: str) -> list[str]:
    """The place names a facility entry covers, in file order, without repeats."""
    seen: list[str] = []
    for part in fix_text(facility).split("|"):
        name = part.strip()
        if name and name not in seen:
            seen.append(name)
    return seen


def facility_label(facility: str) -> str:
    """A short, unique, readable name for one facility entry.

    Files join several names with "|". Three shapes occur: one name repeated
    (Westchester Medical Center), a hospital and its departments (Nyack Hospital,
    Nyack Hospital - Hematology Oncology), and a brand and a hospital
    (NYU Langone|Tisch Hospital). Anything else keeps its first name and says how
    many more it covers, so two different entries never read as the same place.
    """
    names = locations(facility)
    if not names:
        return facility
    first = names[0]
    if first in LABELS:
        return LABELS[first]
    rest = names[1:]
    if not rest or all(name.startswith(first) for name in rest):
        return first
    if first == "NYU Langone" and len(rest) == 1:
        return f"NYU Langone {rest[0]}"
    return f"{first} (+{len(rest)} more)"


@dataclass(frozen=True)
class Hospital:
    label: str
    system: str
    facility: str

    @property
    def covers(self) -> list[str]:
        return locations(self.facility)


def hospitals(rates: pa.Table) -> list[Hospital]:
    """Every facility in the release, labelled, sorted by system then name."""
    pairs = rates.select(["system", "facility"]).group_by(["system", "facility"]).aggregate([])
    found = [
        Hospital(facility_label(f), s, f)
        for s, f in zip(
            pairs.column("system").to_pylist(), pairs.column("facility").to_pylist(), strict=True
        )
    ]
    labels = [h.label for h in found]
    clashes = {label for label in labels if labels.count(label) > 1}
    found = [
        Hospital(f"{h.label} ({h.system})", h.system, h.facility) if h.label in clashes else h
        for h in found
    ]
    return sorted(found, key=lambda h: (h.system, h.label))


# --- presets -----------------------------------------------------------------------

PRESETS: dict[str, list[str]] = {
    # Montefiore's separately published hospitals price very differently from the
    # Bronx campuses, so each stays its own entry. White Plains publishes as its
    # own system; it is grouped here by hand.
    "Montefiore family": [
        MONTEFIORE_BRONX,
        "Nyack Hospital",
        "St. Luke's Cornwall Hospital",
        "Montefiore Mount Vernon Hospital",
        "White Plains Hospital",
    ],
    "Bronx vs. Manhattan peers": [
        MONTEFIORE_BRONX,
        "The Mount Sinai Hospital",
        "NewYork-Presbyterian Columbia University Irving Medical Center",
        "NYU Langone Tisch Hospital",
    ],
}

# Common services every key hospital publishes, in plain words. Checked against
# the 2026-10-01 release: each is published by at least 6 of the 8 preset
# hospitals for all five insurers (93000 by 6, the DRGs by 7, the rest by all 8).
SERVICES: dict[str, str] = {
    "70450": "CT head, no contrast",
    "70551": "MRI brain, no contrast",
    "72148": "MRI lower spine, no contrast",
    "74177": "CT abdomen and pelvis, with contrast",
    "71046": "Chest X-ray, 2 views",
    "76700": "Ultrasound, abdomen, complete",
    "77067": "Screening mammogram",
    "80053": "Comprehensive metabolic panel (blood test)",
    "85025": "Complete blood count (CBC)",
    "93000": "Electrocardiogram (ECG)",
    "99283": "Emergency room visit, level 3 (moderate)",
    "99284": "Emergency room visit, level 4 (high)",
    "99285": "Emergency room visit, level 5 (highest)",
    "45378": "Colonoscopy, diagnostic",
    "43239": "Upper GI endoscopy with biopsy",
    "29881": "Knee arthroscopy",
    "66984": "Cataract surgery",
    "470": "Hip or knee replacement (inpatient stay)",
    "871": "Sepsis with major complications (inpatient stay)",
    "291": "Heart failure with major complications (inpatient stay)",
    "392": "Digestive disorders, no major complications (inpatient stay)",
    "807": "Vaginal delivery, no complications (inpatient stay)",
    "788": "C-section, no complications (inpatient stay)",
}

DEFAULT_BASKET = ["70450", "70551", "74177", "99284", "99285", "45378", "66984", "470"]

# A rate this far from the group median, either way, is flagged for checking: with
# pooled rates, it is more often a mix of plan types than a real price.
FAR = 3.0


def service_name(code: str, described: str = "") -> str:
    return SERVICES.get(code) or described or code


# --- the rows each view draws ---------------------------------------------------------


def _rows(
    rates: pa.Table,
    facilities: Sequence[str],
    carriers: Sequence[str],
    codes: Sequence[str],
) -> list[dict[str, Any]]:
    """Release rows for these facilities, insurers and codes, one per key.

    Matched on the code alone: one file labels 70450 CPT, another HCPCS. Where a
    facility lists the same code under both labels (995 keys in the October
    release), the row backed by more rates wins.
    """
    mask = pc.and_(
        pc.and_(
            pc.is_in(rates.column("facility"), pa.array(list(facilities), pa.string())),
            pc.is_in(rates.column("carrier"), pa.array(list(carriers), pa.string())),
        ),
        pc.is_in(rates.column("code"), pa.array(list(codes), pa.string())),
    )
    best: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rates.filter(mask).to_pylist():
        if row.get("hospital_rate") is None:
            continue
        key = (row["facility"], row["carrier"], row["code"])
        held = best.get(key)
        if held is None or (row.get("hospital_rates") or 0) > (held.get("hospital_rates") or 0):
            best[key] = row
    return list(best.values())


def _check(rate: float | None, middle: float | None) -> str:
    if rate is None or not middle:
        return ""
    ratio = rate / middle
    if ratio >= FAR:
        return f"{ratio:.1f}x the group: check before quoting"
    if ratio <= 1 / FAR:
        return f"{ratio:.2f}x the group: check before quoting"
    return ""


def head_to_head(
    rates: pa.Table, chosen: Sequence[Hospital], carrier: str, code: str
) -> list[dict[str, Any]]:
    """One code, one insurer: a row per chosen hospital, published or not.

    ``vs_lowest`` is how much more than the cheapest chosen hospital (0 for the
    cheapest); ``vs_middle`` is the distance from the chosen group's median.
    """
    found = {
        r["facility"]: r for r in _rows(rates, [h.facility for h in chosen], [carrier], [code])
    }
    published = [found[h.facility]["hospital_rate"] for h in chosen if h.facility in found]
    lowest = min(published) if published else None
    middle = median(published) if published else None
    out = []
    for h in chosen:
        row = found.get(h.facility)
        rate = row["hospital_rate"] if row else None
        out.append(
            {
                "hospital": h.label,
                "system": h.system,
                "rate": rate,
                "rates_behind_it": row.get("hospital_rates") if row else None,
                "vs_lowest": rate / lowest - 1 if rate is not None and lowest else None,
                "vs_middle": rate / middle - 1 if rate is not None and middle else None,
                "lowest": rate is not None and rate == lowest and len(published) > 1,
                "check": _check(rate, middle) if len(published) > 2 else "",
                "published": rate is not None,
            }
        )
    return sorted(out, key=lambda r: (r["rate"] is None, r["rate"] or 0.0))


def scorecard(
    rates: pa.Table, chosen: Sequence[Hospital], carrier: str, codes: Sequence[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Several codes, one insurer: a cell per code and hospital, and a summary.

    Cells carry ``ratio``, the rate over that code's median across the chosen
    hospitals, so a $40 lab test and a $40,000 surgery read on one scale. The
    summary's ``index`` is each hospital's median ratio: 1.20 means it typically
    sits 20% above the group. Codes fewer than two chosen hospitals publish are
    left out of both, since there is nothing to compare them with.
    """
    rows = _rows(rates, [h.facility for h in chosen], [carrier], codes)
    by_code: dict[str, dict[str, float]] = {}
    for row in rows:
        by_code.setdefault(row["code"], {})[row["facility"]] = row["hospital_rate"]
    label = {h.facility: h.label for h in chosen}
    cells: list[dict[str, Any]] = []
    ratios: dict[str, list[float]] = {h.label: [] for h in chosen}
    wins: dict[str, int] = {h.label: 0 for h in chosen}
    compared = 0
    for code in codes:
        found = by_code.get(code, {})
        if len(found) < 2:
            continue
        compared += 1
        middle = median(found.values())
        lowest = min(found.values())
        for h in chosen:
            rate = found.get(h.facility)
            ratio = rate / middle if rate is not None and middle else None
            if ratio is not None:
                ratios[h.label].append(ratio)
            if rate is not None and rate == lowest:
                wins[label[h.facility]] += 1
            cells.append(
                {
                    "code": code,
                    "hospital": h.label,
                    "rate": rate,
                    "ratio": ratio,
                    "check": _check(rate, middle) if len(found) > 2 else "",
                }
            )
    summary = [
        {
            "hospital": h.label,
            "index": median(ratios[h.label]) if ratios[h.label] else None,
            "lowest_on": wins[h.label],
            "published": len(ratios[h.label]),
            "compared": compared,
        }
        for h in chosen
    ]
    summary.sort(key=lambda r: (r["index"] is None, r["index"] or 0.0))
    return cells, summary


def by_insurer(
    rates: pa.Table, hospital: Hospital, carriers: Iterable[str], code: str
) -> list[dict[str, Any]]:
    """One hospital, one code: what each insurer's rate is, highest first."""
    wanted = list(carriers)
    found = {r["carrier"]: r for r in _rows(rates, [hospital.facility], wanted, [code])}
    out = [
        {
            "carrier": c,
            "rate": found[c]["hospital_rate"] if c in found else None,
            "rates_behind_it": found[c].get("hospital_rates") if c in found else None,
        }
        for c in wanted
    ]
    return sorted(out, key=lambda r: (r["rate"] is None, -(r["rate"] or 0.0)))


def carriers(rates: pa.Table) -> list[str]:
    return sorted(pc.unique(rates.column("carrier")).to_pylist())


def money(value: float | None) -> str:
    if value is None:
        return ""
    return f"${value:,.0f}" if value >= 100 else f"${value:,.2f}"


def percent(value: float | None) -> str:
    if value is None:
        return ""
    return "same" if abs(value) < 0.005 else f"{value:+.0%}"
