"""One-off generator for tests/fixtures/gds_full_bounds_cell.gds.

Not imported by any test: the committed .gds is the fixture.  gdstk is not
installed in the workbench venv, so the same GDSII records gdstk would emit are
written directly (UNITS: 1 user unit = 1e-3 mm, 1 database unit = 1 nm).

Layout (structure FULLBOUNDS, bounds -2000..2000 nm in x and y):
  layer 7  polygon exactly filling the library bounds (the crash case)
  layer 1  bar inside the cell
  layer 2  small square near the top edge
"""
from pathlib import Path
import struct

HERE = Path(__file__).resolve().parent
OUT = HERE / "gds_full_bounds_cell.gds"


def record(kind: int, dtype: int, payload: bytes = b"") -> bytes:
    return struct.pack(">HBB", len(payload) + 4, kind, dtype) + payload


def int16(value: int) -> bytes:
    return struct.pack(">h", value)


def int32(value: int) -> bytes:
    return struct.pack(">i", value)


def real8(value: float) -> bytes:
    # GDSII excess-64 exponent, base-16 mantissa, 56-bit fraction.
    exponent = 0
    while value >= 16.0:
        value /= 16.0
        exponent += 1
    while value < 1.0 / 16.0:
        value *= 16.0
        exponent -= 1
    mantissa = int(round(value * (1 << 56)))
    return bytes([exponent + 64]) + mantissa.to_bytes(7, "big")


def point_list(points: list[tuple[int, int]]) -> bytes:
    return b"".join(int32(x) + int32(y) for x, y in points)


def rectangle(x0: int, y0: int, x1: int, y1: int, layer: int) -> bytes:
    return b"".join([
        record(0x08, 0x00),                      # BOUNDARY
        record(0x0D, 0x02, int16(layer)),        # LAYER
        record(0x0E, 0x02, int16(0)),            # DATATYPE
        record(0x10, 0x03, point_list(            # XY, closed
            [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)])),
        record(0x11, 0x00),                      # ENDEL
    ])


def string_record(kind: int, value: str) -> bytes:
    payload = value.encode("ascii")
    if len(payload) % 2:
        payload += b"\x00"
    return record(kind, 0x06, payload)


def main() -> None:
    now = (2026, 10, 29, 0, 0, 0)
    units = b"".join([real8(1e-3), real8(1e-9)])
    blob = b"".join([
        record(0x00, 0x02, int16(600)),                  # HEADER
        record(0x01, 0x02, struct.pack(">12h", *(now + now))),  # BGNLIB
        string_record(0x02, "GDSFULLBOUNDS"),            # LIBNAME
        record(0x03, 0x05, units),                       # UNITS
        record(0x05, 0x02, struct.pack(">12h", *(now + now))),  # BGNSTR
        string_record(0x06, "FULLBOUNDS"),               # STRNAME
        rectangle(-2000, -2000, 2000, 2000, 7),
        rectangle(-1000, -500, 1000, 500, 1),
        rectangle(-200, 1000, 200, 1400, 2),
        record(0x07, 0x00),                              # ENDSTR
        record(0x04, 0x00),                              # ENDLIB
    ])
    OUT.write_bytes(blob)
    print(f"wrote {OUT} ({len(blob)} bytes)")


if __name__ == "__main__":
    main()
