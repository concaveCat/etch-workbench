"""Execute the decided native construction and check geometry, not exit status alone."""
import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=HERE / "canonical-acceptance")
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.relative_to(ROOT)
    output.mkdir(parents=True, exist_ok=True)
    # Expected heights: p=protected (5.0); o=open (5.0 - rate * time).
    cases = [
        ("gds", "reflective", "inside", .1, .2, 5., {}),
        ("gds", "reflective", "outside", .1, .2, 5., {}),
        ("gds", "reflective", "inside", .03, .2, 5., {}),
        ("gds", "reflective", "inside", .1, .01, 5., {}),
        ("box", "reflective", "inside", .1, .2, 5., {"0.0,0.0": "p", "-1.5,1.0": "o"}),
        ("box", "reflective", "outside", .1, .2, 5., {"0.0,0.0": "o", "-1.5,1.0": "p"}),
        ("box", "reflective", "inside", .07, .2, 5.07, {"0.0,0.0": "p", "-1.5,1.0": "o"}),
        ("full", "reflective", "inside", .1, .2, 5., {"0.0,0.0": "p", "-1.5,1.0": "p"}),
        ("full", "reflective", "outside", .1, .2, 5., {"empty": True}),
        ("full", "periodic", "inside", .1, .2, 5., {"0.0,0.0": "p", "2.35,0.1": "p"}),
        ("crossing", "reflective", "inside", .1, .2, 5., {"2.35,0.1": "p", "-2.35,0.1": "o"}),
        ("crossing", "periodic", "inside", .1, .2, 5., {"2.35,0.1": "p", "-2.35,0.1": "p", "0.0,0.0": "o"}),
        ("outside", "reflective", "inside", .1, .2, 5., {"empty": True}),
        ("outside", "reflective", "outside", .1, .2, 5., {"0.0,0.0": "p"}),
        ("outside", "periodic", "inside", .1, .2, 5., {"-1.6,0.1": "p", "0.0,0.0": "o"}),
        ("ring", "reflective", "inside", .1, .2, 5., {"0.0,0.0": "o", "0.75,0.1": "p"}),
        ("ring", "reflective", "outside", .1, .2, 5., {"0.0,0.0": "p", "0.75,0.1": "o"}),
    ]
    results = []
    for index, (source, boundary, retained, delta, duration, extent, expected) in enumerate(cases):
        name = f"{index:02d}-{source}-{boundary}-{retained}-{delta}-{duration}"
        report_path, guard_path = output / f"{name}.json", output / f"{name}.guard.json"
        command = [sys.executable, str(ROOT / "scripts/bounded_child.py"),
                   "--cwd", str(ROOT), "--record", str(guard_path),
                   "--log-path", str(output / f"{name}.log"),
                   "--timeout-seconds", "45", "--max-rss-mib", "2048", "--",
                   sys.executable, str(HERE / "canonical_regrid_probe.py"), source,
                   str(HERE / "fixtures/buf4-selected.gds"), str(report_path),
                   "--boundary", boundary, "--retained", retained, "--delta", str(delta),
                   "--duration", str(duration), "--extent", str(extent)]
        failures = []
        run = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=55)
        if run.returncode != 0 or not report_path.exists():
            failures.append(f"native process failed: {run.returncode}; {run.stderr[-300:]}")
        else:
            report = json.loads(report_path.read_text())
            if expected.get("empty"):
                if not report.get("emptyInDomain"):
                    failures.append("expected empty mask in target domain")
            elif report.get("emptyInDomain"):
                failures.append("unexpected empty mask")
            else:
                if report["setupBefore"] != report["setupAfter"]:
                    failures.append("simulation domain changed")
                if any(value == 0 for value in report["after"]["levelSetPoints"]):
                    failures.append("material level set vanished")
                for material in ("0", "10", "30"):
                    if report["after"]["materials"].get(material, {}).get("triangles", 0) == 0:
                        failures.append(f"material {material} vanished")
                for point, state in expected.items():
                    target = 5. if state == "p" else 5. - .2 * duration
                    height = report["after"]["filmHeights"].get(point)
                    if height is None or abs(height - target) > 1e-5:
                        failures.append(f"film at {point}: {height}, expected {target}")
        row = {"name": name, "passed": not failures, "failures": failures}
        results.append(row)
        print(("PASS " if row["passed"] else "FAIL ") + name + (": " + str(failures) if failures else ""), flush=True)
    summary = {"cases": len(results), "passed": sum(row["passed"] for row in results), "results": results}
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return 0 if summary["passed"] == summary["cases"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
