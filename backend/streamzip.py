"""Small ZIP32 streaming writer with data descriptors (no archive staging)."""

from __future__ import annotations

import os
import re
import struct
import time
import zlib
from pathlib import Path
from pathlib import PurePosixPath
from typing import AsyncIterator, Iterable, Iterator

from .runtime_limits import artifact_limit_bytes


def __getattr__(name: str) -> int:
    # Backwards compatibility for callers that read the old module constant.
    if name == "MAX_ARCHIVE":
        return artifact_limit_bytes()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _limit_label(maximum_bytes: int) -> str:
    return f"{maximum_bytes // (1024 * 1024)} MiB"


def _dos_datetime(epoch: float) -> tuple[int, int]:
    value = time.localtime(epoch)
    date = ((max(1980, value.tm_year) - 1980) << 9) | (value.tm_mon << 5) | value.tm_mday
    clock = (value.tm_hour << 11) | (value.tm_min << 5) | (value.tm_sec // 2)
    return date, clock


def _chunks(data: bytes, size: int = 64 * 1024) -> Iterator[bytes]:
    for start in range(0, len(data), size):
        yield data[start : start + size]


def stream_zip(entries: Iterable[tuple[str, Path]], *, maximum_bytes: int | None = None) -> Iterator[bytes]:
    if maximum_bytes is None:
        maximum_bytes = artifact_limit_bytes()
    limit_label = _limit_label(maximum_bytes)
    manifest = []
    total_size = 0
    for name, path in entries:
        parts = name.split("/") if isinstance(name, str) else []
        if (not name or "\\" in name or name.startswith("/") or ":" in name
                or any(part in ("", ".", "..") for part in parts)
                or PurePosixPath(name).is_absolute() or re.match(r"^[A-Za-z]:", name)):
            raise ValueError("unsafe ZIP entry name")
        stat = path.stat()
        if not path.is_file():
            raise ValueError("ZIP entry is not a regular file")
        total_size += stat.st_size
        if stat.st_size >= 0xFFFFFFFF:
            raise ValueError(f"run result package exceeds the {limit_label} archive limit")
        manifest.append((name, path, stat.st_size, stat.st_mtime))
    if len(manifest) > 65535:
        raise ValueError("too many entries for ZIP32")
    zip_bound = 22
    for name, path, size, _mtime in manifest:
        name_size = len(name.encode("utf-8"))
        zip_bound += size + (size >> 12) + (size >> 14) + (size >> 25) + 13
        zip_bound += 30 + 16 + 46 + name_size * 2
    if zip_bound > maximum_bytes:
        raise ValueError(f"run result package exceeds the {limit_label} streaming archive budget")

    offset = 0
    central = []
    for name, path, _size, mtime in manifest:
        encoded = name.encode("utf-8")
        if len(encoded) > 65535:
            raise ValueError("ZIP entry name is too long")
        date, clock = _dos_datetime(mtime)
        local_offset = offset
        header = struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, 0x0808, 8, clock, date, 0, 0, 0, len(encoded), 0) + encoded
        offset += len(header)
        yield header
        crc = 0
        compressed_size = 0
        uncompressed_size = 0
        compressor = zlib.compressobj(level=6, method=zlib.DEFLATED, wbits=-15)
        with path.open("rb") as stream:
            while True:
                block = stream.read(64 * 1024)
                if not block:
                    break
                uncompressed_size += len(block)
                crc = zlib.crc32(block, crc)
                compressed = compressor.compress(block)
                if compressed:
                    compressed_size += len(compressed)
                    if offset + len(compressed) > maximum_bytes:
                        raise ValueError(f"compressed result package exceeds the {limit_label} archive limit")
                    offset += len(compressed)
                    yield compressed
            tail = compressor.flush()
            if tail:
                compressed_size += len(tail)
                if offset + len(tail) > maximum_bytes:
                    raise ValueError(f"compressed result package exceeds the {limit_label} archive limit")
                offset += len(tail)
                yield tail
        descriptor = struct.pack("<IIII", 0x08074B50, crc & 0xFFFFFFFF, compressed_size, uncompressed_size)
        offset += len(descriptor)
        yield descriptor
        central.append((encoded, date, clock, crc & 0xFFFFFFFF, compressed_size, uncompressed_size, local_offset))

    central_offset = offset
    central_bytes = 0
    for encoded, date, clock, crc, compressed_size, uncompressed_size, local_offset in central:
        record = struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 20, 20, 0x0808, 8, clock, date, crc,
                             compressed_size, uncompressed_size, len(encoded), 0, 0, 0, 0, 0, local_offset) + encoded
        central_bytes += len(record)
        offset += len(record)
        yield record
    if central_offset >= 0xFFFFFFFF or central_bytes >= 0xFFFFFFFF or offset + 22 > maximum_bytes:
        raise ValueError(f"ZIP archive exceeds ZIP32 or {limit_label} limits")
    yield struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, len(central), len(central), central_bytes, central_offset, 0)


async def stream_zip_async(entries: Iterable[tuple[str, Path]], *, maximum_bytes: int | None = None) -> AsyncIterator[bytes]:
    """Yield bounded 64 KiB ZIP chunks through an async response interface."""
    for chunk in stream_zip(entries, maximum_bytes=maximum_bytes):
        yield chunk
