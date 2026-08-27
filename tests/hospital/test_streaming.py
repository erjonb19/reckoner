import gzip
import io
import zipfile

import pytest

from hospital.streaming import MrfStream, UnsupportedContainer

PAYLOAD = b"payer_name,plan_name\n" + b"Aetna,All Commercial Plans\n" * 500


def chunks(data: bytes, size: int = 64):
    return (data[i : i + size] for i in range(0, len(data), size))


def zipped(data: bytes, compress=zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compress) as zf:
        zf.writestr("standardcharges.csv", data)
    return buf.getvalue()


def test_plain_stream_passes_through():
    stream = MrfStream(chunks(PAYLOAD))
    assert stream.container == "plain"
    assert b"".join(stream) == PAYLOAD


def test_gzip_is_inflated():
    stream = MrfStream(chunks(gzip.compress(PAYLOAD)))
    assert stream.container == "gzip"
    assert b"".join(stream) == PAYLOAD


def test_deflated_zip_is_inflated_from_the_local_header():
    """The central directory is at the end of the file; we must not need it."""
    stream = MrfStream(chunks(zipped(PAYLOAD)))
    assert stream.container == "zip"
    assert b"".join(stream) == PAYLOAD


def test_stored_zip_is_passed_through():
    stream = MrfStream(chunks(zipped(PAYLOAD, zipfile.ZIP_STORED)))
    assert b"".join(stream) == PAYLOAD


def test_unsupported_zip_method_is_rejected():
    with pytest.raises(UnsupportedContainer):
        MrfStream(chunks(zipped(PAYLOAD, zipfile.ZIP_BZIP2)))


@pytest.mark.parametrize("wrap", [lambda d: d, gzip.compress, zipped])
def test_byte_cap_truncates_decoded_output(wrap):
    """The cap applies to decoded bytes, which is what a 75x zip hides."""
    stream = MrfStream(chunks(wrap(PAYLOAD)), max_bytes=100)
    body = b"".join(stream)

    assert len(body) == 100
    assert body == PAYLOAD[:100]
    assert stream.truncated
    assert stream.decoded_bytes == 100


def test_uncapped_stream_is_not_marked_truncated():
    stream = MrfStream(chunks(PAYLOAD), max_bytes=len(PAYLOAD) * 2)
    b"".join(stream)
    assert not stream.truncated


def test_reader_is_a_usable_file_object():
    stream = MrfStream(chunks(zipped(PAYLOAD)))
    reader = stream.reader()

    assert reader.peek(4)[:4] == PAYLOAD[:4]
    assert reader.read(len(PAYLOAD)) == PAYLOAD
