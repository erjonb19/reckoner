"""Extract every non-professional rate from an Anthem in-network file into three Parquet tables.

Usage:
    python -I scripts/32bj/extract_facility_rates.py FILE.json.gz OUT_DIR

Writes, into OUT_DIR, one file per table, named after the source file's stem:

- ``<stem>.items.parquet``: one row per in_network item (code, arrangement, bundled codes)
- ``<stem>.groups.parquet``: one row per provider group inside each provider reference
  (billing NPI, member NPIs, network names)
- ``<stem>.prices.parquet``: one row per non-professional negotiated price, with the
  provider-reference ids it applies to
- ``<stem>.meta.json``: header fields, counts, timings

Professional prices are counted, not written. Systems and campuses are assigned later,
in DuckDB, from ``facilities.csv``, so a missing campus costs a query rather than another
pass over the file (CLAUDE.md: parse once).

Two passes, each streaming with ijson and holding one item at a time. The first reads
provider_references and ends the stream at the top-level ``"in_network"`` key, so the
rate array is parsed once.
"""

from __future__ import annotations

import gzip
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, BinaryIO

import ijson
import pyarrow as pa
import pyarrow.parquet as pq

BATCH = 100_000

ITEMS = pa.schema(
    [
        ("source_file", pa.string()),
        ("item_idx", pa.int32()),
        ("billing_code_type", pa.string()),
        ("billing_code_type_version", pa.string()),
        ("billing_code", pa.string()),
        ("severity_of_illness", pa.string()),
        ("description", pa.string()),
        ("negotiation_arrangement", pa.string()),
        ("bundled_rcs", pa.list_(pa.string())),
        ("bundled_other", pa.list_(pa.string())),
    ]
)
GROUPS = pa.schema(
    [
        ("source_file", pa.string()),
        ("provider_group_id", pa.int64()),
        ("pg_idx", pa.int32()),
        ("network_names", pa.list_(pa.string())),
        ("tin_type", pa.string()),
        ("tin_value", pa.string()),
        ("npis", pa.list_(pa.string())),
    ]
)
PRICES = pa.schema(
    [
        ("source_file", pa.string()),
        ("item_idx", pa.int32()),
        ("rate_idx", pa.int32()),
        ("provider_references", pa.list_(pa.int64())),
        ("billing_class", pa.string()),
        ("negotiated_type", pa.string()),
        ("setting", pa.string()),
        ("service_code", pa.list_(pa.string())),
        ("modifier", pa.list_(pa.string())),
        ("negotiated_rate", pa.float64()),
        ("expiration_date", pa.string()),
        ("additional_information", pa.string()),
    ]
)


class CutAt:
    """A read-only file wrapper that ends the stream at the top-level in_network key."""

    def __init__(self, f: BinaryIO, marker: bytes = b'"in_network"') -> None:
        self.f, self.m, self.done, self.tail = f, marker, False, b""

    def read(self, n: int = -1) -> bytes:
        if self.done or n == 0:  # ijson probes with read(0) and discards the result
            return b""
        b = self.f.read(n if n > 0 else 1 << 20)
        i = (self.tail + b).find(self.m)
        if i >= 0:
            # the text before the key ends "...],"; closing the object keeps the JSON valid
            self.done = True
            return b[: max(0, i - len(self.tail))] + b'"_cut":0}'
        self.tail = b[-len(self.m) :]
        return b


class Sink:
    """Buffers rows as columns and writes a Parquet row group every BATCH rows."""

    def __init__(self, path: Path, schema: pa.Schema) -> None:
        self.schema = schema
        self.cols: dict[str, list[Any]] = {f.name: [] for f in schema}
        self.writer = pq.ParquetWriter(path, schema, compression="zstd")
        self.rows = 0

    def add(self, **row: Any) -> None:  # noqa: ANN401 - one row of mixed column types
        for k, v in self.cols.items():
            v.append(row.get(k))
        self.rows += 1
        if len(self.cols["source_file"]) >= BATCH:
            self.flush()

    def flush(self) -> None:
        if self.cols["source_file"]:
            self.writer.write_table(pa.table(self.cols, schema=self.schema))
            self.cols = {k: [] for k in self.cols}

    def close(self) -> None:
        self.flush()
        self.writer.close()


