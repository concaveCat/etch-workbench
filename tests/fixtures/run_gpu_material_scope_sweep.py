"""Exercise generic GPU models over every installed material identity/role.

The nested runner launches both the product worker and an independently built
ViennaPS reference, always beneath bounded_child.py. This script only
orchestrates those bounded cases and archives their per-case summaries.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "docs/stage9-evidence/gpu-coverage-audit/material-scope"
RUNNER = ROOT / "tests/fixtures/run_gpu_geometry_coverage.py"
PYTHON = ROOT / ".venv/bin/python"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", choices=("GPU_DISK", "CPU_DISK"), default="GPU_DISK")
    parser.add_argument("--phase", choices=("builtins", "holes", "fins", "custom", "all"), default="all")
    parser.add_argument("--include-fin", action="store_true")
    parser.add_argument("--boundary", choices=("reflective", "periodic"), default="reflective")
    args = parser.parse_args()
    catalog = json.loads((ROOT / "docs/materials-catalog.json").read_text(encoding="utf-8"))
    materials = [row["name"] for row in catalog["materials"]]
    evidence_dir = EVIDENCE if args.boundary == "reflective" else EVIDENCE / args.boundary
    evidence_dir.mkdir(parents=True, exist_ok=True)
    invocations: list[dict] = []
    cases: list[dict] = []

    def invoke(label: str, flags: list[str]) -> None:
        index = len(invocations)
        command = [str(PYTHON), str(RUNNER), "--engine", args.engine,
                   "--boundary", args.boundary, *flags]
        started = time.time()
        summary_path = ROOT / "docs/stage9-evidence/gpu-coverage-audit/geometry-presets/targeted-summary.json"
        summary_path.unlink(missing_ok=True)
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                                timeout=2400, check=False)
        summary = json.loads(summary_path.read_text(encoding="utf-8")) if result.returncode == 0 and summary_path.is_file() else {"rows": []}
        stem = evidence_dir / f"{index:04d}-{label}"
        runner_summary_path = stem.with_suffix(".runner-summary.json")
        if result.returncode == 0 and summary_path.is_file():
            runner_summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        else:
            runner_summary_path = None
        stem.with_suffix(".stdout.log").write_text(result.stdout, encoding="utf-8")
        stem.with_suffix(".stderr.log").write_text(result.stderr, encoding="utf-8")
        record = {
            "label": label, "arguments": flags, "runnerReturnCode": result.returncode,
            "durationSeconds": round(time.time() - started, 3), "rowCount": len(summary.get("rows", [])),
            "runnerSummary": str(runner_summary_path.relative_to(ROOT)) if runner_summary_path else None,
            "stdout": str(stem.with_suffix(".stdout.log").relative_to(ROOT)),
            "stderr": str(stem.with_suffix(".stderr.log").relative_to(ROOT)),
        }
        invocations.append(record)
        for row in summary.get("rows", []):
            cases.append({**row, "invocation": label})
        print(json.dumps({"label": label, "returnCode": result.returncode,
                          "rows": [row.get("status") for row in summary.get("rows", [])]}, ensure_ascii=False),
              flush=True)

    if args.phase in ("builtins", "all"):
        for index, substrate in enumerate(materials):
            deposit = materials[(index + 1) % len(materials)]
            mask = materials[index]
            mask_substrate = materials[(index + 1) % len(materials)]
            mask_deposit = materials[(index + 2) % len(materials)]
            dims = ["--dimension", "2", "--dimension", "3"]
            invoke(f"plane-material-{index:02d}", ["--geometry", "plane", *dims,
                "--model", "singleParticle", "--model", "multiParticle",
                "--operation", "etch", "--operation", "deposit",
                "--substrate-material", substrate, "--deposit-material", deposit,
                "--single-material-rates", "--multi-material-rates", "--multi-material-sticking"])
            invoke(f"trench-material-{index:02d}", ["--geometry", "trench", *dims,
                "--model", "singleParticle", "--model", "multiParticle",
                "--operation", "etch", "--operation", "deposit", "--mask-height", "0.6",
                "--substrate-material", mask_substrate, "--mask-material", mask,
                "--deposit-material", mask_deposit, "--single-material-rates",
                "--multi-material-rates", "--multi-material-sticking"])

        if args.include_fin:
            for index, material in enumerate(materials):
                other = materials[(index + 1) % len(materials)]
                invoke(f"fin-single-bulk-{index:02d}", ["--geometry", "fin", *["--dimension", "2", "--dimension", "3"],
                    "--model", "singleParticle", "--operation", "etch", "--substrate-material", material,
                    "--single-material-rates"])
                invoke(f"fin-multi-mask-{index:02d}", ["--geometry", "fin", "--dimension", "3", "--fin-mode", "direct",
                    "--fin-construction", "boolean", "--model", "multiParticle", "--operation", "etch",
                    "--mask-height", "0.6", "--substrate-material", material, "--mask-material", other,
                    "--multi-material-rates", "--multi-material-sticking"])

    if args.phase in ("holes", "all"):
        for index, mask in enumerate(materials):
            substrate = materials[(index + 1) % len(materials)]
            deposit = materials[(index + 2) % len(materials)]
            invoke(f"hole-material-{index:02d}", ["--geometry", "hole", "--dimension", "3",
                "--model", "singleParticle", "--model", "multiParticle",
                "--operation", "etch", "--operation", "deposit", "--mask-height", "0.6",
                "--substrate-material", substrate, "--mask-material", mask, "--deposit-material", deposit,
                "--single-material-rates", "--multi-material-rates", "--multi-material-sticking"])

    if args.phase in ("fins", "all"):
        for index, substrate in enumerate(materials):
            deposit = materials[(index + 1) % len(materials)]
            mask = materials[(index + 2) % len(materials)]
            masked_substrate = materials[(index + 3) % len(materials)]
            masked_deposit = materials[(index + 4) % len(materials)]
            dimensions = ["--dimension", "2", "--dimension", "3"]
            for construction in ("native", "boolean"):
                construction_flags = ["--fin-construction", construction]
                for masked in (False, True):
                    role_flags = (["--mask-height", "0.6", "--mask-material", mask,
                                   "--substrate-material", masked_substrate,
                                   "--deposit-material", masked_deposit] if masked else
                                  ["--substrate-material", substrate, "--deposit-material", deposit])
                    role_tag = "masked" if masked else "bulk"
                    invoke(f"fin-{construction}-{role_tag}-{index:02d}", ["--geometry", "fin",
                        *dimensions, "--model", "singleParticle", "--model", "multiParticle",
                        "--operation", "etch", "--operation", "deposit", *construction_flags, *role_flags,
                        "--single-material-rates", "--multi-material-rates", "--multi-material-sticking"])

    if args.phase in ("custom", "all"):
        dims = ["--dimension", "2", "--dimension", "3"]
        for native_id in (177, 240):
            for model, rate_flags in (("singleParticle", ["--single-material-rates"]),
                                      ("multiParticle", ["--multi-material-rates", "--multi-material-sticking"])):
                if native_id == 240:
                    rate_flags = [*rate_flags, "--all-custom-material-tables"]
                invoke(f"custom-{native_id}-{model}-bulk", ["--geometry", "plane", *dims,
                    "--model", model, "--operation", "etch", "--custom-material-id", str(native_id), *rate_flags])
        invoke("custom-177-single-deposit", ["--geometry", "plane", *dims, "--model", "singleParticle",
            "--operation", "deposit", "--custom-material-id", "177", "--custom-material-role", "deposit"])
        invoke("custom-240-multi-mask", ["--geometry", "trench", *dims, "--model", "multiParticle",
            "--operation", "etch", "--mask-height", "0.6", "--custom-material-id", "240",
            "--custom-material-role", "mask", "--multi-material-rates", "--multi-material-sticking"])

    expected = (len(materials) * (2 + 2 + 2 + 2 + 2 + 2 + 4)
                if args.phase in ("builtins", "all") else 0)
    expected += (len(materials) * 4 if args.include_fin and args.phase in ("builtins", "all") else 0)
    expected += len(materials) * 4 if args.phase in ("holes", "all") else 0
    expected += len(materials) * 48 if args.phase in ("fins", "all") else 0
    expected += 12 if args.phase in ("custom", "all") else 0
    result = {
        "schemaVersion": 1, "checkedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "engine": args.engine, "installedVersions": {"viennaps": "4.6.2", "viennals": "5.8.5"},
        "phase": args.phase, "builtInMaterials": materials, "customNativeIdEdges": [177, 240],
        "expectedCaseCount": expected, "caseCount": len(cases), "invocations": invocations, "rows": cases,
        "passed": len(cases) == expected and all(row.get("status") == "verified" for row in cases)
                  and all(row.get("runnerReturnCode") == 0 for row in invocations),
    }
    boundary_tag = "" if args.boundary == "reflective" else f"-{args.boundary}"
    summary = EVIDENCE / (f"summary-{args.phase}{'-fin' if args.include_fin else ''}"
                          f"{boundary_tag}-{args.engine.lower()}.json")
    summary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"summary": str(summary.relative_to(ROOT)), "passed": result["passed"],
                      "caseCount": len(cases), "expectedCaseCount": expected}, ensure_ascii=False), flush=True)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
