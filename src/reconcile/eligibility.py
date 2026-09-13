"""Which systems may have an unstated billing class read as ``facility``.

The answer is a fact about the lake, so it is computed from the lake rather than
listed — a hardcoded set stops being true the moment the lake gains a system,
and it gains one roughly every time someone runs the ingest.

Computing it costs a projection of two columns over 156 million rows. That is
seconds, which is fine once and wasteful on every invocation, so the result is
cached and keyed to the lake it was computed from.

**The key is a fingerprint of the file paths, not a timestamp.** Paths in this
lake carry the partition values and a per-batch id
(``vintage=2026-04/28e5f4b6-20260908T053600970201-...parquet``), so a re-ingest
produces different names and the fingerprint moves. Listing them reads metadata
only — 70 files in under 10 ms — so the check is far cheaper than the scan it
guards. What it will not notice is a file rewritten in place under its existing
name; nothing in this pipeline does that, and ``recompute=True`` exists for when
something does.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pyarrow.dataset as ds

#: Code systems a hospital and a payer both publish. The professional check is
#: scoped to these because a chargemaster row carries no billing class at all
#: and would make every system look facility-only.
SHARED_CODE_TYPES = ("CPT", "HCPCS", "MS-DRG")

CACHE_VERSION = 1


@dataclass(frozen=True)
class SystemEligibility:
    """One system's answer, with the evidence behind it."""

    system: str
    professional_rows: int
    eligible: bool


@dataclass
class EligibilityCache:
    """The whole answer, keyed to the lake state that produced it."""

    vintage: str
    computed_at: str = ""
    cache_version: int = CACHE_VERSION
    systems: list[SystemEligibility] = field(default_factory=list)

    @property
    def eligible(self) -> frozenset[str]:
        return frozenset(s.system for s in self.systems if s.eligible)

    def to_json(self) -> str:
        return json.dumps(
            {
                "cache_version": self.cache_version,
                "vintage": self.vintage,
                "computed_at": self.computed_at,
                "systems": [asdict(s) for s in self.systems],
            },
            indent=1,
        )

    @classmethod
    def from_json(cls, text: str) -> EligibilityCache:
        raw = json.loads(text)
        return cls(
            vintage=str(raw.get("vintage", "")),
            computed_at=str(raw.get("computed_at", "")),
            cache_version=int(raw.get("cache_version", 0)),
            systems=[SystemEligibility(**s) for s in raw.get("systems", [])],
        )


def lake_vintage(dataset: ds.Dataset) -> str:
    """A short, cheap identity for the lake's current contents.

    Metadata only: no row is read. Two lakes with the same files have the same
    vintage, and any add, removal or re-partition changes it.
    """
    digest = hashlib.sha256()
    for path in sorted(str(p) for p in dataset.files):
        # Normalised so a Windows path and its POSIX form agree, which matters
        # once the same lake is read through a cloud filesystem.
        digest.update(path.replace("\\", "/").encode())
        digest.update(b"\n")
    return digest.hexdigest()[:16]


def compute(dataset: ds.Dataset) -> EligibilityCache:
    """Scan the lake and answer for every system in it."""
    table = dataset.to_table(
        columns=["hospital", "billing_class"],
        filter=ds.field("code_type").isin(list(SHARED_CODE_TYPES)),
    )
    counts: dict[str, int] = {}
    for name, billing_class in zip(
        table.column("hospital").to_pylist(),
        table.column("billing_class").to_pylist(),
        strict=True,
    ):
        if not name:
            continue
        professional = (billing_class or "").strip().casefold() == "professional"
        counts[name] = counts.get(name, 0) + (1 if professional else 0)
    return EligibilityCache(
        vintage=lake_vintage(dataset),
        computed_at=datetime.now(UTC).isoformat(timespec="seconds"),
        systems=[
            SystemEligibility(system=name, professional_rows=n, eligible=n == 0)
            for name, n in sorted(counts.items())
        ],
    )


def default_cache_path(root: Path) -> Path:
    return root / "_meta" / "facility_eligibility.json"


def facility_only_hospitals(
    dataset: ds.Dataset,
    *,
    cache_path: Path | None = None,
    recompute: bool = False,
) -> frozenset[str]:
    """Systems publishing no professional rate, from cache when it is still valid.

    A cache whose vintage does not match the lake is ignored rather than
    refreshed in place, because a stale answer here is worse than a slow one: it
    would apply the facility assumption to a system that has since started
    publishing professional rates, which is the one case the assumption is
    plainly wrong for.
    """
    vintage = lake_vintage(dataset)
    if cache_path and not recompute and cache_path.exists():
        try:
            cached = EligibilityCache.from_json(cache_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, TypeError, ValueError):
            cached = None
        if cached and cached.cache_version == CACHE_VERSION and cached.vintage == vintage:
            return cached.eligible

    answer = compute(dataset)
    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(answer.to_json(), encoding="utf-8")
    return answer.eligible


__all__ = [
    "CACHE_VERSION",
    "SHARED_CODE_TYPES",
    "EligibilityCache",
    "SystemEligibility",
    "compute",
    "default_cache_path",
    "facility_only_hospitals",
    "lake_vintage",
]
