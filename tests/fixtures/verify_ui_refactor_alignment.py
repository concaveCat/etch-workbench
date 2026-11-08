"""Verify the formal UI source tree against the accepted refactor baseline."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
REFERENCE = Path("/root/viennaps-workbench-ui-refactor/frontend/src")
FORMAL = ROOT / "frontend/src"
OUTPUT = ROOT / "docs/stage9-evidence/ui-refactor-alignment/source-tree-comparison.json"
EXPECTED_DIFFERENCES = {
    "App.tsx": "Integrates ViennaPS process controls, representative model/geometry/material GPU capability classes with explicit known-failure gates, GPU UUID selection, and CUDA readback into the reference layout; closes default-open top-bar menus at the 900px mobile breakpoint so they do not cover mobile navigation.",
}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    reference_files = {path.relative_to(REFERENCE).as_posix(): path for path in REFERENCE.rglob("*") if path.is_file()}
    formal_files = {path.relative_to(FORMAL).as_posix(): path for path in FORMAL.rglob("*") if path.is_file()}
    reference_only = sorted(reference_files.keys() - formal_files.keys())
    formal_only = sorted(formal_files.keys() - reference_files.keys())
    comparisons = []
    for relative in sorted(reference_files.keys() & formal_files.keys()):
        reference_hash = digest(reference_files[relative])
        formal_hash = digest(formal_files[relative])
        comparisons.append({
            "path": relative,
            "status": "byte-identical" if reference_hash == formal_hash else "intentional-integration-difference",
            "referenceSha256": reference_hash,
            "formalSha256": formal_hash,
            **({"reason": EXPECTED_DIFFERENCES[relative]} if reference_hash != formal_hash else {}),
        })
    differences = {row["path"] for row in comparisons if row["status"] != "byte-identical"}
    if reference_only or formal_only or differences != set(EXPECTED_DIFFERENCES):
        raise SystemExit(json.dumps({"referenceOnly": reference_only, "formalOnly": formal_only,
                                     "unexpectedDifferences": sorted(differences ^ set(EXPECTED_DIFFERENCES))}, indent=2))
    report = {
        "status": "passed",
        "reference": str(REFERENCE),
        "formal": str(FORMAL),
        "referenceFileCount": len(reference_files),
        "formalFileCount": len(formal_files),
        "referenceOnly": reference_only,
        "formalOnly": formal_only,
        "byteIdenticalCount": len(comparisons) - len(differences),
        "intentionalDifferences": [row for row in comparisons if row["path"] in EXPECTED_DIFFERENCES],
        "files": comparisons,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "files": len(comparisons),
                      "byteIdentical": report["byteIdenticalCount"],
                      "intentionalDifferences": sorted(differences), "output": str(OUTPUT.relative_to(ROOT))},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
