"""Static audit of a StepIR flow: find steps that cannot mean what they say.

The comparison cannot validate a flow that is internally inconsistent, and a
25-minute run is too expensive to use as a debugger. This walks the step list,
tracks which materials exist, and reports:

* a patterned etch whose mask material is not present (stale mask);
* a strip of a material that is not there;
* a fill of a material that already exists (should be a shell/deposit);
* a litho whose photoresist already exists (a second coat is not what the flow says);
* an etch deeper than the whole stack above the domain floor.

Usage: ``python -m engines.audit engines/examples/memory1-full.json``
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def audit(job: dict) -> list[dict]:
    initial = job["initial"]
    present: list[str] = [initial["substrate"]]
    findings: list[dict] = []
    height = 0.0
    floor = -float(initial["substrate_thickness"])
    masked_since: str | None = None

    def note(step: dict, kind: str, detail: str, severity: str = "error") -> None:
        findings.append({"step": step["step_id"], "kind": kind, "detail": detail,
                         "severity": severity})

    for step in job["steps"][1:]:
        kind, mechanism = step["kind"], step["mechanism"]
        params = step.get("parameters") or {}
        rates = step.get("rates") or {}
        if mechanism == "litho":
            if params.get("material") in present:
                note(step, "duplicate-resist",
                     f"litho adds {params.get('material')!r} but it is already present; "
                     f"a second level set for the same material confuses the stack")
            present.append(str(params.get("material", "Mask")))
            masked_since = step["step_id"]
            height += float(params.get("coat", 0.1))
            continue
        if mechanism == "strip":
            for name in params.get("materials") or []:
                if name not in present:
                    note(step, "strip-missing", f"{name!r} is not in the stack")
                else:
                    present[present.index(name)] = f"__removed_{name}"
            present = [name for name in present if not name.startswith("__removed_")]
            if masked_since and all(mask not in present for mask in ("PMMA", "Mask")):
                masked_since = None
            continue
        if mechanism == "fill":
            name = str(params.get("material"))
            if name in present:
                note(step, "fill-existing",
                     f"{name!r} already exists; the adapter unions the fill into that "
                     f"material instead of inserting a duplicate level set",
                     severity="note")
            else:
                present.append(name)
            height += float(params.get("offset", 0.0))
            continue
        if kind == "deposit":
            for name, rate in rates.items():
                if float(rate) > 0 and name not in present:
                    present.append(name)
                height += max(0.0, float(rate)) * float(step.get("duration", 0.0))
            continue
        if kind == "etch":
            mask_names = [n for n in (params.get("mask") or []) if n]
            if mask_names and all(name not in present for name in mask_names):
                note(step, "stale-mask",
                     f"mask {mask_names} is not present, so the step will cut through the "
                     f"openings of an earlier module")
            depth = max((abs(float(rate)) for rate in rates.values()), default=0.0) * float(
                step.get("duration", 0.0))
            if depth > height - floor + 1e-9:
                note(step, "etch-deeper-than-stack",
                     f"depth {depth:.3f} um exceeds the {height - floor:.3f} um above the "
                     f"floor; a patterned etch is clamped by the openings",
                     severity="note" if params.get("patterned") else "error")
            continue
        if mechanism == "cmp":
            stop = params.get("stop_material")
            if stop and stop not in present:
                note(step, "cmp-missing-stop", f"stop material {stop!r} is not in the stack")
    return findings


def main(argv: list[str]) -> int:
    path = Path(argv[1] if len(argv) > 1 else "engines/examples/memory1-full.json")
    job = json.loads(path.read_text())
    findings = audit(job)
    errors = [row for row in findings if row["severity"] == "error"]
    notes = [row for row in findings if row["severity"] != "error"]
    print(f"{path}: {len(job['steps'])} steps, {len(errors)} error(s), {len(notes)} note(s)")
    for row in errors + notes:
        mark = "!!" if row["severity"] == "error" else " ·"
        print(f" {mark} {row['step']:<30} {row['kind']:<22} {row['detail']}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
