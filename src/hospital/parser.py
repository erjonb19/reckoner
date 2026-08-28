"""Parse a hospital MRF into raw rate rows, one per payer-specific charge.

Emits strings, not typed values: coercion and validation belong in curate.py, so
that a bad number becomes a quarantined row with a reason code rather than an
exception in the middle of a 5 GB stream.

Three real layouts are handled -- CMS template JSON, "tall" CSV with
payer_name/plan_name columns, and "wide" CSV encoding payer and plan in the
column headers. All three are streamed.
"""

from __future__ import annotations

import codecs
import csv
import io
from collections.abc import Iterator
from dataclasses import dataclass, field

import ijson

from hospital.profile import _MEASURES, _WIDE_COLUMN, find_header
from hospital.streaming import MrfStream


@dataclass(frozen=True)
class FileMeta:
    """Header metadata describing the file as a whole."""

    hospital_name: str | None = None
    last_updated_on: str | None = None
    version: str | None = None
    location_name: str | None = None
    license_number: str | None = None
    layout: str = "?"

    @property
    def has_vintage(self) -> bool:
        return bool(self.last_updated_on)


@dataclass(frozen=True)
class RawRate:
    """One payer-specific rate, still as published."""

    ordinal: int
    code: str | None = None
    code_type: str | None = None
    description: str | None = None
    setting: str | None = None
    billing_class: str | None = None
    modifiers: str | None = None
    #: Every (code, type) pair on the row. A single MRF row routinely carries a
    #: chargemaster id, a revenue code and a HCPCS code at once, and a service
    #: defined as "revenue code X with CPT Y" can only be matched if all of them
    #: survive parsing.
    codes: tuple[tuple[str, str], ...] = ()
    payer_name: str | None = None
    plan_name: str | None = None
    rate_dollar: str | None = None
    rate_percentage: str | None = None
    rate_algorithm: str | None = None
    methodology: str | None = None
    gross_charge: str | None = None
    discounted_cash: str | None = None
    extra: dict[str, str] = field(default_factory=dict)


class MrfParser:
    """Streams an MRF, exposing file metadata and an iterator of raw rates."""

    def __init__(self, stream: MrfStream) -> None:
        self._stream = stream
        self._reader = stream.reader()
        if self._reader.peek(3)[:3] == codecs.BOM_UTF8:
            self._reader.read(3)
        self._is_json = self._reader.peek(4096).lstrip()[:1] == b"{"
        self.meta = FileMeta(layout="json" if self._is_json else "csv")

    def __iter__(self) -> Iterator[RawRate]:
        if self._is_json:
            return self._iter_json()
        return self._iter_csv()

    # -- JSON ------------------------------------------------------------

    def _iter_json(self) -> Iterator[RawRate]:
        ordinal = 0
        # ijson.kvitems over the document root lets us read the header fields and
        # the charge array in a single pass, without buffering the document.
        for key, value in ijson.kvitems(self._reader, ""):
            if key != "standard_charge_information":
                self._absorb_meta(key, value)
                continue
            for item in value:
                for charge in item.get("standard_charges") or []:
                    for payer in charge.get("payers_information") or []:
                        ordinal += 1
                        yield self._json_rate(ordinal, item, charge, payer)

    def _absorb_meta(self, key: str, value: object) -> None:
        text = _first_str(value)
        mapping = {
            "hospital_name": "hospital_name",
            "last_updated_on": "last_updated_on",
            "version": "version",
            "location_name": "location_name",
        }
        if key in mapping and text:
            self.meta = _replace_meta(self.meta, mapping[key], text)
        elif key == "license_information" and isinstance(value, dict):
            number = value.get("license_number")
            if number:
                self.meta = _replace_meta(self.meta, "license_number", str(number))

    @staticmethod
    def _json_rate(
        ordinal: int,
        item: dict[str, object],
        charge: dict[str, object],
        payer: dict[str, object],
    ) -> RawRate:
        raw_codes = item.get("code_information")
        entries: list[dict[str, object]] = (
            [entry for entry in raw_codes if isinstance(entry, dict)]
            if isinstance(raw_codes, list)
            else []
        )
        pairs = tuple(
            (_text(entry.get("code")) or "", _text(entry.get("type")) or "")
            for entry in entries
            if _text(entry.get("code"))
        )
        first: dict[str, object] = entries[0] if entries else {}
        return RawRate(
            ordinal=ordinal,
            code=_text(first.get("code")),
            code_type=_text(first.get("type")),
            codes=pairs,
            description=_text(item.get("description")),
            setting=_text(charge.get("setting")),
            billing_class=_text(charge.get("billing_class")),
            modifiers=_text(charge.get("modifiers")),
            payer_name=_text(payer.get("payer_name")),
            plan_name=_text(payer.get("plan_name")),
            rate_dollar=_text(payer.get("standard_charge_dollar")),
            rate_percentage=_text(payer.get("standard_charge_percentage")),
            rate_algorithm=_text(payer.get("standard_charge_algorithm")),
            methodology=_text(payer.get("methodology")),
            gross_charge=_text(charge.get("gross_charge")),
            discounted_cash=_text(charge.get("discounted_cash")),
        )

    # -- CSV -------------------------------------------------------------

    def _iter_csv(self) -> Iterator[RawRate]:
        text = io.TextIOWrapper(self._reader, encoding="utf-8-sig", errors="replace", newline="")
        rows = csv.reader(text)

        preamble: list[list[str]] = []
        header: list[str] | None = None
        for index, row in enumerate(rows):
            if _looks_like_header(row):
                header = row
                break
            preamble.append(row)
            if index > 8:
                break
        if header is None:
            return

        self._absorb_csv_meta(preamble)
        index_of = {name.strip().lower(): i for i, name in enumerate(header)}
        if "payer_name" in index_of:
            self.meta = _replace_meta(self.meta, "layout", "csv-tall")
            yield from _iter_tall(rows, index_of)
        else:
            self.meta = _replace_meta(self.meta, "layout", "csv-wide")
            yield from _iter_wide(rows, header)

    def _absorb_csv_meta(self, preamble: list[list[str]]) -> None:
        """Row 0 holds the metadata column names, row 1 the values."""
        if len(preamble) < 2:
            return
        names = [c.strip().lower() for c in preamble[0]]
        values = preamble[1]
        for wanted in ("hospital_name", "last_updated_on", "version", "location_name"):
            if wanted in names:
                index = names.index(wanted)
                if index < len(values) and values[index].strip():
                    self.meta = _replace_meta(self.meta, wanted, values[index].strip())
        for name in names:
            if name.startswith("license_number"):
                index = names.index(name)
                if index < len(values) and values[index].strip():
                    self.meta = _replace_meta(self.meta, "license_number", values[index].strip())
                break


