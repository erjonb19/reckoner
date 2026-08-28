"""Load the published NYS DOH rate schedules and APR-DRG weight tables.

Both files are laid out for human readers -- multi-row headers, numbered column
bands, footnotes -- so the loaders locate the data by shape rather than by fixed
cell references, which would break the first time DOH inserts a row.
"""

from __future__ import annotations

import re
from pathlib import Path

import openpyxl

from model.nys_medicaid import Basis, DrgWeight, RateSchedule

#: Column order in PUB_MA_HMO_Acute / PUB_MA_FFS_Acute, after OPCERT and name.
#: The two publications differ: FFS carries an admission rate that MMC does not,
#: so the bands are resolved per basis rather than assumed.
_MMC_COLUMNS = {
    "discharge_rate": 0,
    "statewide_price": 1,
    "isaf": 2,
    "high_cost_charge_converter": 3,
    "ime_pct": 4,
    "dme_rate": 5,
    "capital_per_discharge": 6,
    # Columns 8-13: the non-comparable and directed payment add-ons. The DOH
    # inlier worksheet adds these alongside capital, and omitting them
    # understates safety-net and public hospitals specifically.
    "ambulance_addon": 7,
    "teaching_physicians_addon": 8,
    "nursing_school_addon": 9,
    "minimum_wage_addon": 10,
    "safety_net_addon": 11,
    "acr_addon": 12,
    "capital_per_diem": 13,
    "alc_rate": 14,
    "hcra_surcharge": 15,
}
_FFS_COLUMNS = {
    "admission_rate": 0,
    "discharge_rate": 1,
    "statewide_price": 2,
    "isaf": 3,
    "high_cost_charge_converter": 4,
    "ime_pct": 5,
    "dme_rate": 6,
    # FFS column 8 is captioned "capital per discharge PLUS non-comparables",
    # so the add-ons are already inside it. Adding them again would double-count
    # exactly the hospitals the add-ons exist to support.
    "capital_per_discharge": 7,
    "capital_per_diem": 8,
    "alc_rate": 9,
    "hcra_surcharge": 10,
}

#: MMC add-on columns that are paid per discharge on top of capital.
_MMC_ADDON_FIELDS = (
    "ambulance_addon",
    "teaching_physicians_addon",
    "nursing_school_addon",
    "minimum_wage_addon",
    "safety_net_addon",
    "acr_addon",
)


def _number(value: object) -> float:
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        cleaned = re.sub(r"[$,%\s]", "", value)
        try:
            return float(cleaned)
        except ValueError:
            return 0.0
    return 0.0


def load_rate_schedules(path: Path, basis: Basis, effective_date: str = "") -> list[RateSchedule]:
    """Read one hospital rate publication into RateSchedule rows.

    Data rows are the ones whose first cell is a numeric operating certificate;
    everything above is header banding and everything below is footnotes.
    """
    book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheet_name = next(
        (n for n in book.sheetnames if n.upper().endswith("_ACUTE")), book.sheetnames[0]
    )
    sheet = book[sheet_name]
    columns = _FFS_COLUMNS if basis is Basis.FFS else _MMC_COLUMNS

    schedules: list[RateSchedule] = []
    for row in sheet.iter_rows(values_only=True):
        if not row or row[0] is None or row[1] is None:
            continue
        opcert = str(row[0]).strip()
        if not opcert.isdigit():
            continue
        values = row[2:]

        def band(name: str, values: tuple[object, ...] = values) -> float:
            index = columns.get(name)
            if index is None or index >= len(values):
                return 0.0
            return _number(values[index])

        # FFS already folds the non-comparables into capital; only MMC lists
        # them separately, so only MMC sums them.
        directed = sum(band(field) for field in _MMC_ADDON_FIELDS) if basis is Basis.MMC else 0.0
        safety_net = band("safety_net_addon") + band("acr_addon")
        surcharge = band("hcra_surcharge")

        schedules.append(
            RateSchedule(
                opcert=opcert,
                hospital=str(row[1]).strip(),
                discharge_rate=band("discharge_rate"),
                isaf=band("isaf"),
                high_cost_charge_converter=band("high_cost_charge_converter"),
                capital_per_discharge=band("capital_per_discharge"),
                capital_per_diem=band("capital_per_diem"),
                alc_rate=band("alc_rate"),
                dme_rate=band("dme_rate"),
                ime_pct=band("ime_pct"),
                statewide_price=band("statewide_price"),
                addons_per_discharge=directed,
                transfer_addons=safety_net,
                # The published per-hospital surcharge beats the statutory
                # default, which is only a fallback for a blank cell.
                hcra_surcharge=surcharge if surcharge > 0 else 0.0,
                effective_date=effective_date,
                basis=str(basis),
            )
        )
    return schedules


def load_drg_weights(path: Path) -> list[DrgWeight]:
    """Read the APR-DRG service intensity weight table."""
    book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheet = book[book.sheetnames[0]]

    weights: list[DrgWeight] = []
    for row in sheet.iter_rows(values_only=True):
        if not row or row[0] is None or len(row) < 6:
            continue
        code = str(row[0]).strip()
        severity = str(row[1]).strip() if row[1] is not None else ""
        if not code.isdigit() or severity not in {"1", "2", "3", "4"}:
            continue
        weights.append(
            DrgWeight(
                apr_drg=code.zfill(3),
                severity=int(severity),
                siw=_number(row[3]),
                alos=_number(row[4]),
                cost_outlier_threshold=_number(row[5]),
                description=str(row[2]).strip() if row[2] else "",
            )
        )
    return weights


class WeightTable:
    """Lookup for APR-DRG weights, keyed the way hospitals publish them."""

    def __init__(self, weights: list[DrgWeight]) -> None:
        self._by_key = {w.key: w for w in weights}

    def get(self, code: str, severity: int | None = None) -> DrgWeight | None:
        """Find a weight from either "194-2" or ("194", 2).

        Hospital MRFs publish the joined form; the DOH table publishes the parts.
        """
        text = code.strip()
        if severity is None and "-" in text:
            text, _, tail = text.rpartition("-")
            severity = int(tail) if tail.isdigit() else None
        if severity is None:
            return None
        return self._by_key.get(f"{text.strip().zfill(3)}-{severity}")

    def __len__(self) -> int:
        return len(self._by_key)


class RateTable:
    """Lookup for hospital rate schedules by operating certificate."""

    def __init__(self, schedules: list[RateSchedule]) -> None:
        self._by_opcert: dict[str, RateSchedule] = {}
        for schedule in schedules:
            # An opcert can cover several facilities at the same rate; the first
            # row wins and the rest are the same numbers under another name.
            self._by_opcert.setdefault(schedule.opcert, schedule)

    def get(self, licence_or_opcert: str | None) -> RateSchedule | None:
        """Accept an MRF licence number ("7001020H") or a bare opcert."""
        if not licence_or_opcert:
            return None
        digits = re.sub(r"[^0-9]", "", licence_or_opcert)
        return self._by_opcert.get(digits)

    def __len__(self) -> int:
        return len(self._by_opcert)
