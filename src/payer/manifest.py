"""A durable record of which payer files an analysis actually read.

Every figure this project reports rests on a directory of Parquet that changes
without announcement: files land, get re-parsed at a new vintage, or are
superseded. A number is only defensible if you can say which files produced it,
and that has so far meant trusting whatever the glob returned on the day.

So a manifest is a snapshot: one row per file, with the vintage, the row count,
and an identity cheap enough to take every run. Two snapshots can then be
diffed, which is the part that earns its keep -- it turns "the payer data
changed" into a list of exactly what changed, which is what makes a stale figure
detectable rather than merely suspected.

**What a manifest cannot tell you.** A payer file that parsed but matched no
target hospital leaves no Parquet at all, so its absence looks identical to
never having been attempted. 182 of roughly 280 Emblem files are in that state.
That fact lives upstream in ``mrf_pipeline``'s config and run logs, on the other
side of the boundary ADR 0001 draws, and inventing a state for it here would be
guessing. What a manifest *can* do is make the absence enumerable after the
fact: diff today's snapshot against yesterday's and a file that vanished is
named rather than silently missing.

Identity is the Parquet footer -- row count, row-group count, schema, byte size
-- not a hash of the bytes. The footer costs microseconds and catches a re-parse,
a truncation or a schema change, which are the changes that actually happen
here. ``--hash`` computes a real SHA-256 when a specific claim needs one; it
reads 4.3 GB and is not the default for that reason.

    python -m payer.manifest --payer-root ../mrf_pipeline/payer_parquet --out manifest.json
    python -m payer.manifest --payer-root ... --against manifest.json
    python -m payer.manifest --payer-root ... --snapshot-dir data/manifests

The last form is what the daily task runs: it diffs against the newest snapshot
already in the directory, writes a new one, and prunes. See
``scripts/snapshot_payer_manifest.ps1``. It has to run locally -- the payer
Parquet is a gitignored 4.3 GB directory in a sibling repo, so no cloud runner
can see it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from payer.contract import CONTRACT_VERSION
from payer.curated import DUPLICATE_PAYER_FILES, split_label

#: Bumped when an entry's fields change meaning, so an old snapshot on disk is
#: not silently compared against a newer one under different rules.
MANIFEST_VERSION = 1


class FileState(StrEnum):
    """Why a file is or is not part of the dataset.

    Deliberately excludes any notion of "expected but missing": nothing at this
    boundary knows what was expected. See the module docstring.
    """

    #: Present and read.
    READ = "read"
    #: Present, deliberately skipped -- row-for-row identical to another file.
    DUPLICATE = "duplicate"
    #: A ``.part`` is an open writer handle, not a short file. Never opened.
    IN_FLIGHT = "in_flight"
    #: Present but the footer could not be read.
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class ManifestEntry:
    """One payer file as it stood when the snapshot was taken."""

    stem: str
    carrier: str
    network: str
    state: FileState
    rows: int = 0
    vintage: str | None = None
    bytes: int = 0
    row_groups: int = 0
    columns: int = 0
    #: SHA-256 of the file, only when explicitly asked for.
    sha256: str | None = None

    def identity(self) -> tuple[Any, ...]:
        """What has to match for two snapshots to call this the same file.

        The hash is included only when both sides have one, so a cheap snapshot
        and an expensive one still compare on the fields they share rather than
        reporting every file as changed.
        """
        return (self.rows, self.vintage, self.bytes, self.row_groups, self.columns)


@dataclass
class Manifest:
    """Every file at the boundary at one moment."""

    root: str = ""
    taken_at: str = ""
    manifest_version: int = MANIFEST_VERSION
    contract_version: int = CONTRACT_VERSION
    entries: list[ManifestEntry] = field(default_factory=list)

    @property
    def read(self) -> list[ManifestEntry]:
        return [e for e in self.entries if e.state is FileState.READ]

    @property
    def rows(self) -> int:
        return sum(e.rows for e in self.read)

    def by_stem(self) -> dict[str, ManifestEntry]:
        return {e.stem: e for e in self.entries}

    def to_json(self) -> str:
        payload = {
            "root": self.root,
            "taken_at": self.taken_at,
            "manifest_version": self.manifest_version,
            "contract_version": self.contract_version,
            "files": len(self.entries),
            "files_read": len(self.read),
            "rows": self.rows,
            "entries": [asdict(e) for e in self.entries],
        }
        return json.dumps(payload, indent=1, default=str)

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def read_json(cls, path: Path) -> Manifest:
        raw = json.loads(path.read_text(encoding="utf-8"))
        stored = int(raw.get("manifest_version", 0))
        if stored != MANIFEST_VERSION:
            raise ValueError(
                f"{path} is manifest version {stored}, this is version {MANIFEST_VERSION}; "
                "re-take the snapshot rather than comparing across versions"
            )
        return cls(
            root=raw.get("root", ""),
            taken_at=raw.get("taken_at", ""),
            manifest_version=stored,
            contract_version=int(raw.get("contract_version", 0)),
            entries=[
                ManifestEntry(**{**e, "state": FileState(e["state"])}) for e in raw["entries"]
            ],
        )


@dataclass
class ManifestDiff:
    """What changed between two snapshots."""

    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    #: stem -> the fields that differ, as "field: before -> after".
    changed: dict[str, list[str]] = field(default_factory=dict)

    @property
    def unchanged(self) -> bool:
        return not (self.added or self.removed or self.changed)

    def summary(self) -> dict[str, Any]:
        return {
            "unchanged": self.unchanged,
            "added": self.added,
            "removed": self.removed,
            "changed": self.changed,
        }


_COMPARED = ("state", "rows", "vintage", "bytes", "row_groups", "columns", "sha256")


def diff(before: Manifest, after: Manifest) -> ManifestDiff:
    """Compare two snapshots by stem.

    A removed file is the interesting case and the reason this exists: a payer
    file that stops matching any target hospital simply stops being written, and
    without a previous snapshot its absence is indistinguishable from never
    having been parsed.
    """
    old, new = before.by_stem(), after.by_stem()
    out = ManifestDiff(
        added=sorted(set(new) - set(old)),
        removed=sorted(set(old) - set(new)),
    )
    for stem in sorted(set(old) & set(new)):
        a, b = old[stem], new[stem]
        fields = []
        for name in _COMPARED:
            was, now = getattr(a, name), getattr(b, name)
            # A hash only present on one side is not a change in the file.
            if name == "sha256" and (was is None or now is None):
                continue
            if was != now:
                fields.append(f"{name}: {was} -> {now}")
        if fields:
            out.changed[stem] = fields
    return out


def _sha256(path: Path) -> str:
    """Streamed in chunks: these files run to 35 MB and the lake to 4.3 GB."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _entry(path: Path, *, with_hash: bool) -> ManifestEntry:
    stem = path.stem
    carrier, network = split_label(stem)
    state = FileState.DUPLICATE if stem in DUPLICATE_PAYER_FILES else FileState.READ
    try:
        handle = pq.ParquetFile(path)
        meta = handle.metadata
        schema = handle.schema_arrow
        vintage = None
        if "last_updated_on" in schema.names and meta.num_row_groups:
            column = handle.read_row_group(0, columns=["last_updated_on"]).column(0)
            if column.length():
                vintage = column[0].as_py() or None
        return ManifestEntry(
            stem=stem,
            carrier=carrier,
            network=network,
            state=state,
            rows=meta.num_rows,
            vintage=vintage,
            bytes=path.stat().st_size,
            row_groups=meta.num_row_groups,
            columns=len(schema.names),
            sha256=_sha256(path) if with_hash else None,
        )
    except (OSError, pa.ArrowInvalid):
        return ManifestEntry(
            stem=stem,
            carrier=carrier,
            network=network,
            state=FileState.UNREADABLE,
            bytes=path.stat().st_size if path.exists() else 0,
        )


