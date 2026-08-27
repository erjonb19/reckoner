import httpx
import respx

from discovery.probe import ProbeResult, probe_many, probe_size

URL = "https://example.org/mrf.json"


@respx.mock
async def test_head_reports_size_and_cache_headers():
    respx.head(URL).mock(
        return_value=httpx.Response(
            200,
            headers={
                "Content-Length": "12345678",
                "Content-Type": "application/json",
                "Last-Modified": "Wed, 01 Jan 2025 00:00:00 GMT",
                "ETag": '"abc123"',
            },
        )
    )

    async with httpx.AsyncClient() as client:
        result = await probe_size(client, URL)

    assert result.ok
    assert result.method == "HEAD"
    assert result.content_length == 12345678
    assert result.content_type == "application/json"
    assert result.etag == '"abc123"'


@respx.mock
async def test_head_rejected_falls_back_to_ranged_get():
    """CDNs commonly answer 405 to HEAD; Content-Range still yields the total."""
    respx.head(URL).mock(return_value=httpx.Response(405))
    respx.get(URL).mock(
        return_value=httpx.Response(
            206,
            headers={"Content-Range": "bytes 0-0/98765432", "Content-Length": "1"},
            content=b"x",
        )
    )

    async with httpx.AsyncClient() as client:
        result = await probe_size(client, URL)

    assert result.method == "GET-range"
    assert result.content_length == 98765432


@respx.mock
async def test_head_without_content_length_falls_back():
    respx.head(URL).mock(return_value=httpx.Response(200))
    respx.get(URL).mock(
        return_value=httpx.Response(206, headers={"Content-Range": "bytes 0-0/4096"}, content=b"x")
    )

    async with httpx.AsyncClient() as client:
        result = await probe_size(client, URL)

    assert result.content_length == 4096


@respx.mock
async def test_range_fallback_without_content_range_reports_unknown_not_one_byte():
    respx.head(URL).mock(return_value=httpx.Response(403))
    respx.get(URL).mock(
        return_value=httpx.Response(200, headers={"Content-Length": "1"}, content=b"x")
    )

    async with httpx.AsyncClient() as client:
        result = await probe_size(client, URL)

    assert result.content_length is None


@respx.mock
async def test_404_is_reported_not_probed_again():
    route = respx.head(URL).mock(return_value=httpx.Response(404))

    async with httpx.AsyncClient() as client:
        result = await probe_size(client, URL)

    assert route.call_count == 1
    assert not result.ok
    assert result.status == 404


@respx.mock
async def test_transport_error_is_captured_not_raised():
    respx.head(URL).mock(side_effect=httpx.ConnectError("dns failure"))

    async with httpx.AsyncClient() as client:
        result = await probe_size(client, URL)

    assert isinstance(result, ProbeResult)
    assert not result.ok
    assert "ConnectError" in result.error


@respx.mock
async def test_probe_many_preserves_input_order():
    for i in range(3):
        respx.head(f"https://example.org/{i}.json").mock(
            return_value=httpx.Response(200, headers={"Content-Length": str(i + 1)})
        )

    urls = [f"https://example.org/{i}.json" for i in range(3)]
    async with httpx.AsyncClient() as client:
        results = await probe_many(client, urls, concurrency=2)

    assert [r.url for r in results] == urls
    assert [r.content_length for r in results] == [1, 2, 3]
