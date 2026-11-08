"""Bounded, canonical CSV rate-table assets for the native ``CSVFileProcess``.

The installed ViennaPS 4.6.2 binding reads the table through
``viennaps::RateGrid<NumericType, D>::loadFromCSV``. That loader takes exactly
``D`` numeric columns per row and no header:

* 2D (``D == 2``): ``x,rate`` — one rate per lateral x position.
* 3D (``D == 3``): ``x,y,rate`` — one rate per (x, y) position.

There is no vertical/z column in either dimension. The audit note that expected
``x,z,rate`` does not match the installed binding; this module is the verified
schema, and it is the single place the workbench parses an uploaded table.

The native reader is narrower than Python's text parsing: it only strips ASCII
spaces, does not accept a BOM, treats tabs as data, and only treats a line whose
first character (after its own space trimming) is ``#`` as a comment. Instead of
trying to mirror those quirks, uploads are parsed strictly and re-emitted in a
canonical form that is known-native-safe:

* optional UTF-8 BOM is removed; the rest must be ASCII;
* ASCII spaces/tabs around lines and cells are normalized;
* comment lines (leading ASCII whitespace, then ``#``) and blank lines are
  removed; an inline ``#`` in a data row is rejected because the native reader
  would try to parse it as a number;
* only plain decimal/exponent numbers are accepted, with no NaN/Inf/hex, no
  underflow-to-zero and no nonzero subnormal doubles (the native reader rejects
  subnormals while Python would still represent them);
* every number is re-emitted with Python's shortest round-trip decimal form;
* output is LF-terminated ASCII with exactly D comma-separated columns per row.

The stored asset bytes are exactly these canonical bytes, so the asset ID is the
SHA-256 of what the native engine loads. The worker re-canonicalizes its copy
and refuses anything else.

The native interpolation modes are ``LINEAR``, ``IDW`` and ``CUSTOM``. Only the
first two are exposed: ``CUSTOM`` installs an arbitrary Python callable and is
intentionally unreachable from submitted JSON.
"""
from __future__ import annotations

import hashlib
import math
import re
import sys
from typing import Any

MAX_CSV_BYTES = 16 * 1024 * 1024
MAX_ROWS = 1_000_000
MAX_COORDINATE = 1_000_000.0
MAX_RATE = 1_000_000.0
MAX_CELL_CHARS = 64
CANONICAL_FORMAT = "ascii-lf-comma-decimal-v1"

