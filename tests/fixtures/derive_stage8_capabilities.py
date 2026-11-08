"""Derive exact Stage 8 capability rows only from the 28 direct comparisons."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
EVIDENCE = ROOT / "docs" / "stage8-evidence" / "product-worker"
OUTPUT = ROOT / "docs" / "stage8-evidence" / "capability-matrix.json"


def main() -> int:
    summary_path = EVIDENCE / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    expected = {(model, dimension) for model in (
        "sf6o2", "cf4o2", "hbro2", "sf6c4f8", "fluorocarbon", "ionBeam", "wetEtch",
        "teosDeposition", "teospecvd", "ald", "oxidation", "oxideRegrowth", "selectiveEpitaxy", "neutralTransport"
    ) for dimension in (2, 3)}
    actual = {(row.get("model"), row.get("dimension")) for row in summary.get("rows", [])}
    if not summary.get("passed") or actual != expected or len(summary.get("rows", [])) != 28:
        raise SystemExit("Stage 8 matrix is not a complete successful 14-model × 2-dimension run")
    # The catalog is Python data; import it from the installed workbench.
    from backend.specialized_models import specialized_catalog
    specs = {row["id"]: row for row in specialized_catalog()["models"]}
    rows = []
    for result in summary["rows"]:
        model, dimension = result["model"], result["dimension"]
        comparisons = result.get("directReference", {}).get("comparisons", {})
        if result.get("status") != "succeeded" or not comparisons:
            raise SystemExit(f"missing successful direct reference for {model} {dimension}D")
        if any(not value for group in comparisons.values() for value in group.values()):
            raise SystemExit(f"a mesh/material comparison failed for {model} {dimension}D")
        expected_engine = "CPU_DISK" if specs[model]["usesFlux"] else "not-applicable"
        execution = result.get("actualExecution") or []
        if len(execution) != 1 or execution[0].get("actualEngine") != expected_engine:
            raise SystemExit(f"native execution evidence is missing for {model} {dimension}D")
        if model == "oxideRegrowth" and comparisons.get("nativeCellSet") != {"numberOfCells": True, "fields": True}:
            raise SystemExit("OxideRegrowth DenseCellSet comparison is required")
        detail = EVIDENCE / f"{model}-{dimension}d.json"
        direct = ROOT / result["directReference"]["file"]
        guard = EVIDENCE / f"{model}-{dimension}d.final.guard.json"
        direct_guard = EVIDENCE / f"{model}-{dimension}d.final.direct.guard.json"
        rows.append({
            "dimension": dimension,
            "nativeModel": model,
            "operation": specs[model]["operation"],
            "boundaryXY": "reflective",
            "engine": expected_engine,
            "status": "verified",
            "evidence": {
                "productWorker": str(detail.relative_to(ROOT)),
                "productGuard": str(guard.relative_to(ROOT)),
                "directReference": str(direct.relative_to(ROOT)),
                "directGuard": str(direct_guard.relative_to(ROOT)),
                "productWorkerSha256": hashlib.sha256(detail.read_bytes()).hexdigest(),
                "directReferenceSha256": hashlib.sha256(direct.read_bytes()).hexdigest(),
                "viennaps": summary["viennaps"],
                "viennals": summary["viennals"],
                "comparisons": comparisons,
            },
        })
    document = {"schemaVersion": 1, "generatedFrom": str(summary_path.relative_to(ROOT)),
                "viennaps": summary["viennaps"], "viennals": summary["viennals"], "rows": rows}
    if document["viennaps"] != importlib.metadata.version("viennaps") or document["viennals"] != importlib.metadata.version("viennals"):
        raise SystemExit("acceptance evidence version differs from the installed ViennaPS / ViennaLS")
    OUTPUT.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {OUTPUT.relative_to(ROOT)} with {len(rows)} evidence-backed combinations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
