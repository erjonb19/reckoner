"""Count the storage operations one system's mart run makes, with and without the spool.

Runs the mart twice over a local copy of silver, served through a filesystem
that counts what ADLS would bill: a listing per directory walk, a properties
call per file opened, and one ranged GET per read. The first run scans the
"lake" directly, as the mart did before; the second copies what it needs with
:mod:`pipeline.spool` and scans the copy. Each run writes its gold to its own
directory, and the files are compared by sha256: the spool must change where the
bytes come from and nothing else.

    python scripts/measure_mart_reads.py --silver C:/path/to/copy --system white-plains-hospital

``--silver`` is a directory holding ``silver/hospital_rates`` and
``silver/payer_rates`` exactly as the lake does (``pyarrow.fs.copy_files`` from
ADLS makes one). Nothing here touches the lake itself.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.fs as pafs

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import storage
from pipeline import mart
from storage import Location


class CountingFile:
    """A file object that counts each read, as a ranged GET would be counted."""

    def __init__(self, raw: Any, counts: Counter[str], lock: threading.Lock) -> None:  # noqa: ANN401
        self.raw, self.counts, self.lock = raw, counts, lock
        self.closed = False

    def read(self, n: int = -1) -> bytes:
        data = self.raw.read(n)
        with self.lock:
            self.counts["read"] += 1
            self.counts["bytes"] += len(data)
        return data

    def seek(self, offset: int, whence: int = 0) -> int:
        return self.raw.seek(offset, whence)

    def tell(self) -> int:
        return self.raw.tell()

    def close(self) -> None:
        self.closed = True
        self.raw.close()

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False


class CountingHandler(pafs.FileSystemHandler):
    """The local filesystem, counting what a remote one would charge for."""

    def __init__(self) -> None:
        self.local = pafs.LocalFileSystem()
        self.counts: Counter[str] = Counter()
        self.lock = threading.Lock()

    def _count(self, what: str) -> None:
        with self.lock:
            self.counts[what] += 1

    def __eq__(self, other: object) -> bool:
        return self is other

    def __ne__(self, other: object) -> bool:
        return self is not other

    def get_type_name(self) -> str:
        return "counting"

    def normalize_path(self, path: str) -> str:
        return path

    def get_file_info(self, paths: list[str]) -> list[pafs.FileInfo]:
        for _ in paths:
            self._count("info")
        return self.local.get_file_info(paths)

    def get_file_info_selector(self, selector: pafs.FileSelector) -> list[pafs.FileInfo]:
        self._count("list")
        return self.local.get_file_info(selector)

    def open_input_file(self, path: str) -> pa.NativeFile:
        self._count("open")
        raw = open(path, "rb")  # noqa: SIM115 - closed by the PythonFile that wraps it
        return pa.PythonFile(CountingFile(raw, self.counts, self.lock), mode="r")

    def open_input_stream(self, path: str) -> pa.NativeFile:
        return self.open_input_file(path)

    # Writes never happen here: the mart reads silver and writes gold elsewhere.
    def create_dir(self, path: str, recursive: bool) -> None:
        raise NotImplementedError

    def delete_dir(self, path: str) -> None:
        raise NotImplementedError

    def delete_dir_contents(self, path: str, missing_dir_ok: bool = False) -> None:
        raise NotImplementedError

    def delete_root_dir_contents(self) -> None:
        raise NotImplementedError

    def delete_file(self, path: str) -> None:
        raise NotImplementedError

    def move(self, src: str, dest: str) -> None:
        raise NotImplementedError

    def copy_file(self, src: str, dest: str) -> None:
        raise NotImplementedError

    def open_output_stream(self, path: str, metadata: dict[str, str] | None = None) -> Any:  # noqa: ANN401
        raise NotImplementedError

    def open_append_stream(self, path: str, metadata: dict[str, str] | None = None) -> Any:  # noqa: ANN401
        raise NotImplementedError


def checksums(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def run(silver: Path, system: str, out: Path, spool_dir: Path | None) -> dict[str, Any]:
    handler = CountingHandler()
    lake = Location(root=silver.as_posix(), filesystem=pafs.PyFileSystem(handler))
    started = time.monotonic()
    runs = mart.build(lake, only=system, spool_dir=spool_dir)
    elapsed = time.monotonic() - started
    slugs = {r.hospital_slug for r in runs}
    mart.write(storage.local(out), mart.tables(runs), systems=slugs)
    counts = dict(handler.counts)
    # What ADLS bills as read operations: every GET and every properties call.
    counts["billed_reads"] = counts.get("read", 0) + counts.get("open", 0) + counts.get("info", 0)
    counts["billed_listings"] = counts.get("list", 0)
    return {"seconds": round(elapsed, 1), **counts}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--silver", type=Path, required=True)
    parser.add_argument("--system", required=True)
    parser.add_argument("--out", type=Path, default=Path(tempfile.mkdtemp(prefix="mart-reads-")))
    args = parser.parse_args(argv)

    direct = run(args.silver, args.system, args.out / "direct", None)
    print("direct:", json.dumps(direct))
    spooled = run(args.silver, args.system, args.out / "spooled", args.out / "spool")
    print("spooled:", json.dumps(spooled))

    a, b = checksums(args.out / "direct"), checksums(args.out / "spooled")
    same = a == b
    print(f"gold files: {len(a)} direct, {len(b)} spooled; byte-identical: {same}")
    for name in sorted(set(a) | set(b)):
        mark = "same" if a.get(name) == b.get(name) else "DIFFERENT"
        print(f"  {mark:9} {a.get(name, '-')[:16]}  {name}")
    if direct["billed_reads"]:
        cut = 1 - spooled["billed_reads"] / direct["billed_reads"]
        before, after = direct["billed_reads"], spooled["billed_reads"]
        print(f"billed reads: {before:,} -> {after:,} ({cut:.1%} fewer)")
    return 0 if same else 1


if __name__ == "__main__":
    sys.exit(main())
