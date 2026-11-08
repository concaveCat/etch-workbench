"""Inventory public ViennaPS 4.6.2 bindings without running a simulation."""
from __future__ import annotations

import inspect
import json
import re
import sys
from pathlib import Path

import viennals as vls
import viennaps as vps


PATTERN = re.compile(r"etch|plasma|ion|wet|deposit|teos|ald|oxid|epitax|neutral|particle|transport|precursor|surface|unit", re.I)


def doc(value) -> str | None:
    raw = getattr(value, "__doc__", None)
    if not raw:
        return None
    return inspect.cleandoc(raw)[:12000]


def safe_signature(value) -> str | None:
    try:
        return str(inspect.signature(value))
    except Exception:
        return None


def describe_class(name: str, value) -> dict:
    methods = {}
    selected_model = bool(re.search(r"Etch|Etching|Deposit|Deposition|Transport|Regrowth|Oxidation|Epitaxy|ALD|Process", name, re.I))
    for method_name in sorted(set(dir(value))):
        if method_name.startswith("_"):
            continue
        try:
            method = getattr(value, method_name)
        except Exception as exc:
            methods[method_name] = {"readError": f"{type(exc).__name__}: {exc}"}
            continue
        if callable(method) and (method_name in {"apply", "run", "execute", "calculateFlux"}
                                 or method_name.startswith(("set", "get", "add", "save", "write"))
                                 or (name in {"Process", "MultiParticleProcess"} and PATTERN.search(method_name))):
            methods[method_name] = {"signature": safe_signature(method), "doc": doc(method)}
    constructor = getattr(value, "__init__", None)
    return {"name": name, "signature": safe_signature(value), "doc": doc(value),
            "constructorDoc": doc(constructor) if selected_model else None, "methods": methods}


def describe_module(module) -> dict:
    symbols = {}
    names = sorted(dir(module))
    for name in names:
        if name.startswith("_") or not PATTERN.search(name):
            continue
        try:
            value = getattr(module, name)
        except Exception as exc:
            symbols[name] = {"readError": f"{type(exc).__name__}: {exc}"}
            continue
        if inspect.isclass(value) or inspect.isfunction(value) or callable(value):
            symbols[name] = describe_class(name, value) if inspect.isclass(value) else {
                "signature": safe_signature(value), "doc": doc(value)
            }
        else:
            symbols[name] = {"value": repr(value)[:500]}
    return {"module": module.__name__, "version": getattr(module, "__version__", None),
            "allPublicNames": [name for name in names if not name.startswith("_")], "symbols": symbols}


def main(output_path: str) -> None:
    payload = {
        "python": sys.version,
        "viennaps": describe_module(vps),
        "viennals": describe_module(vls),
    }
    Path(output_path).write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: stage8_binding_inventory.py OUTPUT.json")
    main(sys.argv[1])
