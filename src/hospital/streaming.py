"""Byte-stream decoding for hospital MRFs.

Files arrive as plain CSV/JSON, zipped, or gzipped, and the compressed size hides
the real one: a 2.8 MB zip in this dataset expands to 212 MB. Nothing here holds
a whole file in memory. The HTTP body is consumed in chunks and inflated on the
fly, with a hard cap on *decoded* bytes so a pass over a multi-GB file stops
early instead of pulling all of it down.

Zip members are inflated from the local file header rather than the central
directory, which lives at the end of the file and would force a full download.
"""

from __future__ import annotations

import io
import zlib
from collections.abc import Iterable, Iterator

ZIP_MAGIC = b"PK\x03\x04"
GZIP_MAGIC = b"\x1f\x8b"

_ZIP_LOCAL_HEADER_LEN = 30
_METHOD_STORED = 0
_METHOD_DEFLATE = 8


class UnsupportedContainer(Exception):
    """Raised for a container we cannot inflate from the front of the stream."""


class _ChunkReader(io.RawIOBase):
    """A read-only binary file object over an iterator of byte chunks."""

    def __init__(self, chunks: Iterable[bytes]) -> None:
        self._chunks = iter(chunks)
        self._buf = bytearray()
        self._eof = False

    def readable(self) -> bool:
        return True

    def _fill(self, want: int) -> None:
        while len(self._buf) < want and not self._eof:
            try:
                self._buf += next(self._chunks)
            except StopIteration:
                self._eof = True

    def readinto(self, buffer: memoryview) -> int:  # type: ignore[override]
        want = len(buffer)
        self._fill(want)
        take = min(want, len(self._buf))
        buffer[:take] = self._buf[:take]
        del self._buf[:take]
        return take

    def read_exactly(self, size: int) -> bytes:
        self._fill(size)
        data = bytes(self._buf[:size])
        del self._buf[:size]
        return data


class MrfStream:
    """Decode an MRF body, transparently handling zip and gzip containers.

    Iterating yields decoded chunks. ``truncated`` becomes True once the decoded
    byte cap is hit, which tells a caller that any totals it computed are a
    sample rather than the whole file.
    """

    def __init__(self, chunks: Iterable[bytes], max_bytes: int | None = None) -> None:
        self._raw = _ChunkReader(chunks)
        self.max_bytes = max_bytes
        self.truncated = False
        self.decoded_bytes = 0
        self._decompressor: zlib._Decompress | None = None
        self._prefix = b""
        #: For a STORED zip member, how many bytes belong to it. Without this we
        #: would stream the central directory out as if it were file content.
        self._stored_remaining: int | None = None
        self.container = self._detect()

    def _detect(self) -> str:
        magic = self._raw.read_exactly(4)
        if magic.startswith(ZIP_MAGIC):
            header = magic + self._raw.read_exactly(_ZIP_LOCAL_HEADER_LEN - 4)
            flags = int.from_bytes(header[6:8], "little")
            method = int.from_bytes(header[8:10], "little")
            uncompressed_size = int.from_bytes(header[22:26], "little")
            name_len = int.from_bytes(header[26:28], "little")
            extra_len = int.from_bytes(header[28:30], "little")
            self._raw.read_exactly(name_len + extra_len)
            if method == _METHOD_DEFLATE:
                self._decompressor = zlib.decompressobj(-zlib.MAX_WBITS)
            elif method == _METHOD_STORED:
                # Bit 3 means the size is only known from the trailing data
                # descriptor, in which case we cannot bound the member up front.
                self._stored_remaining = None if flags & 0x08 else uncompressed_size
            else:
                raise UnsupportedContainer(f"zip compression method {method}")
            return "zip"
        if magic.startswith(GZIP_MAGIC):
            self._decompressor = zlib.decompressobj(zlib.MAX_WBITS | 16)
            self._prefix = magic
            return "gzip"
        self._prefix = magic
        return "plain"

    def __iter__(self) -> Iterator[bytes]:
        pending = self._prefix
        self._prefix = b""
        while True:
            if pending:
                chunk, pending = pending, b""
            else:
                chunk = self._raw.read(1 << 18)
                if not chunk:
                    break
            data = self._decompressor.decompress(chunk) if self._decompressor else chunk
            if self._stored_remaining is not None:
                data = data[: self._stored_remaining]
                self._stored_remaining -= len(data)
            if not data:
                if self._stored_remaining == 0:
                    return
                continue
            if self.max_bytes is not None and self.decoded_bytes + len(data) >= self.max_bytes:
                keep = self.max_bytes - self.decoded_bytes
                self.decoded_bytes += keep
                self.truncated = True
                if keep:
                    yield data[:keep]
                return
            self.decoded_bytes += len(data)
            yield data

    def reader(self) -> io.BufferedReader:
        """A buffered file object over the decoded bytes, for csv/ijson."""
        return io.BufferedReader(_ChunkReader(self))
