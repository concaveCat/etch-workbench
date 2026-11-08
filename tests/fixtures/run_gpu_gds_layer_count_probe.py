"""Bounded GPU_DISK coverage for the maximum 64 GDS material mappings."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import struct
import time
from pathlib import Path
from typing import Any

import run_gpu_gds_coverage as gds
from backend.gds import parse_gds

ROOT = gds.ROOT
EVIDENCE = ROOT / "docs/stage9-evidence/gpu-coverage-audit/gds-layer-count/grid-aligned-0.1"
TEMPLATE = ROOT / "docs/stage5-evidence/two-structure-two-layer.gds"
BUILTINS = [row["name"] for row in json.loads((ROOT / "docs/materials-catalog.json").read_text())["materials"]]


def record(kind: int, data_type: int, payload: bytes = b"") -> bytes:
    if data_type == 0x06 and len(payload) % 2:
        payload += b"\0"
    return struct.pack(">HBB", len(payload) + 4, kind, data_type) + payload


def make_gds(layer_count: int = 64) -> bytes:
    parsed = parse_gds(TEMPLATE.read_bytes())
    timestamp = struct.pack(">12h", 2026, 1, 1, 0, 0, 0, 2026, 1, 1, 0, 0, 0)
    structure = record(0x05, 0x02, timestamp) + record(0x06, 0x06, b"LAYERS64")
    for index in range(layer_count):
        layer = index + 3
        # Distinct 0.3um-wide strips at 0.2um grid-aligned gaps. The full
        # 31.8um geometry fits the allowed 40um x domain at gridDelta=0.1um.
        x0, x1, y0, y1 = index * 500, index * 500 + 300, 0, 1500
        points = (x0, y0, x1, y0, x1, y1, x0, y1, x0, y0)
        structure += (record(0x08, 0x00) + record(0x0D, 0x02, struct.pack(">h", layer))
                      + record(0x0E, 0x02, struct.pack(">h", 0))
                      + record(0x10, 0x03, struct.pack(">10i", *points))
                      + record(0x11, 0x00))
    structure += record(0x07, 0x00)
    data = TEMPLATE.read_bytes()
    result = data[:parsed.prefix_end] + structure + data[parsed.suffix_start:]
    metadata = parse_gds(result)
    expected = list(range(3, 3 + layer_count))
    if metadata.structure_names() != ["LAYERS64"] or metadata.effective_layers("LAYERS64") != expected:
        raise RuntimeError("synthetic GDS mapping-count geometry is malformed")
    return result


def mappings_for(dimension: int, role: str) -> list[dict[str, Any]]:
    film_pool = [name for name in BUILTINS if name not in {"Mask", "Si3N4", "Al2O3"}]
    mask_pool = [name for name in BUILTINS if name not in {"Si3N4", "Al2O3"}]
    rows = []
    for index in range(64):
        is_mask = dimension == 3 and (role == "mask" or (role == "mixed" and index >= 32))
        pool = mask_pool if is_mask else film_pool
        row: dict[str, Any] = {"layer": index + 3, "material": pool[index % len(pool)]}
        if dimension == 3:
            row.update({"baseHeight": 0.2, "height": 0.8, "mask": is_mask})
        rows.append(row)
    return rows


def main() -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    source = EVIDENCE / "sixty-four-layer-mapping.gds"
    source.write_bytes(make_gds())
    asset_id = hashlib.sha256(source.read_bytes()).hexdigest()
    gds.GDS_SOURCE = source
    gds.ASSET_ID = asset_id
    cases = []
    # The 64-mapping maximum is crossed with both dimensions, both particle
    # process wrappers, both operations, both boundaries, and each available
    # 3D material role profile. All native work remains bounded.
    profiles = [(2, "film"), (3, "film"), (3, "mask"), (3, "mixed")]
    for dimension, role in profiles:
        for boundary in ("reflective", "periodic"):
            for model in ("singleParticle", "multiParticle"):
                for operation in ("etch", "deposit"):
                    layer_mappings = mappings_for(dimension, role)
                    case = gds.run_case(
                        dimension, model, operation, boundary, role in {"mask", "mixed"},
                        deposit_material="Si3N4" if dimension == 2 else "Al2O3",
                        material_tables=model == "multiParticle", label=f"64-mappings-{role}",
                        evidence_root=EVIDENCE, asset_id=asset_id, structure="LAYERS64",
                        layer_mappings=layer_mappings,
                        position_um=[0.0, 0.0, 0.0],
                        domain_x_extent_um=40.0, geometry_unit_scale=1.0,
                        domain_grid_delta_um=0.1)
                    cases.append(case)
    summary = {
        "schemaVersion": 1,
        "checkedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "engine": "GPU_DISK",
        "installedVersions": {"viennaps": importlib.metadata.version("viennaps"),
                              "viennals": importlib.metadata.version("viennals")},
        "assetId": asset_id,
        "geometry": "64 disjoint grid-aligned GDS strips mapped to 64 material level sets",
        "mappingCount": 64,
        "expectedCaseCount": 32,
        "caseCount": len(cases),
        "passed": len(cases) == 32 and all(row.get("status") == "verified" for row in cases),
        "rows": cases,
    }
    path = EVIDENCE / "summary.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"summary": str(path.relative_to(ROOT)), "passed": summary["passed"],
                      "caseCount": summary["caseCount"], "expectedCaseCount": 32}, ensure_ascii=False))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
