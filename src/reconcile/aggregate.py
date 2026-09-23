"""Exact per-group medians, without a digest per group.

Every shard load aggregated rates with Arrow's ``approximate_median``. That
keeps a t-digest per group, and the digests are allocated outside Arrow's
memory pool -- so ``max_memory()`` never saw them. Measured in the container
on NYU Langone's shard 1: the scan held 628 MiB of RSS, and the aggregate over
586,207 groups took it to 6,269 MiB, with Arrow's pool peaking at 1,598. About
ten kilobytes of digest per group, for groups that mostly hold one or two rates.
That step is what killed NYU Langone and Montefiore at the 8 GiB ceiling.

The replacement collects each group's values as a list -- the values
themselves, a few megabytes for the same shard -- and reads the middle of
each. Memory is proportional to rows rather than to groups times a
digest, and the median is exact rather than approximate, which is a small
change to some published rates and is recorded where the rebuild is.
"""

from __future__ import annotations

from collections.abc import Sequence

import pyarrow as pa

#: Groups converted to Python per slice. Bounds the lists alive at once.
MEDIAN_SLICE = 50_000


def list_medians(lists: pa.ChunkedArray | pa.Array) -> pa.Array:
    """The median of each list, ignoring nulls; null for a list with no values.

    Plain Python over a slice of groups at a time: the groups are small -- most
    hold one or two rates -- so a sort per group is cheap, and the slice bounds
    how many are alive at once. It avoids a numpy dependency whose stubs the
    type checker cannot read under this project's Python target.
    """
    out: list[float | None] = []
    for start in range(0, len(lists), MEDIAN_SLICE):
        for values in lists.slice(start, MEDIAN_SLICE).to_pylist():
            clean = sorted(v for v in values or () if v is not None)
            n = len(clean)
            out.append((clean[(n - 1) // 2] + clean[n // 2]) / 2 if n else None)
    return pa.array(out, type=pa.float64())


def median_by_group(
    table: pa.Table,
    keys: Sequence[str],
    value: str,
    extra: Sequence[tuple[str, str]] = (),
) -> pa.Table:
    """Group ``table`` by ``keys``: exact ``<value>_median``, ``<value>_count``, and ``extra``.

    The drop-in for ``group_by(keys).aggregate([(value, "approximate_median"),
    (value, "count"), *extra])``, with the median column renamed to say what it
    now is.
    """
    grouped = table.group_by(list(keys)).aggregate([(value, "list"), (value, "count"), *extra])
    medians = list_medians(grouped.column(f"{value}_list"))
    return grouped.drop_columns([f"{value}_list"]).append_column(f"{value}_median", medians)


__all__ = ["list_medians", "median_by_group"]
