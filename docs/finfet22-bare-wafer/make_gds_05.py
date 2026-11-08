# -*- coding: utf-8 -*-
"""Write a 0.5 x 0.5 um version of the 22 nm FinFET mask set.

Only the layers the bare-wafer demo uses are kept:
  1 Fin, 2 Gate, 4 CB, 5 M0, 7 SUB (wafer).
WELL/TS/STI are dropped. The layout is the same as make_gds.py, just in a
smaller cell so the field cell-set grid fits the 2M native limit at 5 nm.
"""
from __future__ import annotations

import hashlib
import pathlib
import struct
import sys

HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE / "finfet22_05.gds"
sys.path.insert(0, str(HERE.parents[1]))
from backend.gds import _encode_real8, _encode_record  # noqa: E402

FIN_X0, FIN_PITCH, FIN_WIDTH = 0.050, 0.060, 0.010
FIN_Y0, FIN_Y1, N_FIN = 0.030, 0.470, 7
GATE_Y0, GATE_PITCH, GATE_LENGTH = 0.060, 0.090, 0.028
GATE_X0, GATE_X1, N_GATE = 0.020, 0.440, 5
CB_Y, CB_SIZE = [0.105, 0.195, 0.285, 0.375], 0.020
M0_HALF_WIDTH = 0.012
OFF_X, OFF_Y = -0.230, -0.240
DOMAIN = 0.500


def rect(x0, y0, x1, y1, layer):
    pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    xy = b"".join(struct.pack(">ii", round(x * 1000), round(y * 1000)) for x, y in pts)
    return (b"".join([
        _encode_record(0x08, 0x00, b""),          # BOUNDARY
        _encode_record(0x0D, 0x02, struct.pack(">h", layer)),
        _encode_record(0x0E, 0x02, struct.pack(">h", 0)),
        _encode_record(0x10, 0x03, xy),
        _encode_record(0x11, 0x00, b""),          # ENDEL
    ]))


def shift(x0, y0, x1, y1, layer):
    return rect(x0 + OFF_X, y0 + OFF_Y, x1 + OFF_X, y1 + OFF_Y, layer)


def ascii_rec(kind, text):
    payload = text.encode("ascii")
    if len(payload) % 2:
        payload += b"\0"
    return _encode_record(kind, 0x06, payload)


def main():
    body = []
    for i in range(N_FIN):
        x = FIN_X0 + i * FIN_PITCH
        body.append(shift(x - FIN_WIDTH / 2, FIN_Y0, x + FIN_WIDTH / 2, FIN_Y1, 1))
    for j in range(N_GATE):
        y = GATE_Y0 + j * GATE_PITCH
        body.append(shift(GATE_X0, y - GATE_LENGTH / 2, GATE_X1, y + GATE_LENGTH / 2, 2))
    for y in CB_Y:
        for i in range(N_FIN):
            x = FIN_X0 + i * FIN_PITCH
            body.append(shift(x - CB_SIZE / 2, y - CB_SIZE / 2,
                              x + CB_SIZE / 2, y + CB_SIZE / 2, 4))
        body.append(shift(0.030, y - M0_HALF_WIDTH, 0.430, y + M0_HALF_WIDTH, 5))
    inset = 0.001
    body.append(rect(-DOMAIN / 2 + inset, -DOMAIN / 2 + inset,
                     DOMAIN / 2 - inset, DOMAIN / 2 - inset, 7))

    now = (2026, 10, 31, 12, 0, 0)
    data = b"".join([
        _encode_record(0x00, 0x02, struct.pack(">h", 600)),
        _encode_record(0x01, 0x02, struct.pack(">12h", *(now + now))),
        ascii_rec(0x02, "FINFET22_05"),
        _encode_record(0x03, 0x05, _encode_real8(0.001) + _encode_real8(1e-9)),
        _encode_record(0x05, 0x02, struct.pack(">12h", *(now + now))),
        ascii_rec(0x06, "FINFET22"),
        b"".join(body),
        _encode_record(0x07, 0x00, b""),
        _encode_record(0x04, 0x00, b""),
    ])
    OUT.write_bytes(data)
    print("wrote", OUT, len(data), "sha256", hashlib.sha256(data).hexdigest())


if __name__ == "__main__":
    main()