def strs(xs: Any) -> list[str] | None:  # noqa: ANN401 - whatever ijson produced
    return [str(x) for x in xs] if xs else None


def header(path: Path) -> dict[str, str]:
    with gzip.open(path, "rb") as f:
        head = f.read(4096).decode("utf-8", "replace")
    keys = "reporting_entity_name|reporting_entity_type|last_updated_on|version"
    return dict(re.findall(rf'"({keys})"\s*:\s*"([^"]*)"', head))


def main(src: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = src.name.removesuffix(".json.gz")
    t0 = time.time()
    meta: dict[str, Any] = {"source_file": src.name, "header": header(src)}

    groups = Sink(out_dir / f"{stem}.groups.parquet", GROUPS)
    with gzip.open(src, "rb") as raw:
        for ref in ijson.items(CutAt(raw), "provider_references.item"):
            nets = strs(ref.get("network_name"))
            for i, pg in enumerate(ref.get("provider_groups") or []):
                tin = pg.get("tin") or {}
                groups.add(
                    source_file=src.name,
                    provider_group_id=int(ref["provider_group_id"]),
                    pg_idx=i,
                    network_names=nets,
                    tin_type=tin.get("type"),
                    tin_value=str(tin.get("value")),
                    npis=strs(pg.get("npi")),
                )
    groups.close()
    meta["group_rows"] = groups.rows
    meta["pass1_seconds"] = round(time.time() - t0)
    print(
        f"pass1: {groups.rows} provider-group rows, {meta['pass1_seconds']}s",
        file=sys.stderr,
        flush=True,
    )

    items = Sink(out_dir / f"{stem}.items.parquet", ITEMS)
    prices = Sink(out_dir / f"{stem}.prices.parquet", PRICES)
    professional = inline = 0
    with gzip.open(src, "rb") as f:
        for idx, item in enumerate(ijson.items(f, "in_network.item")):
            if idx and idx % 5000 == 0:
                print(
                    f"  {idx} items, {prices.rows} prices kept, {time.time() - t0:.0f}s",
                    file=sys.stderr,
                    flush=True,
                )
            bundled = item.get("bundled_codes") or []
            items.add(
                source_file=src.name,
                item_idx=idx,
                billing_code_type=item.get("billing_code_type"),
                billing_code_type_version=str(item.get("billing_code_type_version")),
                billing_code=str(item.get("billing_code")),
                severity_of_illness=item.get("severity_of_illness"),
                description=item.get("description"),
                negotiation_arrangement=item.get("negotiation_arrangement"),
                bundled_rcs=[
                    str(b["billing_code"]) for b in bundled if b.get("billing_code_type") == "RC"
                ]
                or None,
                bundled_other=[
                    f"{b.get('billing_code_type')}:{b.get('billing_code')}"
                    for b in bundled
                    if b.get("billing_code_type") != "RC"
                ]
                or None,
            )
            for r, nr in enumerate(item.get("negotiated_rates") or []):
                if nr.get("provider_groups"):
                    inline += 1  # none seen in profiling; counted so a change is visible
                refs = [int(x) for x in nr.get("provider_references") or []]
                for pr in nr.get("negotiated_prices") or []:
                    if pr.get("billing_class") == "professional":
                        professional += 1
                        continue
                    prices.add(
                        source_file=src.name,
                        item_idx=idx,
                        rate_idx=r,
                        provider_references=refs,
                        billing_class=pr.get("billing_class"),
                        negotiated_type=pr.get("negotiated_type"),
                        setting=pr.get("setting"),
                        service_code=strs(pr.get("service_code")),
                        modifier=strs(pr.get("billing_code_modifier")),
                        negotiated_rate=float(pr["negotiated_rate"])
                        if pr.get("negotiated_rate") is not None
                        else None,
                        expiration_date=pr.get("expiration_date"),
                        additional_information=pr.get("additional_information"),
                    )
    items.close()
    prices.close()
    meta.update(
        items=items.rows,
        prices_kept=prices.rows,
        professional_prices_skipped=professional,
        inline_provider_group_rates=inline,
        seconds=round(time.time() - t0),
    )
    (out_dir / f"{stem}.meta.json").write_text(json.dumps(meta, indent=1))
    print("done", json.dumps(meta), file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(Path(sys.argv[1]), Path(sys.argv[2]))
