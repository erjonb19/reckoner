"""The 32BJ page's logic: 32BJ-tier hospital rates beside standard Anthem rates, without a UI.

32BJ Health Fund members on Anthem (Empire BlueCross BlueShield) reach hospitals through
their own tier, network ``NY Preferred Facility Tier1  32BJ`` in Anthem's file
``NY_HYBYMED32BJ``. The standard rate for the same facility and service is the one in
Anthem's HMO POS file ``NY_HXRLMED0000_06_06``. Both are dated 2026-10-01, so the two
columns carry no vintage gap.

Everything the page shows is computed here and imports no Streamlit, so what the page
claims is covered by tests (``tests/pipeline/test_thirtytwobj_view.py``). Rules, from
``docs/32bj-evaluation.md`` section 8:

- Dollars only in the main tables (``negotiated``, ``fee schedule``, ``derived``). Per
  diem and percent-of-charges rates are listed separately and never mixed in.
- Inpatient rates are never compared across DRG types: each system is shown only in the
  table of the DRG type its case rates use.
- A cell with several distinct rates shows its range with a flag; no value is picked.
- Revenue codes reach a CPT/HCPCS rate only through a bundle's list of revenue codes, and
  the rate never varies by revenue code (section 8.2), so an RC+CPT pair is labelled,
  not priced separately.

The data is public and local: the Parquet written by ``scripts/32bj``. When it is
missing the page says so; nothing here makes a network call.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # imported where used: the page must load where DuckDB is not installed
    import duckdb

TIER_FILE = "NY_HYBYMED32BJ.json.gz"
STANDARD_FILE = "NY_HXRLMED0000_06_06.json.gz"
TIER_NETWORK = "NY Preferred Facility Tier1  32BJ"

TIER = "32BJ tier"
STANDARD = "Standard Anthem"
TIER_FILE_HMO = "Standard (inside 32BJ file)"
BOTH = "Both"

DOLLAR_TYPES = ("negotiated", "fee schedule", "derived")
SYSTEMS = {
    "montefiore": "Montefiore",
    "mount_sinai": "Mount Sinai",
    "nyu": "NYU Langone",
    "northwell": "Northwell",
}
DEFAULT_SYSTEMS = ("montefiore", "mount_sinai", "nyu")
FLAG = "⚑"

#: Outpatient rates are held back until the St. Luke's Cornwall cross-check is explained
#: (docs/32bj-evaluation.md 8.10): its own file and Anthem's agree on every inpatient
#: case rate but on only about half of outpatient codes. While True, every table,
#: download and pasted-code lookup is limited to inpatient rates.
OUTPATIENT_UNDER_REVIEW = True
OUTPATIENT_NOTE = "Outpatient rates under review: they don't yet match the hospital's own file."

RC_LISTED = "bundle rate; RC listed"
RC_NOT_LISTED = "RC not listed for this code"
RC_NOT_SPECIFIC = "not specific to revenue code"


# --- building the tidy table --------------------------------------------------------


def capped(memory_limit: str = "1GB", spill: Path | None = None) -> duckdb.DuckDBPyConnection:
    """A DuckDB connection held to MEMORY_LIMIT that spills to SPILL instead of growing."""
    import duckdb

    con = duckdb.connect()
    con.sql(f"set memory_limit = '{memory_limit}'")
    con.sql("set preserve_insertion_order = false")
    con.sql("set threads = 2")
    if spill is not None:
        spill.mkdir(parents=True, exist_ok=True)
        con.sql(f"set temp_directory = '{spill.as_posix()}'")
    return con


def build_facility_rates(
    derived: Path, facilities_csv: Path, out: Path, memory_limit: str = "1GB"
) -> dict[str, int]:
    """Write one tidy Parquet of every non-professional rate at the listed facilities.

    Reads the items/groups/prices tables ``scripts/32bj/extract_facility_rates.py``
    wrote for both Anthem files. One output row per distinct (network, facility NPI,
    code, setting, rate, ...); a rate entry that names several campuses yields a row per
    campus, since the contract rate applies to each.

    The 32BJ file's groups table holds 114M rows, so this streams under a memory cap:
    the groups are first cut to the listed facilities' billing NPIs (a few thousand
    rows), then the join is copied straight to Parquet with no DISTINCT. A rate can
    repeat when a facility sits in two groups of one reference; that does not change a
    cell's lowest, highest or count of distinct rates. Each item's revenue-code list goes
    to a companion file, ``facility_items.parquet``, joined on load.
    """
    d = derived.as_posix()
    con = capped(memory_limit, derived / "_spill")
    with facilities_csv.open(encoding="utf-8") as fh:
        facilities = list(csv.DictReader(fh))
    npis = _sql_list(f["npi"] for f in facilities)
    con.sql(
        "create temp table f (npi varchar, system varchar, campus varchar, acute_campus boolean)"
    )
    con.executemany(
        "insert into f values (?, ?, ?, ?)",
        [(f["npi"], f["system"], f["campus"], f["acute_campus"] == "True") for f in facilities],
    )
    con.sql(f"""
        create temp table g as
        select source_file, provider_group_id, tin_value,
               list_contains(network_names, '{TIER_NETWORK}') as in_tier
        from read_parquet('{d}/*.groups.parquet')
        where tin_type = 'npi' and tin_value in {npis}
          and source_file in ('{TIER_FILE}', '{STANDARD_FILE}')""")
    con.sql(f"""
        copy (
            with p as (select source_file, item_idx, billing_class, negotiated_type, setting,
                              negotiated_rate, service_code, expiration_date,
                              unnest(provider_references) as gid
                       from read_parquet('{d}/*.prices.parquet')
                       where source_file in ('{TIER_FILE}', '{STANDARD_FILE}'))
            select
                case when p.source_file = '{STANDARD_FILE}' then '{STANDARD}'
                     when g.in_tier then '{TIER}' else '{TIER_FILE_HMO}' end as network,
                p.source_file, p.item_idx, f.system, f.campus, f.acute_campus,
                g.tin_value as billing_npi, i.billing_code_type as code_type,
                i.billing_code as code,
                i.severity_of_illness as severity,
                i.description, i.negotiation_arrangement as arrangement,
                p.billing_class, p.negotiated_type, p.setting, round(p.negotiated_rate, 2) as rate,
                array_to_string(p.service_code, ',') as service_code, p.expiration_date
            from p
            join g on g.source_file = p.source_file and g.provider_group_id = p.gid
            join f on f.npi = g.tin_value
            join (select source_file, item_idx, billing_code_type, billing_code,
                         severity_of_illness, description, negotiation_arrangement
                  from read_parquet('{d}/*.items.parquet')) i
              on i.source_file = p.source_file and i.item_idx = p.item_idx
        ) to '{out.as_posix()}' (format parquet, compression zstd)""")
    con.sql(f"""
        copy (select source_file, item_idx, bundled_rcs from read_parquet('{d}/*.items.parquet')
              where bundled_rcs is not null and source_file in ('{TIER_FILE}', '{STANDARD_FILE}'))
        to '{(out.parent / "facility_items.parquet").as_posix()}'
        (format parquet, compression zstd)""")
    return dict(
        con.sql(
            f"select network, count(*) from read_parquet('{out.as_posix()}') group by 1"
        ).fetchall()
    )


# --- loading ----------------------------------------------------------------------------


@dataclass
class Data:
    """The page's dataset: the tidy rates, the top-25 lists and the cross-check result."""

    con: duckdb.DuckDBPyConnection
    top25: list[dict[str, Any]]
    crosscheck: dict[str, Any] | None
    drg_type: dict[str, str]  # system -> the DRG type its inpatient case rates use
    codes: dict[tuple[str, str], str]  # (code type, code) -> description


