#!/usr/bin/env python3
"""Run masked particle-shape probe cases, one bounded child process each.

Every case runs in its own ``scripts/bounded_child.py`` process so a SIGSEGV or
SIGABRT can never take down the caller. Cases and results live under
``var/probe-a4-mask/`` (git-ignored).

    python worker/run_particle_mask_probe.py [case-name ...]
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = str(ROOT / ".venv" / "bin" / "python")
OUT = ROOT / "var" / "probe-a4-mask"


def plane(dimension: int, **overrides) -> dict:
    case = {"dimension": dimension, "geometry": "plane", "mask": "none",
            "shape": "sphere", "magnitude": 0.2, "process": "two-arg"}
    case.update(overrides)
    return case


CASES: dict[str, dict] = {}


def add(name: str, case: dict) -> None:
    CASES[name] = case


# ---- 1. the official 4.6.2 FinFET pattern ---------------------------------
# examples/emulation/FinFET.py: explicit Mask level set, addMaskMaterial(Si)
# and a negative BoxDistribution. All three Process overloads must agree.
for dim in (3,):
    for label, process, duration in (
        ("twoarg", "two-arg", None), ("zero", "three-arg", 0.0), ("one", "three-arg", 1.0)
    ):
        add(f"finfet-masked-etch-{dim}d-{label}",
            plane(dim, geometry="finfet", shape="box",
                  halfAxes=[-0.05, -0.05, -0.3], magnitude=-0.3, mask="material",
                  maskMaterial="Si", process=process, duration=duration))
    add(f"finfet-unmasked-etch-{dim}d",
        plane(dim, geometry="finfet", shape="box",
              halfAxes=[-0.05, -0.05, -0.3], magnitude=-0.3, mask="none",
              process="two-arg"))
    add(f"finfet-masked-depo-{dim}d",
        plane(dim, geometry="finfet", shape="sphere", magnitude=0.2,
              mask="material", maskMaterial="Si", process="two-arg"))

# ---- 2. masked etch: the supported direction -------------------------------
for dim in (2, 3):
    add(f"etch-maskbox-{dim}d",
        plane(dim, geometry="mask-box", shape="box",
              halfAxes=[-0.05, -0.05, -0.2], magnitude=-0.2, mask="material",
              maskMaterial="Mask", process="two-arg"))
    add(f"etch-maskbox-{dim}d-sphere",
        plane(dim, geometry="mask-box", shape="sphere", magnitude=-0.15,
              mask="material", maskMaterial="Mask", process="two-arg"))
    add(f"etch-maskbox-{dim}d-duration1",
        plane(dim, geometry="mask-box", shape="sphere", magnitude=-0.15,
              mask="material", maskMaterial="Mask", process="three-arg", duration=1.0))
    add(f"etch-maskbox-{dim}d-raw",
        plane(dim, geometry="mask-box", shape="sphere", magnitude=-0.15,
              mask="getMaterialLevelSet", maskMaterial="Mask", process="two-arg"))
    add(f"etch-maskbox-{dim}d-single",
        plane(dim, geometry="mask-box", shape="sphere", magnitude=-0.15,
              mask="applyToSingleMaterial", targetMaterial="Si", process="two-arg"))
    add(f"etch-maskbox-{dim}d-insertmask",
        plane(dim, geometry="mask-box", insertMode="mask", shape="sphere",
              magnitude=-0.15, mask="material", maskMaterial="Mask", process="two-arg"))
    add(f"etch-maskbox-{dim}d-far",
        plane(dim, geometry="mask-box",
              maskBox=([-1.0, -1.0, 1.2], [1.0, 1.0, 1.8]) if dim == 3
              else ([-1.0, 1.2], [1.0, 1.8]),
              shape="sphere", magnitude=-0.15, mask="material", maskMaterial="Mask",
              process="two-arg"))
    add(f"etch-maskbox-{dim}d-full",
        plane(dim, geometry="mask-full", shape="sphere", magnitude=-0.15,
              mask="material", maskMaterial="Mask", process="two-arg"))
    add(f"etch-maskbox-{dim}d-deep",
        plane(dim, geometry="mask-box", shape="box",
              halfAxes=[-0.05, -0.05, -0.9], magnitude=-0.9, mask="material",
              maskMaterial="Mask", process="two-arg"))
    add(f"etch-trench-{dim}d",
        plane(dim, geometry="trench", shape="sphere", magnitude=-0.1,
              mask="material", maskMaterial="Mask", process="two-arg"))
    add(f"etch-trench-{dim}d-nomask",
        plane(dim, geometry="trench", shape="sphere", magnitude=-0.1, mask="none",
              process="two-arg"))
    add(f"etch-stack-{dim}d",
        plane(dim, geometry="stack", shape="sphere", magnitude=-0.1,
              mask="material", maskMaterials=["Mask", "Si3N4"], process="two-arg"))

# ---- 3. masked deposition: every non-empty mask SIGSEGVs -------------------
for dim in (2, 3):
    add(f"depo-maskbox-{dim}d",
        plane(dim, geometry="mask-box", shape="sphere", magnitude=0.2,
              mask="material", maskMaterial="Mask", process="two-arg"))
    add(f"depo-maskbox-{dim}d-raw",
        plane(dim, geometry="mask-box", shape="sphere", magnitude=0.2,
              mask="getMaterialLevelSet", maskMaterial="Mask", process="two-arg"))
    add(f"depo-maskbox-{dim}d-single",
        plane(dim, geometry="mask-box", shape="sphere", magnitude=0.2,
              mask="applyToSingleMaterial", targetMaterial="Si", process="two-arg"))
    add(f"depo-maskbox-{dim}d-insertmask",
        plane(dim, geometry="mask-box", insertMode="mask", shape="sphere",
              magnitude=0.2, mask="material", maskMaterial="Mask", process="two-arg"))
    add(f"depo-maskbox-{dim}d-far",
        plane(dim, geometry="mask-box",
              maskBox=([-1.0, -1.0, 1.2], [1.0, 1.0, 1.8]) if dim == 3
              else ([-1.0, 1.2], [1.0, 1.8]),
              shape="sphere", magnitude=0.2, mask="material", maskMaterial="Mask",
              process="two-arg"))
    add(f"depo-maskbox-{dim}d-full",
        plane(dim, geometry="mask-full", shape="sphere", magnitude=0.2,
              mask="material", maskMaterial="Mask", process="two-arg"))
    add(f"depo-rawsurface-{dim}d",
        plane(dim, geometry="plane", shape="sphere", magnitude=0.2,
              mask="surface-domain", process="two-arg"))
    add(f"depo-trench-{dim}d",
        plane(dim, geometry="trench", shape="sphere", magnitude=0.2,
              mask="material", maskMaterial="Mask", process="two-arg"))
    add(f"depo-stack-{dim}d",
        plane(dim, geometry="stack", shape="sphere", magnitude=0.1,
              mask="material", maskMaterials=["Mask", "Si3N4"], process="two-arg"))

# ---- 4. mask present but unusable: degrades to the no-mask overload --------
for dim in (2, 3):
    add(f"depo-maskempty-{dim}d",
        plane(dim, geometry="mask-empty", shape="sphere", magnitude=0.2,
              mask="material", maskMaterial="Mask", process="two-arg"))
    add(f"depo-maskabsent-{dim}d",
        plane(dim, geometry="plane", shape="sphere", magnitude=0.2,
              mask="material", maskMaterial="Mask", process="two-arg"))


def run(name: str) -> dict:
    case = CASES[name]
    OUT.mkdir(parents=True, exist_ok=True)
    case_path = OUT / f"{name}.case.json"
    out_path = OUT / f"{name}.out.json"
    record_path = OUT / f"{name}.guard.json"
    log_path = OUT / f"{name}.log"
    for stale in (out_path, record_path, log_path):
        stale.unlink(missing_ok=True)
    case_path.write_text(json.dumps(case, indent=2) + "\n", encoding="utf-8")
    command = [PYTHON, "scripts/bounded_child.py", "--cwd", str(ROOT),
               "--record", str(record_path), "--log-path", str(log_path),
               "--timeout-seconds", "60", "--max-rss-mib", "4096",
               "--max-log-bytes", str(1024 * 1024), "--",
               PYTHON, "worker/probe_particle_mask.py", str(case_path), str(out_path)]
    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=120)
    guard = json.loads(record_path.read_text(encoding="utf-8")) if record_path.exists() else None
    result = {"name": name, "case": case, "supervisorRc": completed.returncode,
              "guard": guard}
    if out_path.exists():
        result["probe"] = json.loads(out_path.read_text(encoding="utf-8"))
    log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    result["logTail"] = log[-1200:]
    return result


def main() -> int:
    selected = sys.argv[1:] or list(CASES)
    unknown = [name for name in selected if name not in CASES]
    if unknown:
        print(f"unknown cases: {', '.join(unknown)}", file=sys.stderr)
        return 2
    summary = []
    for name in selected:
        result = run(name)
        guard = result["guard"] or {}
        probe = result.get("probe") or {}
        entry = {
            "name": name,
            "returnCode": guard.get("returnCode"),
            "terminationReason": guard.get("terminationReason"),
            "nativeClass": probe.get("nativeClass"),
            "processCall": probe.get("processCall"),
            "beforeBox": (probe.get("diagnostics") or {}).get("before", {}).get("boundingBox"),
            "afterBox": (probe.get("after") or {}).get("boundingBox"),
            "levelsBefore": (probe.get("diagnostics") or {}).get("before", {}).get("levelSetPointCounts"),
            "levelsAfter": (probe.get("after") or {}).get("levelSetPointCounts"),
        }
        summary.append(entry)
        print(json.dumps(entry, ensure_ascii=False), flush=True)
        if guard.get("returnCode") not in (0, None):
            print(f"  log tail:\n{result['logTail']}", flush=True)
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
