"""Probe real GPU_DISK processing of multiple GDS layer mappings."""
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
EVIDENCE = ROOT / "docs/stage9-evidence/gpu-coverage-audit/gds-multilayer"
SOURCE_TEMPLATE = ROOT / "docs/stage5-evidence/two-structure-two-layer.gds"


def record(kind: int, data_type: int, payload: bytes = b"") -> bytes:
    if data_type == 0x06 and len(payload) % 2:
        payload += b"\0"
    return struct.pack(">HBB", len(payload) + 4, kind, data_type) + payload


def make_multilayer_gds() -> bytes:
    template = SOURCE_TEMPLATE.read_bytes()
    parsed = parse_gds(template)
    timestamp = struct.pack(">12h", 2026, 1, 1, 0, 0, 0, 2026, 1, 1, 0, 0, 0)
    structure = record(0x05, 0x02, timestamp) + record(0x06, 0x06, b"MULTI")
    for layer, (x0, y0, x1, y1) in ((3, (0, 0, 2000, 1500)),
                                    (4, (2500, 0, 4500, 1500))):
        points = (x0, y0, x1, y0, x1, y1, x0, y1, x0, y0)
        structure += (record(0x08, 0x00) + record(0x0D, 0x02, struct.pack(">h", layer))
                      + record(0x0E, 0x02, struct.pack(">h", 0))
                      + record(0x10, 0x03, struct.pack(">10i", *points))
                      + record(0x11, 0x00))
    structure += record(0x07, 0x00)
    data = template[:parsed.prefix_end] + structure + template[parsed.suffix_start:]
    metadata = parse_gds(data)
    if metadata.structure_names() != ["MULTI"] or metadata.effective_layers("MULTI") != [3, 4]:
        raise RuntimeError("synthetic two-layer GDS did not parse with the expected structure/layers")
    return data


def run() -> dict[str, Any]:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    source = EVIDENCE / "multilayer-two-adjacent-polygons.gds"
    source.write_bytes(make_multilayer_gds())
    asset_id = hashlib.sha256(source.read_bytes()).hexdigest()
    gds.GDS_SOURCE = source
    gds.ASSET_ID = asset_id
    custom_registry = [
        {"name": f"GdsGpuCustom{index:02d}", "id": 177 + index, "customId": index,
         "color": f"#{(0x223344 + index * 101) % 0xFFFFFF:06x}", "conductive": False,
         "description": "two-layer GPU custom material scope"}
        for index in range(64)
    ]
    cases = []
    profiles = {
        2: [
            ("two-film-distinct", [(3, "SiO2", False), (4, "W", False)]),
            ("two-film-same", [(3, "SiO2", False), (4, "SiO2", False)]),
            ("two-film-custom-edges", [(3, "GdsGpuCustom00", False), (4, "GdsGpuCustom63", False)]),
        ],
        3: [
            ("film-mask-distinct", [(3, "SiO2", False), (4, "W", True)]),
            ("two-film-distinct", [(3, "SiO2", False), (4, "W", False)]),
            ("two-mask-distinct", [(3, "SiO2", True), (4, "W", True)]),
            ("film-mask-same", [(3, "SiO2", False), (4, "SiO2", True)]),
            ("two-film-same", [(3, "SiO2", False), (4, "SiO2", False)]),
            ("two-mask-same", [(3, "SiO2", True), (4, "SiO2", True)]),
            ("film-mask-custom-edges", [(3, "GdsGpuCustom00", False), (4, "GdsGpuCustom63", True)]),
        ],
    }
    for dimension in (2, 3):
        for profile_name, profile_layers in profiles[dimension]:
            for boundary in ("reflective", "periodic"):
                for model in ("singleParticle", "multiParticle"):
                    for operation in ("etch", "deposit"):
                        mappings = []
                        for layer, material, is_mask in profile_layers:
                            row: dict[str, Any] = {"layer": layer, "material": material}
                            if dimension == 3:
                                row.update({"baseHeight": 0.25, "height": 0.75, "mask": is_mask})
                            mappings.append(row)
                        masked = any(is_mask for _, _, is_mask in profile_layers)
                        # 2D maps GDS x to the periodic x axis; the two-layer
                        # fixture at unitScale 1.5 spans x=0..6.75 um, so center
                        # it first.  Periodic GDS requires the structure to fit
                        # inside the requested period.
                        row = gds.run_case(
                            dimension, model, operation, boundary, masked,
                            deposit_material="Si3N4" if dimension == 2 else "Al2O3",
                            custom_definitions=custom_registry if "custom-edges" in profile_name else None,
                            material_tables=model == "multiParticle", label=f"two-layer-{profile_name}",
                            evidence_root=EVIDENCE, asset_id=asset_id, structure="MULTI",
                            layer_mappings=mappings,
                            position_um=([-0.25, 0.25, 0.0] if dimension == 3
                                          else [-3.35, 0.1, 0.0]))
                        cases.append(row)
                        print(json.dumps({"name": row["name"], "status": row["status"],
                                          "comparisons": row.get("comparisons")}, ensure_ascii=False), flush=True)
    summary = {
        "schemaVersion": 1, "checkedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "engine": "GPU_DISK", "installedVersions": {
            "viennaps": importlib.metadata.version("viennaps"),
            "viennals": importlib.metadata.version("viennals")},
        "assetId": asset_id, "geometry": "two adjacent rectangles on separate layers in one GDS structure",
        "scope": "2D film/film; 3D film/film, mask/mask, and film/mask; built-in distinct/repeated identities plus native custom IDs 177+240 in mixed roles; reflective/periodic × single/multi-particle × etch/deposit",
        "expectedCaseCount": 80, "caseCount": len(cases),
        "passed": len(cases) == 80 and all(row.get("status") == "verified" for row in cases),
        "rows": cases,
    }
    summary_path = EVIDENCE / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"summary": str(summary_path.relative_to(ROOT)), "passed": summary["passed"],
                      "caseCount": summary["caseCount"], "expectedCaseCount": 80}, ensure_ascii=False))
    return summary


if __name__ == "__main__":
    result = run()
    raise SystemExit(0 if result["passed"] else 1)
