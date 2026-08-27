"""Resolve ``<hospital-domain>/cms-hpt.txt`` across the target hospital list.

CMS requires the file at the *root* of the hospital's website domain. In practice
health systems host their patient-facing site on a subdomain, redirect, or serve
the file only over www, so a miss here is a data point about that hospital's
compliance rather than a crawler bug. Failures are recorded, never raised.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from discovery.cms_hpt import CmsHptDocument, parse_cms_hpt

CMS_HPT_FILENAME = "cms-hpt.txt"

#: Reason codes for a discovery miss. These distinguish "the hospital did not
#: publish" from "we could not reach it", which are very different findings and
#: must not be collapsed into one bucket.
REASON_BLOCKED = "blocked"  # WAF/bot-protection challenge; file may well exist
REASON_HTTP = "http-error"  # genuine 404/5xx from the origin
REASON_SOFT_404 = "soft-404"  # 200 OK, but the body is an HTML page
REASON_NO_MRF = "no-mrf-url"  # parseable file with no mrf-url in it
REASON_TRANSPORT = "transport-error"  # DNS, TLS, timeout


@dataclass(frozen=True)
class DiscoveryResult:
    domain: str
    url: str
    status: int | None = None
    document: CmsHptDocument | None = None
    mrf_urls: tuple[str, ...] = ()
    elapsed_ms: int = 0
    error: str | None = None
    #: Raw response body, kept so a live crawl can be captured as a test fixture.
    raw_text: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.mrf_urls)


def cms_hpt_url(domain: str) -> str:
    """Normalise a hospital domain to its canonical ``cms-hpt.txt`` URL."""
    cleaned = domain.strip().rstrip("/")
    if "://" not in cleaned:
        cleaned = f"https://{cleaned}"
    parsed = urlsplit(cleaned)
    return urlunsplit((parsed.scheme or "https", parsed.netloc, f"/{CMS_HPT_FILENAME}", "", ""))


async def fetch_cms_hpt(client: httpx.AsyncClient, domain: str) -> DiscoveryResult:
    """Fetch and parse one hospital's ``cms-hpt.txt``."""
    url = cms_hpt_url(domain)
    started = time.monotonic()
    try:
        response = await client.get(url, follow_redirects=True)
    except httpx.HTTPError as exc:
        return DiscoveryResult(
            domain=domain,
            url=url,
            elapsed_ms=_elapsed(started),
            error=f"{REASON_TRANSPORT}: {type(exc).__name__}: {exc}",
        )

    if not response.is_success:
        # A challenge page is the WAF talking, not the hospital. Treating it as
        # non-compliance would overstate what we actually observed.
        reason = (
            REASON_BLOCKED
            if response.status_code in (401, 403, 429) and _looks_like_html(response)
            else REASON_HTTP
        )
        return DiscoveryResult(
            domain=domain,
            url=url,
            status=response.status_code,
            elapsed_ms=_elapsed(started),
            error=f"{reason}: HTTP {response.status_code}",
        )

    if _looks_like_html(response):
        # Some sites answer 200 with their homepage for any unknown path. Parsing
        # 100 KB of markup as key/value lines yields thousands of junk warnings.
        return DiscoveryResult(
            domain=domain,
            url=url,
            status=response.status_code,
            elapsed_ms=_elapsed(started),
            error=f"{REASON_SOFT_404}: served HTML, not a cms-hpt.txt file",
            raw_text=response.text,
        )

    document = parse_cms_hpt(response.text)
    # MRF links are sometimes relative to the page that served them. Dedupe:
    # multi-campus files routinely repeat one URL across every location block.
    mrf_urls = _dedupe(urljoin(str(response.url), raw) for raw in document.mrf_urls)
    error = None if mrf_urls else f"{REASON_NO_MRF}: no mrf-url found in cms-hpt.txt"
    return DiscoveryResult(
        domain=domain,
        url=url,
        status=response.status_code,
        document=document,
        mrf_urls=mrf_urls,
        elapsed_ms=_elapsed(started),
        error=error,
        raw_text=response.text,
    )


async def discover_many(
    client: httpx.AsyncClient,
    domains: Iterable[str],
    concurrency: int = 6,
) -> list[DiscoveryResult]:
    semaphore = asyncio.Semaphore(concurrency)

    async def bounded(domain: str) -> DiscoveryResult:
        async with semaphore:
            return await fetch_cms_hpt(client, domain)

    targets: Sequence[str] = list(domains)
    return list(await asyncio.gather(*(bounded(domain) for domain in targets)))


def _looks_like_html(response: httpx.Response) -> bool:
    if response.headers.get("content-type", "").split(";")[0].strip() == "text/html":
        return True
    return response.text.lstrip()[:200].lower().startswith(("<!doctype html", "<html"))


def _dedupe(urls: Iterable[str]) -> tuple[str, ...]:
    """Order-preserving de-duplication."""
    return tuple(dict.fromkeys(urls))


def _elapsed(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