# Same numeric grammar for every cell. Native strtod accepts a leading sign, a
# bare fraction and an exponent; underscores, hex floats and inf/nan spellings
# are rejected.
_NUMBER = re.compile(r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$")

COLUMNS_BY_DIMENSION = {2: ["x", "rate"], 3: ["x", "y", "rate"]}


class RateCsvError(ValueError):
    pass


def _finite(text: str, *, line: int, column: str) -> float:
    token = text.strip(" \t")
    if not token:
        raise RateCsvError(f"line {line}: column {column} is empty")
    if len(token) > MAX_CELL_CHARS:
        raise RateCsvError(f"line {line}: column {column} is too long")
    if not _NUMBER.fullmatch(token):
        raise RateCsvError(f"line {line}: column {column} is not a plain decimal number: {text!r}")
    value = float(token)
    if not math.isfinite(value):
        raise RateCsvError(f"line {line}: column {column} does not fit a finite float")
    if value == 0.0:
        mantissa = token.lower().split("e", 1)[0].lstrip("+-")
        if any(character in "123456789" for character in mantissa):
            raise RateCsvError(
                f"line {line}: column {column} underflows to zero; use a representable rate or an explicit 0")
        # Normalize negative zero so the canonical file is deterministic.
        value = 0.0
    elif abs(value) < sys.float_info.min:
        # The native RateGrid reader rejects subnormal literals even though
        # Python can represent them. IDW then returns without moving and LINEAR
        # can crash, so they must never reach the native constructor.
        raise RateCsvError(
            f"line {line}: column {column} is a subnormal double ({value:g}); "
            f"use 0 or a magnitude at least {sys.float_info.min:g}")
    return value


def _format_number(value: float) -> str:
    text = repr(float(value))
    if text == "-0.0":
        return "0.0"
    if "e" in text:
        mantissa, exponent = text.split("e", 1)
        if "." not in mantissa:
            mantissa += ".0"
        text = f"{mantissa}e{int(exponent):+d}"
    return text


def canonicalize_rate_csv(data: bytes, dimension: int) -> tuple[bytes, dict[str, Any]]:
    """Validate ``data`` and return ``(canonical_bytes, summary)``."""
    if type(dimension) is not int or dimension not in (2, 3):
        raise RateCsvError("rate tables require dimension 2 or 3")
    if not data:
        raise RateCsvError("rate table is empty")
    if len(data) > MAX_CSV_BYTES:
        raise RateCsvError(f"rate table exceeds the {MAX_CSV_BYTES // (1024 * 1024)} MiB limit")

    payload = data[3:] if data.startswith(b"\xef\xbb\xbf") else data
    if b"\xef\xbb\xbf" in payload:
        raise RateCsvError("a UTF-8 BOM is only allowed at the start of the file")
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as exc:
        raise RateCsvError("rate table must be ASCII text; Unicode whitespace and digits are not accepted by the native reader") from exc

    expected_columns = len(COLUMNS_BY_DIMENSION[dimension])
    rows: list[list[float]] = []
    lines = text.split("\n")
    for line_number, raw_line in enumerate(lines, start=1):
        if raw_line.endswith("\r"):
            raw_line = raw_line[:-1]
        if "\r" in raw_line:
            raise RateCsvError(f"line {line_number}: stray carriage return; use LF or CRLF line endings")
        if any(ord(character) < 32 and character not in "\t" for character in raw_line):
            raise RateCsvError(f"line {line_number}: control characters are not allowed")
        line = raw_line.strip(" \t")
        if not line:
            continue
        if line.startswith("#"):
            continue
        if "#" in line:
            raise RateCsvError(
                f"line {line_number}: inline '#' is not a comment; the native reader only skips lines that start with '#'")
        cells = line.split(",")
        if len(cells) != expected_columns:
            raise RateCsvError(
                f"line {line_number}: expected {expected_columns} columns "
                f"({','.join(COLUMNS_BY_DIMENSION[dimension])}), found {len(cells)}")
        values = [_finite(cell, line=line_number, column=COLUMNS_BY_DIMENSION[dimension][index])
                  for index, cell in enumerate(cells)]
        for coordinate in values[:-1]:
            if abs(coordinate) > MAX_COORDINATE:
                raise RateCsvError(f"line {line_number}: coordinate exceeds ±{MAX_COORDINATE:g} um")
        if abs(values[-1]) > MAX_RATE:
            raise RateCsvError(f"line {line_number}: rate exceeds ±{MAX_RATE:g} um/s")
        rows.append(values)
        if len(rows) > MAX_ROWS:
            raise RateCsvError(f"rate table exceeds the {MAX_ROWS} row limit")
    if len(rows) < 2:
        raise RateCsvError("rate table needs at least two data rows")

    canonical_bytes = ("\n".join(",".join(_format_number(value) for value in row) for row in rows) + "\n").encode("ascii")
    if len(canonical_bytes) > MAX_CSV_BYTES:
        raise RateCsvError(f"canonical rate table exceeds the {MAX_CSV_BYTES // (1024 * 1024)} MiB limit")

    xs = sorted({row[0] for row in rows})
    if dimension == 2:
        linear_compatible = len(xs) == len(rows) and all(
            rows[index][0] < rows[index + 1][0] for index in range(len(rows) - 1))
        y_values = None
    else:
        ys = sorted({row[1] for row in rows})
        pairs = {(row[0], row[1]) for row in rows}
        linear_compatible = (len(xs) >= 2 and len(ys) >= 2
                             and len(pairs) == len(rows) == len(xs) * len(ys))
        y_values = {"min": ys[0], "max": ys[-1], "count": len(ys)}
    rates = [row[-1] for row in rows]
    summary: dict[str, Any] = {
        "schema": "viennaps-rate-grid-v1",
        "canonicalFormat": CANONICAL_FORMAT,
        "canonicalSha256": hashlib.sha256(canonical_bytes).hexdigest(),
        "dimension": dimension,
        "columns": list(COLUMNS_BY_DIMENSION[dimension]),
        "rowCount": len(rows),
        "coordinateUnit": "um",
        "rateUnit": "um/s",
        "x": {"min": xs[0], "max": xs[-1], "count": len(xs)},
        "rate": {"min": min(rates), "max": max(rates),
                 "sign": "deposit" if min(rates) > 0 else "etch" if max(rates) < 0 else "mixed"},
        "linearCompatible": linear_compatible,
        "idwCompatible": True,
        "sizeBytes": len(canonical_bytes),
        "sourceSizeBytes": len(data),
    }
    if y_values is not None:
        summary["y"] = y_values
    return canonical_bytes, summary


def inspect_rate_csv(data: bytes, dimension: int) -> dict[str, Any]:
    """Validate a rate table and return its canonical summary."""
    return canonicalize_rate_csv(data, dimension)[1]
