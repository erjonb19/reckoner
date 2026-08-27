import httpx
import pytest
import respx

from discovery.crawler import cms_hpt_url, discover_many, fetch_cms_hpt

SAMPLE = (
    "location-name: Example Hospital\n"
    "mrf-url: https://cdn.example.org/123_example_standardcharges.json\n"
)


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("example.org", "https://example.org/cms-hpt.txt"),
        ("example.org/", "https://example.org/cms-hpt.txt"),
        ("https://example.org", "https://example.org/cms-hpt.txt"),
        ("https://www.example.org/patients/pricing", "https://www.example.org/cms-hpt.txt"),
        ("http://example.org", "http://example.org/cms-hpt.txt"),
        ("  example.org  ", "https://example.org/cms-hpt.txt"),
    ],
)
def test_cms_hpt_url_normalisation(given, expected):
    assert cms_hpt_url(given) == expected


@respx.mock
async def test_fetch_returns_absolute_mrf_urls():
    respx.get("https://example.org/cms-hpt.txt").mock(return_value=httpx.Response(200, text=SAMPLE))

    async with httpx.AsyncClient() as client:
        result = await fetch_cms_hpt(client, "example.org")

    assert result.ok
    assert result.status == 200
    assert result.mrf_urls == ("https://cdn.example.org/123_example_standardcharges.json",)


@respx.mock
async def test_relative_mrf_url_resolves_against_final_url():
    respx.get("https://example.org/cms-hpt.txt").mock(
        return_value=httpx.Response(200, text="mrf-url: /files/mrf.json\n")
    )

    async with httpx.AsyncClient() as client:
        result = await fetch_cms_hpt(client, "example.org")

    assert result.mrf_urls == ("https://example.org/files/mrf.json",)


@respx.mock
async def test_redirect_is_followed_before_resolving_relative_urls():
    respx.get("https://example.org/cms-hpt.txt").mock(
        return_value=httpx.Response(
            301, headers={"Location": "https://www.example.org/cms-hpt.txt"}
        )
    )
    respx.get("https://www.example.org/cms-hpt.txt").mock(
        return_value=httpx.Response(200, text="mrf-url: files/mrf.json\n")
    )

    async with httpx.AsyncClient() as client:
        result = await fetch_cms_hpt(client, "example.org")

    assert result.mrf_urls == ("https://www.example.org/files/mrf.json",)


@respx.mock
async def test_missing_file_is_recorded_not_raised():
    """A 404 is a compliance data point about that hospital, not a crawler failure."""
    respx.get("https://example.org/cms-hpt.txt").mock(return_value=httpx.Response(404))

    async with httpx.AsyncClient() as client:
        result = await fetch_cms_hpt(client, "example.org")

    assert not result.ok
    assert result.status == 404
    assert result.error == "http-error: HTTP 404"


@respx.mock
async def test_present_but_unusable_file_is_flagged():
    respx.get("https://example.org/cms-hpt.txt").mock(
        return_value=httpx.Response(200, text="see our pricing page\n")
    )

    async with httpx.AsyncClient() as client:
        result = await fetch_cms_hpt(client, "example.org")

    assert not result.ok
    assert result.error.startswith("no-mrf-url")
    assert result.document is not None and result.document.warnings


@respx.mock
async def test_waf_challenge_is_distinguished_from_a_real_404():
    """403 + HTML is bot protection, not evidence the hospital failed to publish."""
    respx.get("https://example.org/cms-hpt.txt").mock(
        return_value=httpx.Response(
            403,
            headers={"Content-Type": "text/html; charset=UTF-8"},
            text="<!DOCTYPE html><html><head><title>Just a moment...</title></head></html>",
        )
    )

    async with httpx.AsyncClient() as client:
        result = await fetch_cms_hpt(client, "example.org")

    assert result.error.startswith("blocked")


@respx.mock
async def test_soft_404_html_body_is_not_parsed_as_key_values():
    """Some sites answer 200 with their homepage; do not emit thousands of warnings."""
    homepage = '<!doctype html>\n<html lang="en">\n<head>\n<title>A Hospital</title>\n' + (
        "<meta name='x' content='y'>\n" * 200
    )
    respx.get("https://example.org/cms-hpt.txt").mock(
        return_value=httpx.Response(200, headers={"Content-Type": "text/html"}, text=homepage)
    )

    async with httpx.AsyncClient() as client:
        result = await fetch_cms_hpt(client, "example.org")

    assert not result.ok
    assert result.error.startswith("soft-404")
    assert result.document is None
    assert result.raw_text == homepage


@respx.mock
async def test_repeated_mrf_url_across_locations_is_deduped():
    """Multi-campus files routinely point every location at one MRF."""
    block = "location-name: {name}\nmrf-url: https://cdn.example.org/one_standardcharges.json\n"
    respx.get("https://example.org/cms-hpt.txt").mock(
        return_value=httpx.Response(
            200,
            text="\n".join(block.format(name=n) for n in ("Campus A", "Campus B", "Campus C")),
        )
    )

    async with httpx.AsyncClient() as client:
        result = await fetch_cms_hpt(client, "example.org")

    assert len(result.document.records) == 3
    assert result.mrf_urls == ("https://cdn.example.org/one_standardcharges.json",)


@respx.mock
async def test_connect_error_is_captured():
    respx.get("https://example.org/cms-hpt.txt").mock(side_effect=httpx.ConnectError("boom"))

    async with httpx.AsyncClient() as client:
        result = await fetch_cms_hpt(client, "example.org")

    assert not result.ok
    assert "ConnectError" in result.error


@respx.mock
async def test_discover_many_preserves_order_and_isolates_failures():
    respx.get("https://good.org/cms-hpt.txt").mock(return_value=httpx.Response(200, text=SAMPLE))
    respx.get("https://bad.org/cms-hpt.txt").mock(side_effect=httpx.ConnectError("boom"))

    async with httpx.AsyncClient() as client:
        results = await discover_many(client, ["good.org", "bad.org"], concurrency=2)

    assert [r.domain for r in results] == ["good.org", "bad.org"]
    assert results[0].ok
    assert not results[1].ok
