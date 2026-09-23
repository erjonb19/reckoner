"""Exact per-group medians, without a digest per group.

Every shard load aggregated rates with Arrow's ``approximate_median``. That
keeps a t-digest per group, and the digests are allocated outside Arrow's
memory pool -- so ``max_memory()`` never saw them. Measured in the container
on NYU Langone's shard 1: the scan held 628 MiB of RSS, and the aggregate over
586,207 groups took it to 6,269 MiB, with Arrow's pool peaking at 1,598. About
ten kilobytes of digest per group, for groups that mostly hold one or two rates.
That step is what killed NYU Langone and Montefiore at the 8 GiB ceiling.

The replacement collects each group's values as a list -- the values
themselves, a few megabytes for the same shard -- sorts them once, and reads
the middle. Memory is proportional to rows rather than to groups times a
digest, and the median is exact rather than approximate, which is a small
change to some published rates and is recorded where the rebuild is.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc


def list_medians(lists: pa.ChunkedArray | pa.Array) -> pa.Array:
    """The median of each list, ignoring nulls; null for a list with no values.

    One sort over every value, keyed by the list each came from, then two
    index lookups per list. No per-list Python work.
    """
    if isinstance(lists, pa.ChunkedArray):
        lists = lists.combine_chunks()
    groups = len(lists)
    if groups == 0:
        return pa.array([], type=pa.float64())
    values = pc.list_flatten(lists)
    parents = pc.list_parent_indices(lists)
    valid = pc.is_valid(values)
    values = pc.filter(values, valid).to_numpy(zero_copy_only=False).astype(np.float64)
    parents = pc.filter(parents, valid).to_numpy(zero_copy_only=False).astype(np.int64)

    order = np.lexsort((values, parents))
    ordered = values[order]
    counts = np.bincount(parents, minlength=groups)
    starts = np.concatenate(([0], np.cumsum(counts)[:-1]))
    present = counts > 0
    low = np.where(present, starts + (counts - 1) // 2, 0)
    high = np.where(present, starts + counts // 2, 0)
    medians = (ordered[low] + ordered[high]) / 2 if ordered.size else np.zeros(groups)
    return pa.array(medians, type=pa.float64(), mask=~present)


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
