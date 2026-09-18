"""Whether the mart may run: did the drift check on the same layers pass?

The mart reads bronze and both silver layers and writes gold from them. If those
layers have drifted since they were published, the mart will compute a full,
plausible gold from the wrong data -- and gold is what the report, the page and
any human reading it will then quote. Running it after a failed drift check is
the most expensive way to be wrong here.

**The verdict travels through the lake, not through an API.** Stage 1 writes its
result to ``_meta``; stage 2 reads it. That needs no new role assignment, no
Log Analytics query permission, and no second job able to interrogate the first
-- and it puts the dependency in the data, where the manifests it is about
already live.

**A missing verdict is not a failed one.** On a lake that has never run stage 1
there is nothing to read, and refusing then would make this impossible to
deploy. So an absent or stale verdict is reported loudly and the run proceeds;
only an explicit failure stops it. That distinction is the whole design: an
unknown reported as a refusal is as much a lie as a failure reported as a pass.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from storage import Location

#: Where stage 1 leaves its verdict.
STATUS_PATH = ("_meta", "last_manifest_run.json")

#: Beyond this, a verdict is too old to be evidence about the current lake.
#: Three days rather than one: the manifest job runs monthly, and a window
#: shorter than the gap between runs would make every mart run "stale", which
#: is a warning that means nothing.
STALE_AFTER = timedelta(days=3)


@dataclass(frozen=True)
class Verdict:
    """What stage 1 last concluded, and whether stage 2 may act on it."""

    state: str
    detail: str
    checked_at: str = ""
    layers: dict[str, bool] | None = None

    @property
    def may_run(self) -> bool:
        """Only an explicit failure stops the mart."""
        return self.state != "failed"

    @property
    def is_known(self) -> bool:
        return self.state in {"passed", "failed"}


def write_status(
    lake: Location,
    layers: dict[str, bool],
    *,
    build_sha: str = "",
) -> str:
    """Record stage 1's per-layer verdict where stage 2 can find it."""
    target = lake.child(*STATUS_PATH)
    lake.filesystem.create_dir(lake.child(STATUS_PATH[0]).root, recursive=True)
    body = {
        "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "build_sha": build_sha,
        "layers": layers,
        "ok": all(layers.values()) and bool(layers),
    }
    with lake.filesystem.open_output_stream(target.root) as handle:
        handle.write((json.dumps(body, indent=1) + "\n").encode("utf-8"))
    return target.root


def read_status(lake: Location) -> dict[str, Any] | None:
    """Stage 1's verdict, or ``None`` when there is not one to read."""
    try:
        with lake.filesystem.open_input_stream(lake.child(*STATUS_PATH).root) as handle:
            parsed: dict[str, Any] = json.loads(handle.readall().decode("utf-8-sig"))
        return parsed
    except Exception:
        return None


def check(
    lake: Location,
    *,
    now: datetime | None = None,
    stale_after: timedelta = STALE_AFTER,
) -> Verdict:
    """Read the verdict and decide what it licenses.

    Four outcomes, and the two in the middle are the reason this returns a
    value rather than a boolean: ``missing`` and ``stale`` are both "we do not
    know", which is a different thing from "the layers are fine" and a
    different thing again from "the layers drifted".
    """
    status = read_status(lake)
    if status is None:
        return Verdict("missing", f"no {'/'.join(STATUS_PATH)}; stage 1 has not run here")

    layers = {str(k): bool(v) for k, v in (status.get("layers") or {}).items()}
    checked_at = str(status.get("checked_at") or "")

    if not layers:
        return Verdict("missing", "verdict records no layers", checked_at=checked_at)

    failed = sorted(name for name, ok in layers.items() if not ok)
    if failed:
        return Verdict(
            "failed",
            f"stage 1 found drift in {', '.join(failed)}; gold built from these would be "
            "wrong and would be quoted as if it were not",
            checked_at=checked_at,
            layers=layers,
        )

    moment = now or datetime.now(UTC)
    try:
        when = datetime.fromisoformat(checked_at)
    except ValueError:
        return Verdict("stale", f"unreadable checked_at {checked_at!r}", layers=layers)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    age = moment - when
    if age > stale_after:
        return Verdict(
            "stale",
            f"last checked {age.days} days ago, beyond {stale_after.days}",
            checked_at=checked_at,
            layers=layers,
        )

    return Verdict(
        "passed", "all layers matched their manifests", checked_at=checked_at, layers=layers
    )


__all__ = ["STALE_AFTER", "STATUS_PATH", "Verdict", "check", "read_status", "write_status"]