def build(root: Path, *, with_hash: bool = False) -> Manifest:
    """Snapshot every payer file under ``root``.

    Non-recursive, matching :func:`payer.curated.discover_payer_files`:
    ``old_npi_only/`` and ``trial_60tins/`` hold superseded output and are not
    part of the dataset, so a flat glob excludes them without naming them.
    """
    if not root.exists():
        raise FileNotFoundError(f"no payer parquet directory at {root}")

    entries = [_entry(path, with_hash=with_hash) for path in sorted(root.glob("*.parquet"))]
    for path in sorted(root.glob("*.parquet.part")):
        stem = path.name.removesuffix(".parquet.part")
        carrier, network = split_label(stem)
        entries.append(
            ManifestEntry(
                stem=stem,
                carrier=carrier,
                network=network,
                state=FileState.IN_FLIGHT,
                bytes=path.stat().st_size,
            )
        )
    entries.sort(key=lambda e: e.stem)
    return Manifest(
        root=str(root),
        taken_at=datetime.now(UTC).isoformat(timespec="seconds"),
        entries=entries,
    )


#: Snapshot filenames sort lexicographically into chronological order, because
#: the timestamp is UTC and fixed-width. That is what lets "the previous one" be
#: a sort rather than a stored pointer that can go stale.
SNAPSHOT_GLOB = "manifest-*.json"
_SNAPSHOT_STAMP = "%Y%m%dT%H%M%SZ"