def _iter_tall(rows: Iterator[list[str]], index_of: dict[str, int]) -> Iterator[RawRate]:
    def cell(row: list[str], name: str) -> str | None:
        index = index_of.get(name, -1)
        if 0 <= index < len(row):
            value = row[index].strip()
            return value or None
        return None

    code_columns = sorted(
        {
            int(name.split("|")[1])
            for name in index_of
            if name.startswith("code|") and name.split("|")[1].isdigit()
        }
    )

    def all_codes(row: list[str]) -> tuple[tuple[str, str], ...]:
        found = []
        for n in code_columns:
            value = cell(row, f"code|{n}")
            if value:
                found.append((value, cell(row, f"code|{n}|type") or ""))
        return tuple(found)

    for ordinal, row in enumerate(rows, start=1):
        if not any(c.strip() for c in row):
            continue
        yield RawRate(
            ordinal=ordinal,
            code=cell(row, "code|1"),
            code_type=cell(row, "code|1|type"),
            codes=all_codes(row),
            description=cell(row, "description"),
            setting=cell(row, "setting"),
            billing_class=cell(row, "billing_class"),
            modifiers=cell(row, "modifiers"),
            payer_name=cell(row, "payer_name"),
            plan_name=cell(row, "plan_name"),
            rate_dollar=cell(row, "standard_charge|negotiated_dollar"),
            rate_percentage=cell(row, "standard_charge|negotiated_percentage"),
            rate_algorithm=cell(row, "standard_charge|negotiated_algorithm"),
            methodology=cell(row, "standard_charge|methodology"),
            gross_charge=cell(row, "standard_charge|gross"),
            discounted_cash=cell(row, "standard_charge|discounted_cash"),
        )


def _iter_wide(rows: Iterator[list[str]], header: list[str]) -> Iterator[RawRate]:
    """Wide layout: one row becomes several rates, one per payer/plan column."""
    plain = {name.strip().lower(): i for i, name in enumerate(header)}
    columns: list[tuple[int, str, str, str]] = []
    for i, name in enumerate(header):
        match = _WIDE_COLUMN.match(name.strip())
        if not match:
            continue
        measure = _MEASURES.get(match["measure"].lower())
        if measure:
            columns.append((i, match["payer"].strip(), match["plan"].strip(), measure))

    def cell(row: list[str], name: str) -> str | None:
        index = plain.get(name, -1)
        if 0 <= index < len(row):
            value = row[index].strip()
            return value or None
        return None

    wide_code_columns = sorted(
        {
            int(name.split("|")[1])
            for name in plain
            if name.startswith("code|") and name.split("|")[1].isdigit()
        }
    )

    def wide_codes(row: list[str]) -> tuple[tuple[str, str], ...]:
        found = []
        for n in wide_code_columns:
            value = cell(row, f"code|{n}")
            if value:
                found.append((value, cell(row, f"code|{n}|type") or ""))
        return tuple(found)

    ordinal = 0
    for row in rows:
        if not any(c.strip() for c in row):
            continue
        for i, payer, plan, measure in columns:
            if i >= len(row) or not row[i].strip():
                continue
            ordinal += 1
            value = row[i].strip()
            yield RawRate(
                ordinal=ordinal,
                code=cell(row, "code|1"),
                code_type=cell(row, "code|1|type"),
                codes=wide_codes(row),
                description=cell(row, "description"),
                setting=cell(row, "setting"),
                billing_class=cell(row, "billing_class"),
                payer_name=payer,
                plan_name=plan,
                rate_dollar=value if measure == "dollar" else None,
                rate_percentage=value if measure == "percentage" else None,
                rate_algorithm=value if measure == "algorithm" else None,
                gross_charge=cell(row, "standard_charge|gross"),
                discounted_cash=cell(row, "standard_charge|discounted_cash"),
            )


def _looks_like_header(row: list[str]) -> bool:
    return find_header(iter([row])) is not None


def _replace_meta(meta: FileMeta, attribute: str, value: str) -> FileMeta:
    from dataclasses import replace

    return replace(meta, **{attribute: value})


def _first_str(value: object) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, list) and value:
        return _first_str(value[0])
    if isinstance(value, int | float):
        return str(value)
    return None


def _text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    return str(value)