def load(directory: Path) -> Data | None:
    """Open ``facility_rates.parquet`` and its companions in DIRECTORY, or None if absent."""
    rates = directory / "facility_rates.parquet"
    if not rates.exists():
        return None
    import duckdb  # raises ImportError where it is not installed; the page says so

    con = duckdb.connect()
    items = directory / "facility_items.parquet"
    if items.exists():
        con.sql(f"""create view rates as
                    select r.*, i.bundled_rcs from read_parquet('{rates.as_posix()}') r
                    left join read_parquet('{items.as_posix()}') i using (source_file, item_idx)""")
    else:
        con.sql(
            "create view rates as select *, null::varchar[] as bundled_rcs "
            f"from read_parquet('{rates.as_posix()}')"
        )
    top25: list[dict[str, Any]] = []
    top_path = directory / "top25.csv"
    if top_path.exists():
        with top_path.open(encoding="utf-8") as f:
            top25 = [
                dict(r, rank=int(r["rank"]), volume=int(r["volume"])) for r in csv.DictReader(f)
            ]
    cross_path = directory / "st_lukes_crosscheck.json"
    crosscheck = json.loads(cross_path.read_text()) if cross_path.exists() else None
    drg = con.sql(f"""
        select system, arg_max(code_type, n) from (
            select system, code_type, count(*) n from rates
            where code_type in ('APR-DRG', 'MS-DRG') and negotiated_type in {DOLLAR_TYPES}
            group by all) group by 1""").fetchall()
    codes = {
        (t, c): d
        for t, c, d in con.sql(
            "select distinct code_type, code, any_value(description) from rates group by all"
        ).fetchall()
    }
    return Data(con=con, top25=top25, crosscheck=crosscheck, drg_type=dict(drg), codes=codes)


