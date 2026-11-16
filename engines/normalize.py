"""Material-name canonicalisation shared by the engines and the comparison.

SPX calls silicon ``Silicon``, oxide ``Oxide`` and reports ``Gas``/``None`` for
space; ViennaPS uses the workbench catalogue (``Si``, ``SiO2``, ...). Comparing
labels without a single canonical vocabulary produces agreement 0.0 while both
engines are right, so the mapping lives here and nowhere else.

``engines/normalize.json`` is the editable table; this module is the loader plus
the two rules that cannot be expressed in JSON (void aliases and the preference for
the primary workbench name when several share one SPX material).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

VOID = "void"
TABLE_PATH = Path(__file__).resolve().parent / "normalize.json"

# SPX/synthetic labels that mean "no material here".
VOID_ALIASES = {"", "none", "void", "gas", "empty", "air", "vacuum", "unknown"}


@lru_cache(maxsize=1)
def table() -> dict[str, Any]:
    return json.loads(TABLE_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def spx_to_workbench() -> dict[str, str]:
    """SPX material -> workbench name, preferring the first (primary) entry.

    ``Dielectric`` and ``SiO2`` both map to SPX ``Oxide``; the JSON order decides
    which workbench name wins (``SiO2`` is listed first on purpose).
    """
    mapping: dict[str, str] = {}
    for workbench, row in table()["materials"].items():
        spx_name = str(row["spx"])
        mapping.setdefault(spx_name, workbench)
    return mapping


def to_spx(name: str) -> str:
    return str((table()["materials"].get(name) or {}).get("spx") or name)


def canonical(name: Any, engine: str) -> str:
    """Canonical workbench label for a raw engine label."""
    if name is None:
        return VOID
    text = str(name).strip()
    if text.lower() in VOID_ALIASES:
        return VOID
    if engine == "spx":
        return spx_to_workbench().get(text, text)
    return text


def color(name: str) -> str:
    return str((table()["materials"].get(name) or {}).get("color") or "#888888")