def latest_snapshot(directory: Path) -> Path | None:
    """The most recent snapshot in ``directory``, or ``None`` on the first run."""
    existing = sorted(directory.glob(SNAPSHOT_GLOB))
    return existing[-1] if existing else None


def rotate(
    manifest: Manifest, directory: Path, *, keep: int = 30
) -> tuple[Path, ManifestDiff | None]:
    """Write ``manifest`` into ``directory`` and diff it against the one before.

    Returns the path written and the diff, or ``None`` for the diff on a first
    run. ``None`` rather than an empty diff because "nothing changed" and
    "nothing to compare against" are different statements, and only one of them
    is reassuring -- a scheduled job that reported the first as the second would
    be quietly useless for exactly as long as nobody checked.

    Old snapshots are pruned to ``keep``. They are a few kilobytes each, so the
    limit is about not accumulating forever rather than about space.
    """
    directory.mkdir(parents=True, exist_ok=True)
    previous = latest_snapshot(directory)
    changes = diff(Manifest.read_json(previous), manifest) if previous else None

    # Second resolution, so two snapshots inside one second would otherwise land
    # on the same name and the older would vanish into the newer.
    #
    # The counter is always present, never only on collision. With it optional,
    # "manifest-<stamp>-01.json" sorts *before* "manifest-<stamp>.json" -- "-" is
    # 0x2D and "." is 0x2E -- so the first file written in a second sorted as the
    # newest, and rotation compared against the wrong snapshot. Uniform names
    # keep the lexicographic sort chronological, which is the whole basis for
    # `latest_snapshot` being a sort rather than a stored pointer.
    stamp = datetime.now(UTC).strftime(_SNAPSHOT_STAMP)
    # One past the highest counter already used this second, rather than the
    # first free one. Pruning frees old names, so a search for a gap will happily
    # reuse "-00" after it has been deleted -- and that file then sorts oldest,
    # gets pruned again on the same call, and `rotate` returns a path that no
    # longer exists. Monotonic avoids the whole class.
    used = [
        int(candidate.stem.rsplit("-", 1)[1])
        for candidate in directory.glob(f"manifest-{stamp}-*.json")
        if candidate.stem.rsplit("-", 1)[1].isdigit()
    ]
    path = directory / f"manifest-{stamp}-{(max(used) + 1 if used else 0):02d}.json"
    manifest.write(path)

    if keep > 0:
        for stale in sorted(directory.glob(SNAPSHOT_GLOB))[:-keep]:
            stale.unlink()
    return path, changes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--payer-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, help="write the snapshot here")
    parser.add_argument("--against", type=Path, help="diff against a previous snapshot")
    parser.add_argument(
        "--hash", action="store_true", help="compute SHA-256 per file; reads every byte"
    )
    parser.add_argument(
        "--snapshot-dir",
        type=Path,
        help="take a snapshot here, diffing against the newest already present",
    )
    parser.add_argument(
        "--keep", type=int, default=30, help="snapshots to retain in --snapshot-dir"
    )
    parser.add_argument(
        "--fail-on-change",
        action="store_true",
        help="exit non-zero when the diff is non-empty; for a scheduled run that should be quiet",
    )
    args = parser.parse_args(argv)

    manifest = build(args.payer_root, with_hash=args.hash)
    carriers: dict[str, int] = {}
    for entry in manifest.read:
        carriers[entry.carrier] = carriers.get(entry.carrier, 0) + 1
    print(
        json.dumps(
            {
                "taken_at": manifest.taken_at,
                "files": len(manifest.entries),
                "read": len(manifest.read),
                "rows": manifest.rows,
                "by_state": {
                    str(s): sum(1 for e in manifest.entries if e.state is s) for s in FileState
                },
                "by_carrier": dict(sorted(carriers.items(), key=lambda kv: -kv[1])),
                "vintages": sorted({e.vintage for e in manifest.read if e.vintage}),
            },
            indent=1,
        )
    )

    if args.out:
        manifest.write(args.out)
        print(f"\nwritten to {args.out}")

    changes: ManifestDiff | None = None
    if args.snapshot_dir:
        path, changes = rotate(manifest, args.snapshot_dir, keep=args.keep)
        print(f"\nsnapshot written to {path}")
        if changes is None:
            print("no previous snapshot: nothing to compare against yet")
    elif args.against:
        changes = diff(Manifest.read_json(args.against), manifest)

    if changes is not None:
        print("\n" + json.dumps(changes.summary(), indent=1))
        if args.fail_on_change and not changes.unchanged:
            return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - entrypoint
    sys.exit(main())
