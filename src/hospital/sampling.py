"""Unbiased sampling of a large uncompressed MRF via HTTP range requests.

A capped read from the front of a file is not a sample, it is a prefix. These
files are ordered -- by code, by payer, or by however the source system emitted
them -- so a prefix can misstate the methodology mix badly: one NYC Health +
Hospitals file reads 82% dollar-denominated over its first 48 MB and 3.6% over
the whole thing.

Where the host honours Range requests on an uncompressed file, reading N windows
spread evenly across the byte range costs a fraction of a full download and is
not biased toward either end. Compressed files cannot be seeked into, so those
still need a full streaming pass.

Window edges land mid-record, so the first and last fragment of each window are
discarded; a window therefore measures slightly fewer rows than it spans.
"""

from __future__ import annotations

import csv
from collections.abc import Callable, Iterable

from hospital.profile import MrfProfile, csv_row_recorder, find_header

#: Fetch bytes [start, end] inclusive, as an HTTP Range request would.
RangeFetcher = Callable[[int, int], bytes]

DEFAULT_WINDOWS = 16
DEFAULT_WINDOW_BYTES = 2 * 1024 * 1024


def window_starts(
    total_bytes: int,
    body_start: int = 0,
    windows: int = DEFAULT_WINDOWS,
    window_bytes: int = DEFAULT_WINDOW_BYTES,
) -> list[int]:
    """Evenly spaced window offsets covering the body of the file.

    Returns a single window when the file is small enough that sampling would
    read most of it anyway -- at that point a full read is simpler and exact.
    """
    body = max(total_bytes - body_start, 0)
    if windows < 1 or body <= 0:
        return []
    if body <= windows * window_bytes:
        return [body_start]
    step = body // windows
    return [body_start + index * step for index in range(windows)]


def parse_window(blob: bytes, at_body_start: bool) -> list[str]:
    """Split a window into whole lines, dropping the truncated edges."""
    text = blob.decode("utf-8", errors="replace")
    lines = text.split("\n")
    if not at_body_start:
        lines = lines[1:]  # opening fragment belongs to the previous record
    if lines:
        lines = lines[:-1]  # closing fragment continues past the window
    return [line for line in lines if line.strip()]


def find_header_offset(head: bytes, max_rows: int = 12) -> tuple[list[str] | None, int]:
    """Find the header row and the *byte* offset of the first data row.

    Works on bytes throughout: character offsets diverge from byte offsets as
    soon as a hospital name carries a non-ASCII character, and that offset is
    what the range requests are keyed on.
    """
    offset = 0
    for index, raw_line in enumerate(head.split(b"\n")):
        if index > max_rows:
            break
        line = raw_line.decode("utf-8-sig", errors="replace")
        row = find_header(iter([next(csv.reader([line]), [])]))
        offset += len(raw_line) + 1
        if row is not None:
            return row, offset
    return None, 0


def sample_csv(
    fetch: RangeFetcher,
    total_bytes: int,
    profile: MrfProfile,
    windows: int = DEFAULT_WINDOWS,
    window_bytes: int = DEFAULT_WINDOW_BYTES,
    header_probe_bytes: int = 512 * 1024,
) -> int:
    """Profile an uncompressed CSV MRF by sampling windows across its length.

    Returns the number of bytes actually read. Rows whose field count does not
    match the header are skipped: a window edge can fall inside a quoted field
    containing a newline, and a malformed row must not be counted as a rate.
    """
    head = fetch(0, min(header_probe_bytes, total_bytes) - 1)
    header, body_start = find_header_offset(head)
    if header is None:
        profile.error = "no CMS template header row found in first 9 rows"
        return len(head)

    record, layout = csv_row_recorder(header, profile)
    profile.layout = layout

    bytes_read = len(head)
    starts = window_starts(total_bytes, body_start, windows, window_bytes)
    for start in starts:
        end = min(start + window_bytes, total_bytes) - 1
        if end < start:
            continue
        blob = fetch(start, end)
        bytes_read += len(blob)
        for row in _rows_of(parse_window(blob, at_body_start=start == body_start)):
            if len(row) == len(header):
                record(row)

    profile.bytes_scanned = bytes_read
    profile.truncated = bytes_read < total_bytes
    return bytes_read


def _rows_of(lines: Iterable[str]) -> Iterable[list[str]]:
    return csv.reader(lines)