# --- filters ----------------------------------------------------------------------------


@dataclass
class Filters:
    """What the reader narrowed the tables to. Empty tuples mean everything."""

    systems: tuple[str, ...] = DEFAULT_SYSTEMS
    campuses: tuple[str, ...] = ()
    setting: str = "All"  # "All", "inpatient" or "outpatient"; rows set to "both" match either
    code_types: tuple[str, ...] = ()
    network: str = BOTH  # BOTH, TIER or STANDARD
    include_units: bool = False  # psych, rehab, dialysis units and clinics besides acute campuses

    def where(self) -> str:
        parts = [f"negotiated_type in {DOLLAR_TYPES}", f"network in ('{TIER}', '{STANDARD}')"]
        if OUTPATIENT_UNDER_REVIEW:
            parts.append("setting = 'inpatient'")
        if self.systems:
            parts.append(f"system in {_sql_list(self.systems)}")
        if self.campuses:
            parts.append(f"campus in {_sql_list(self.campuses)}")
        if self.setting in ("inpatient", "outpatient"):
            parts.append(f"setting in ('{self.setting}', 'both')")
        if self.code_types:
            parts.append(f"code_type in {_sql_list(self.code_types)}")
        if self.network in (TIER, STANDARD):
            parts.append(f"network = '{self.network}'")
        if not self.include_units:
            parts.append("acute_campus")
        return " and ".join(parts)


def _sql_list(values: Iterable[str]) -> str:
    quoted = ["'" + str(v).replace("'", "''") + "'" for v in values]
    return "(" + ", ".join(quoted or ["''"]) + ")"


def options(data: Data, systems: Sequence[str] = ()) -> dict[str, list[str]]:
    """Choices for the sidebar: campuses follow the systems picked."""
    where = f"where system in {_sql_list(systems)}" if systems else ""
    return {
        "systems": [
            s
            for s in SYSTEMS
            if s in {r[0] for r in data.con.sql("select distinct system from rates").fetchall()}
        ],
        "campuses": [
            r[0]
            for r in data.con.sql(
                f"select distinct campus from rates {where} order by 1"
            ).fetchall()
        ],
        "code_types": [
            r[0] for r in data.con.sql("select distinct code_type from rates order by 1").fetchall()
        ],
    }


# --- cells -----------------------------------------------------------------------------


def cell_rows(
    data: Data, codes: Sequence[tuple[str, str]], filters: Filters
) -> list[dict[str, Any]]:
    """Long form: one row per (code, system, campus, network) with the dollar rates found.

    ``codes`` is a list of (code type, code). Several distinct rates in one cell keep
    their range and count, flagged; ``basis`` names the negotiated types behind them.
    """
    if not codes:
        return []
    pairs = ", ".join(f"('{t}', '{c}')" for t, c in codes)
    rows = data.con.sql(f"""
        select code_type, code, any_value(description) as description, system, campus, network,
               min(rate) as min_rate, max(rate) as max_rate, count(distinct rate) as n_rates,
               string_agg(distinct negotiated_type, ', ' order by negotiated_type) as basis,
               string_agg(distinct arrangement, ', ' order by arrangement) as arrangement,
               list(distinct billing_npi order by billing_npi) as billing_npis,
               flatten(list(distinct coalesce(bundled_rcs, []))) as bundled_rcs
        from rates
        where (code_type, code) in ({pairs}) and {filters.where()}
        group by code_type, code, system, campus, network""").fetchall()
    cols = [
        "code_type",
        "code",
        "description",
        "system",
        "campus",
        "network",
        "min_rate",
        "max_rate",
        "n_rates",
        "basis",
        "arrangement",
        "billing_npis",
        "bundled_rcs",
    ]
    out = [dict(zip(cols, r, strict=True)) for r in rows]
    for r in out:
        r["flag"] = FLAG if r["n_rates"] > 1 else ""
        r["system_name"] = SYSTEMS.get(r["system"], r["system"])
        r["bundled_rcs"] = sorted(set(r["bundled_rcs"] or []))
        r["billing_npis"] = ", ".join(r["billing_npis"])
    return out


def cell_text(row: dict[str, Any] | None) -> str:
    """How a cell reads: one dollar figure, or a flagged range with its count."""
    if not row:
        return "—"
    if row["n_rates"] > 1:
        low, high = f"${row['min_rate']:,.2f}", f"${row['max_rate']:,.2f}"
        return f"{low} – {high} {FLAG} ({row['n_rates']})"  # noqa: RUF001 - a typographic range
    return f"${row['min_rate']:,.2f}"


