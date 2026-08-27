"""HEAD-first size probe for machine-readable files.

Phase 1 requires recording ``Content-Length`` *before* downloading anything: a
hospital MRF runs to hundreds of MB and a payer TiC file can exceed 1 TB, so file
size is a planning input, not something to discover mid-download.

Not every host answers HEAD. CDNs in front of MRFs commonly return 403, 405, or a
200 with no ``Content-Length``. When that happens, fall back to a one-byte ranged
GET and read the true total out of ``Content-Range``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace

import httpx

#: Statuses that mean "this host dislikes HEAD", not "this file is missing".
_RANGE_FALLBACK_STATUSES = frozenset({403, 405, 501})


@dataclass(frozen=True)
class ProbeResult:
    url: str
    method: str
    status: int | None = None
    final_url: str | None = None
    content_length: int | None = None
    content_type: str | None = None
    last_modified: str | None = None
    etag: str | None = None
    elapsed_ms: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.status is not None and 200 <= self.status < 300


async def probe_size(client: httpx.AsyncClient, url: str) -> ProbeResult:
    """Return size and cache metadata for ``url`` without downloading it."""
    started = time.monotonic()
    try:
        response = await client.head(url, follow_redirects=True)
    except httpx.HTTPError as exc:
        return ProbeResult(
            url=url, method="HEAD", elapsed_ms=_elapsed(started), error=_describe(exc)
        )

    needs_fallback = response.status_code in _RANGE_FALLBACK_STATUSES or (
        response.is_success and response.headers.get("content-length") is None
    )
    if needs_fallback:
        return await _probe_with_range(client, url, started)
    return _result_from(url, "HEAD", response, started)


async def probe_many(
    client: httpx.AsyncClient,
    urls: Iterable[str],
    concurrency: int = 6,
) -> list[ProbeResult]:
    """Probe many URLs, bounded so we stay polite to any one host."""
    semaphore = asyncio.Semaphore(concurrency)

    async def bounded(url: str) -> ProbeResult:
        async with semaphore:
            return await probe_size(client, url)

    targets: Sequence[str] = list(urls)
    return list(await asyncio.gather(*(bounded(url) for url in targets)))


async def _probe_with_range(
    client: httpx.AsyncClient,
    url: str,
    started: float,
) -> ProbeResult:
    try:
        response = await client.get(
            url,
            headers={"Range": "bytes=0-0"},
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        return ProbeResult(
            url=url,
            method="GET-range",
            elapsed_ms=_elapsed(started),
            error=_describe(exc),
        )

    result = _result_from(url, "GET-range", response, started)
    # Content-Length here describes the single byte we asked for, so the only
    # trustworthy total is the one in Content-Range. If it is absent, report
    # unknown rather than 1.
    return replace(
        result, content_length=_parse_content_range(response.headers.get("content-range"))
    )


def _result_from(url: str, method: str, response: httpx.Response, started: float) -> ProbeResult:
    raw_length = response.headers.get("content-length")
    return ProbeResult(
        url=url,
        method=method,
        status=response.status_code,
        final_url=str(response.url),
        content_length=int(raw_length) if raw_length and raw_length.isdigit() else None,
        content_type=response.headers.get("content-type"),
        last_modified=response.headers.get("last-modified"),
        etag=response.headers.get("etag"),
        elapsed_ms=_elapsed(started),
    )


def _parse_content_range(value: str | None) -> int | None:
    """Extract the total size from a ``bytes 0-0/12345`` header."""
    if not value:
        return None
    total = value.partition("/")[2].strip()
    return int(total) if total.isdigit() else None


def _elapsed(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _describe(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"