def ratio(tier: dict[str, Any] | None, standard: dict[str, Any] | None) -> float | None:
    """32BJ ÷ standard, only when both cells hold a single rate; a range has no one ratio."""
    if (
        not tier
        or not standard
        or tier["n_rates"] > 1
        or standard["n_rates"] > 1
        or not standard["min_rate"]
    ):
        return None
    return float(tier["min_rate"]) / float(standard["min_rate"])


@dataclass
class Table:
    """A wide table for the page: one row per code, a column per campus and network."""

    rows: list[dict[str, Any]]
    columns: list[str]
    long: list[dict[str, Any]]


def wide(
    data: Data,
    codes: Sequence[tuple[str, str]],
    filters: Filters,
    extra: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> Table:
    """Pivot :func:`cell_rows` so each code is a row and each campus has 32BJ and standard columns.

    ``extra`` adds leading columns per code (rank, volume, an RC from a pasted pair).
    """
    long = cell_rows(data, codes, filters)
    by = {(r["code_type"], r["code"], r["campus"], r["network"]): r for r in long}
    campuses = sorted(
        {(r["system"], r["campus"]) for r in long},
        key=lambda sc: (list(SYSTEMS).index(sc[0]), sc[1]),
    )
    networks = [n for n in (TIER, STANDARD) if filters.network in (BOTH, n)]
    columns: list[str] = []
    for _, campus in campuses:
        columns += [f"{campus} · {'32BJ' if n == TIER else 'standard'}" for n in networks]
        if len(networks) == 2:
            columns.append(f"{campus} · 32BJ ÷ standard")
    rows = []
    for t, c in codes:
        row: dict[str, Any] = dict((extra or {}).get((t, c), {}))
        rc = row.get("pasted RC")
        row.update({"code type": t, "code": c, "description": data.codes.get((t, c), "")})
        for _, campus in campuses:
            cells = {n: by.get((t, c, campus, n)) for n in networks}
            for n in networks:
                cell = cells[n]
                text = cell_text(cell)
                if rc and cell:
                    label = rc_label(cell, rc)
                    cell["rc_link"] = label
                    text = f"{text} · {label}"
                row[f"{campus} · {'32BJ' if n == TIER else 'standard'}"] = text
            if len(networks) == 2:
                q = ratio(cells[TIER], cells[STANDARD])
                row[f"{campus} · 32BJ ÷ standard"] = f"{q:.3f}" if q is not None else ""
        rows.append(row)
    return Table(rows=rows, columns=list(rows[0]) if rows else [], long=long)


def discount_summary(data: Data, filters: Filters) -> list[dict[str, Any]]:
    """Per campus: how many codes have both rates, and the median 32BJ ÷ standard among them."""
    f = Filters(**{**filters.__dict__, "network": BOTH})
    rows = data.con.sql(f"""
        with c as (select system, campus, network, code_type, code, setting,
                          negotiated_type, min(rate) lo, max(rate) hi
                   from rates where {f.where()} group by all),
             j as (select t.system, t.campus, t.lo / s.lo as q
                   from c t
                   join c s using (system, campus, code_type, code, setting, negotiated_type)
                   where t.network = '{TIER}' and s.network = '{STANDARD}'
                     and t.lo = t.hi and s.lo = s.hi and s.lo > 0)
        select system, campus, count(*) as codes_compared, median(q) as median_ratio,
               sum(case when abs(q - 1) < 0.0005 then 1 else 0 end) as identical
        from j group by all order by system, campus""").fetchall()
    return [
        {
            "system": SYSTEMS.get(s, s),
            "campus": c,
            "codes compared": n,
            "median 32BJ ÷ standard": round(m, 3),
            "identical": i,
        }
        for s, c, n, m, i in rows
    ]


# --- top 25 -----------------------------------------------------------------------------


TOP_TABLES = {
    "inpatient_apr_drg": ("Inpatient · APR-DRG", "APR-DRG"),
    "inpatient_ms_drg": ("Inpatient · MS-DRG", "MS-DRG"),
    "outpatient": ("Outpatient · CPT/HCPCS", None),
}


def visible_tables() -> list[str]:
    """The default tables the page shows: outpatient is left out while it is under review."""
    return [n for n in TOP_TABLES if not (OUTPATIENT_UNDER_REVIEW and n == "outpatient")]


def top_table(data: Data, name: str, filters: Filters) -> Table:
    """One of the three default tables, its systems limited to those using its DRG type."""
    _, drg = TOP_TABLES[name]
    picks = [r for r in data.top25 if r["table"] == name]
    systems = filters.systems or tuple(SYSTEMS)
    if drg:
        systems = tuple(s for s in systems if data.drg_type.get(s) == drg)
    f = Filters(**{**filters.__dict__, "systems": systems or ("none",)})
    if drg:
        f.setting = "inpatient"
    codes = []
    extra = {}
    for r in picks:
        code_type = r["code_type"]
        if not drg:  # the shoppable list names CPT codes; Anthem types some of them HCPCS
            code_type = next(
                (t for t in ("CPT", "HCPCS") if (t, r["code"]) in data.codes), r["code_type"]
            )
        codes.append((code_type, r["code"]))
        extra[(code_type, r["code"])] = {"rank": r["rank"], "volume": r["volume"]}
    return wide(data, codes, f, extra)


def top_caption(data: Data, name: str) -> str:
    picks = [r for r in data.top25 if r["table"] == name]
    unit = picks[0]["volume_unit"] if picks else ""
    note = {
        "inpatient_apr_drg": (
            "Montefiore prices inpatient stays by APR-DRG, so it appears only here. One row per "
            "DRG and severity level (1 minor to 4 extreme)."
        ),
        "inpatient_ms_drg": (
            "Mount Sinai and NYU Langone price inpatient stays by MS-DRG, so they appear only "
            "here. Ranked by Medicare volume, which leans toward conditions of people over 65; "
            "childbirth and other commercial-heavy stays are under-represented."
        ),
        "outpatient": (
            "The CPT/HCPCS codes from CMS's 70 shoppable services, ranked by New York "
            "facility-setting volume. Some labs and office visits on that list have no facility "
            "volume and rank last."
        ),
    }[name]
    return f"{note} Ranked by: {unit}."


# --- other methodologies ----------------------------------------------------------------


def other_methods(data: Data, filters: Filters, kind: str) -> list[dict[str, Any]]:
    """Per diem, percent-of-charges or revenue-code-only rates, never mixed into the dollars."""
    base = Filters(**filters.__dict__).where()
    base = base.replace(f"negotiated_type in {DOLLAR_TYPES}", "true")
    cond = {
        "per diem": "negotiated_type = 'per diem'",
        "percentage": "negotiated_type = 'percentage'",
        "rc": "code_type = 'RC'",
    }[kind]
    rows = data.con.sql(f"""
        select code_type, code, any_value(description), system, campus, network,
               negotiated_type, setting, min(rate), max(rate), count(distinct rate)
        from rates where {base} and {cond}
        group by code_type, code, system, campus, network, negotiated_type, setting
        order by code_type, code, system, campus, network limit 20000""").fetchall()
    cols = [
        "code type",
        "code",
        "description",
        "system",
        "campus",
        "network",
        "basis",
        "setting",
        "min",
        "max",
        "rates",
    ]
    out = [dict(zip(cols, r, strict=True)) for r in rows]
    for r in out:
        r["system"] = SYSTEMS.get(r["system"], r["system"])
        if kind == "percentage":
            r["min"] = f"{r['min']:g}% of charges"
            r["max"] = f"{r['max']:g}% of charges"
    return out


# --- pasted and picked codes ------------------------------------------------------------

HEADER_WORDS = re.compile(
    r"^(codes?|cpt|hcpcs|drg|ms-?drg|apr-?drg|rev(enue)?(\s*code)?|rc|code\s*type|description)$",
    re.I,
)


@dataclass
class Entry:
    """One pasted line after cleaning: the code, and the revenue code if a pair was pasted."""

    code: str
    rc: str | None = None
    raw: str = ""


@dataclass
class PasteResult:
    entries: list[Entry]
    pasted: int  # tokens kept after blanks and headers
    duplicates: int
    split_note: list[str] = field(default_factory=list)  # lines that looked like "139 4"


def looks_like_rc(token: str) -> bool:
    t = token.strip()
    return t.isdigit() and ((len(t) == 4 and t.startswith("0")) or len(t) == 3)


def parse_paste(text: str) -> PasteResult:
    """Split a column (or two) pasted from Excel into entries.

    Lines split on newlines; columns on tabs, then commas. A line with neither splits on
    spaces, except ``139 4``-shaped lines, which are read as APR-DRG with severity. Two
    columns become an RC+CPT pair when the first looks like a revenue code; otherwise only
    the first value is used. Headers and blanks are dropped, values trimmed, duplicates
    removed after normalising.
    """
    entries: list[Entry] = []
    seen: set[tuple[str, str | None]] = set()
    pasted = dupes = 0
    notes: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if "\t" in line:
            parts = [p.strip() for p in line.split("\t")]
        elif "," in line:
            parts = [p.strip() for p in line.split(",")]
        elif re.fullmatch(r"\d{1,3}\s+[1-4]", line):
            parts = [re.sub(r"\s+", "-", line)]
            notes.append(line)
        else:
            parts = line.split()
        parts = [p for p in parts if p and not HEADER_WORDS.match(p) and re.search(r"\d", p)]
        if not parts:
            continue
        items: list[tuple[str, str | None]]
        if "\t" in line and len(parts) >= 2 and looks_like_rc(parts[0]):
            items = [(parts[1], parts[0])]
        elif "\t" in line:
            items = [(parts[0], None)]
        else:
            items = [(p, None) for p in parts]
        for code, rc in items:
            pasted += 1
            key = (code.upper(), rc)
            if key in seen:
                dupes += 1
                continue
            seen.add(key)
            entries.append(
                Entry(code=code.upper().strip(), rc=rc.zfill(4) if rc else None, raw=line)
            )
    return PasteResult(entries=entries, pasted=pasted, duplicates=dupes, split_note=notes)


def candidates(token: str) -> list[tuple[str, str]]:
    """Every (code type, code) a pasted token could mean, leading zeros restored.

    Resolution against the data (:func:`resolve`) picks among these; nothing here guesses.
    A three-digit token yields both DRG families: MS-DRG ``NNN`` and the four APR-DRG
    severities ``NNN1``-``NNN4``, so each system is matched to its own DRG type.
    """
    m = re.fullmatch(r"(\d{1,3})\s*[-.\s]\s*([1-4])", token.strip())
    if m:
        return [("APR-DRG", f"{int(m.group(1)):03d}{m.group(2)}")]
    t = token.strip().upper().replace(" ", "")
    if re.fullmatch(r"[A-Z]\d{4}", t):
        return [("HCPCS", t)]
    if re.fullmatch(r"\d{4}[A-Z]", t):
        return [("CPT", t), ("HCPCS", t)]
    if not t.isdigit():
        return []
    out: list[tuple[str, str]] = []
    if len(t) == 5:
        out += [("CPT", t), ("HCPCS", t)]
    if len(t) == 4:
        out += [("APR-DRG", t), ("RC", t), ("CPT", t.zfill(5))]
    if len(t) <= 3:
        drg = t.zfill(3)
        out += [("MS-DRG", drg)] + [("APR-DRG", f"{drg}{s}") for s in "1234"]
        out += [("RC", t.zfill(4)), ("CPT", t.zfill(5))]
    return out


@dataclass
class Resolution:
    found: list[tuple[str, str]]  # (code type, code) to show, in paste order
    not_found: list[str]
    ambiguous: list[dict[str, Any]]  # token and every kind it matched
    rc_for: dict[tuple[str, str], str]  # (type, code) -> RC pasted with it


def resolve(
    paste: PasteResult, available: Iterable[tuple[str, str]], picked: Sequence[tuple[str, str]] = ()
) -> Resolution:
    """Match pasted entries to codes in the data; flag tokens that hit more than one kind.

    MS-DRG and APR-DRG hits from one three-digit token count as one kind, "DRG": that is
    the same DRG read in each system's own type, not an ambiguity.
    """
    have = set(available)
    found: list[tuple[str, str]] = []
    not_found: list[str] = []
    ambiguous: list[dict[str, Any]] = []
    rc_for: dict[tuple[str, str], str] = {}
    for e in paste.entries:
        hits = [c for c in candidates(e.code) if c in have]
        if not hits:
            not_found.append(e.code if not e.rc else f"{e.rc} + {e.code}")
            continue
        kinds = {"DRG" if t in ("MS-DRG", "APR-DRG") else t for t, _ in hits}
        if len(kinds) > 1:
            ambiguous.append({"pasted": e.code, "matches": ", ".join(f"{t} {c}" for t, c in hits)})
        for h in hits:
            if h not in found:
                found.append(h)
            if e.rc:
                rc_for[h] = e.rc
    for p in picked:
        if p not in found:
            found.append(p)
    return Resolution(found=found, not_found=not_found, ambiguous=ambiguous, rc_for=rc_for)


def rc_label(row: dict[str, Any], rc: str | None) -> str:
    """For an RC+CPT pair: whether this cell's CPT rate is tied to the pasted revenue code."""
    if not rc:
        return ""
    if "bundle" in (row.get("arrangement") or ""):
        return RC_LISTED if rc in (row.get("bundled_rcs") or []) else RC_NOT_LISTED
    return RC_NOT_SPECIFIC


# --- words ------------------------------------------------------------------------------


def summary_points(data: Data) -> list[str]:
    """The plain-English summary, with its numbers computed from the data rather than typed in."""
    disc = discount_summary(data, Filters(systems=tuple(SYSTEMS), include_units=False))
    below = [d for d in disc if d["median 32BJ ÷ standard"] < 0.995]
    at = [d for d in disc if abs(d["median 32BJ ÷ standard"] - 1) < 0.005]
    points = [
        "32BJ members using Anthem have their own hospital tier, "
        '"NY Preferred Facility Tier1 32BJ", published by Anthem (Empire BlueCross BlueShield) '
        "on 2026-10-01."
    ]
    if below:
        names = ", ".join(sorted({d["campus"] for d in below}))
        pct = round((1 - median(d["median 32BJ ÷ standard"] for d in below)) * 100)
        points.append(
            f"At {names}, 32BJ rates are about {pct}% below Anthem's standard rates for the "
            "same service."
        )
    if at:
        systems = sorted({d["system"] for d in at})
        points.append(
            f"Every other acute campus with both rates ({', '.join(systems)}) is at the "
            "standard Anthem rate."
        )
    nw = data.con.sql(f"""
        select count(distinct campus) filter (where network = '{TIER}' and acute_campus),
               count(distinct campus) filter (where network = '{STANDARD}' and acute_campus),
               count(distinct campus) filter (where network = '{TIER}' and not acute_campus)
        from rates where system = 'northwell'""").fetchone()
    if nw is not None and nw[1] and nw[0] == 0:
        units = (
            " Some Northwell psychiatric, dialysis, rehabilitation and outpatient-surgery units "
            f"({nw[2]}) are in the 32BJ tier."
            if nw[2]
            else ""
        )
        points.append(
            f"Northwell's {nw[1]} main hospitals are not in the 32BJ tier: Anthem lists them "
            f"only at its standard rates.{units} 32BJ reaches Northwell's hospitals through a "
            "separate direct contract, Northwell Direct."
        )
    if OUTPATIENT_UNDER_REVIEW:
        points.append(OUTPATIENT_NOTE + " Only inpatient rates are shown for now.")
    return points


DEFINITIONS = [
    (
        "32BJ-tier rate",
        'The rate Anthem lists for a hospital in network "NY Preferred Facility Tier1 32BJ", in '
        "file NY_HYBYMED32BJ. It is what applies to 32BJ Health Fund members on Anthem.",
    ),
    (
        "Standard Anthem rate",
        "The rate Anthem lists for the same hospital and service in its HMO POS network, file "
        "NY_HXRLMED0000_06_06, published the same day.",
    ),
    (
        "32BJ ÷ standard",
        "The 32BJ rate divided by the standard rate. 0.917 means 8.3% lower; 1.000 means the same. "
        "Shown only when both cells hold a single rate.",
    ),
    (
        "Campus",
        "A hospital as Anthem bills it, by its NPI. Montefiore's Bronx campuses (Moses, Weiler, "
        "Wakefield) bill under one NPI, so they are one column.",
    ),
    (
        "APR-DRG and MS-DRG",
        "Two ways of grouping a hospital stay into one priced case. APR-DRG adds a severity level "
        "from 1 (minor) to 4 (extreme). Montefiore uses APR-DRG; Mount Sinai and NYU Langone use "
        "MS-DRG. The two are never compared.",
    ),
    (
        'Case rate ("derived")',
        'One dollar amount for the whole stay. Anthem labels its DRG case rates "derived".',
    ),
    ("Fee schedule / negotiated", "A dollar amount for one service, such as a CT scan."),
    (
        "Per diem",
        "A dollar amount per day. Listed separately, because it is not comparable with a "
        "case rate.",
    ),
    (
        "Percent of charges",
        "A percentage of the hospital's list price. It has no dollar value without the hospital's "
        "price list, so it is counted, not shown as dollars.",
    ),
    (
        f"{FLAG} and a range",
        "Anthem lists more than one rate for that code at that campus (for example for different "
        "NPIs of one campus). The lowest and highest are shown with how many there are.",
    ),
    (
        "Revenue code (RC)",
        "A hospital department code, such as 0450 for the emergency room. Some RC rows are "
        "rates of their own (mostly per diems). For CPT/HCPCS codes, Anthem lists which revenue "
        "codes a rate applies under; the rate itself never changes with the revenue code.",
    ),
    (RC_LISTED, "The CPT rate is a bundle whose list includes the revenue code you pasted."),
    (
        RC_NOT_LISTED,
        "The CPT rate is a bundle, but its list does not include the revenue code you pasted. The "
        "contract does not say what happens then; the rate is shown, flagged.",
    ),
    (RC_NOT_SPECIFIC, "The CPT rate is a plain rate with no revenue-code list."),
    ("—", "No rate of that kind for that code at that campus."),
]

SOURCES = [
    (
        "Anthem 32BJ file",
        "NY_HYBYMED32BJ.json.gz, Empire BlueCross BlueShield, Transparency in Coverage in-network "
        "file, schema 2.0.0, last updated 2026-10-01.",
    ),
    (
        "Anthem standard file",
        "NY_HXRLMED0000_06_06.json.gz, same publisher and date, network HMO POS PROVIDERS.",
    ),
    (
        "Campuses",
        "NPPES (the federal NPI registry), each NPI resolved to its organisation name, "
        "address and type.",
    ),
    (
        "Most common APR-DRGs",
        "NY SPARCS Hospital Inpatient Discharges (De-Identified) 2023, private insurance.",
    ),
    (
        "Most common MS-DRGs",
        "CMS Medicare Inpatient Hospitals by Geography and Service 2024, New York.",
    ),
    (
        "Most common outpatient codes",
        "CMS's 70 shoppable services (84 FR 65571), ranked by CMS Medicare Physician & "
        "Other Practitioners by Geography and Service 2024, New York, facility setting.",
    ),
]

LIMITS = [
    "Montefiore's Bronx campuses are one column, because they bill Anthem under one NPI.",
    "The rates are what Anthem published; they are not claims, and say nothing about how often "
    "a service is used.",
    "Percent-of-charges and per diem rates are not converted to dollars.",
    "The MS-DRG ranking uses Medicare volume, which under-represents childbirth and other "
    "younger-patient stays.",
    "St. Luke's Cornwall's own hospital file is dated April 2026; Anthem's files are dated "
    "October 2026. The two agree on every inpatient case rate but on only about half of "
    "outpatient codes, so outpatient rates are held back until that is explained.",
    "Everything here is public data. Nothing comes from any hospital's internal systems.",
]


# --- downloads --------------------------------------------------------------------------


def to_csv(rows: list[dict[str, Any]]) -> bytes:
    if not rows:
        return b""
    buffer = io.StringIO()
    w = csv.DictWriter(buffer, fieldnames=list(rows[0]), extrasaction="ignore")
    w.writeheader()
    w.writerows([{k: _flat(v) for k, v in r.items()} for r in rows])
    return buffer.getvalue().encode("utf-8")


def _flat(v: Any) -> Any:  # noqa: ANN401 - cells are mixed
    return ", ".join(map(str, v)) if isinstance(v, list | tuple) else v


def to_xlsx(sheets: dict[str, list[dict[str, Any]]]) -> bytes:
    """An Excel workbook, one sheet per entry, header row bold and frozen."""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name[:31])
        if not rows:
            ws.append(["(no rows)"])
            continue
        cols = list(rows[0])
        ws.append(cols)
        for c in ws[1]:
            c.font = Font(bold=True)
        for r in rows:
            ws.append([_flat(r.get(c)) for c in cols])
        ws.freeze_panes = "A2"
        for i, c in enumerate(cols, 1):
            width = max([len(str(c))] + [len(str(_flat(r.get(c)) or "")) for r in rows[:200]])
            ws.column_dimensions[ws.cell(1, i).column_letter].width = min(60, width + 2)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def findings_workbook(data: Data, rows: list[dict[str, Any]]) -> bytes:
    """Summary, definitions and the filtered data rows, as one workbook."""
    summary = [{"finding": p} for p in summary_points(data)]
    summary += [
        {
            "finding": f"{d['campus']}: median 32BJ ÷ standard {d['median 32BJ ÷ standard']} over "
            f"{d['codes compared']} codes"
        }
        for d in discount_summary(data, Filters(systems=tuple(SYSTEMS)))
    ]
    definitions = [{"term": t, "meaning": m} for t, m in DEFINITIONS]
    definitions += [{"term": f"Source: {t}", "meaning": m} for t, m in SOURCES]
    definitions += [{"term": "Limit", "meaning": m} for m in LIMITS]
    return to_xlsx({"Summary": summary, "Definitions": definitions, "Data": rows})


__all__ = [
    "BOTH",
    "OUTPATIENT_NOTE",
    "OUTPATIENT_UNDER_REVIEW",
    "STANDARD",
    "TIER",
    "Data",
    "Entry",
    "Filters",
    "PasteResult",
    "Resolution",
    "Table",
    "build_facility_rates",
    "candidates",
    "cell_rows",
    "cell_text",
    "discount_summary",
    "findings_workbook",
    "load",
    "options",
    "other_methods",
    "parse_paste",
    "ratio",
    "rc_label",
    "resolve",
    "summary_points",
    "to_csv",
    "to_xlsx",
    "top_caption",
    "top_table",
    "visible_tables",
    "wide",
]
