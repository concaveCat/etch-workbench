"""Compare the product GDS Domain builder with the direct-reference builder."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import shutil
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "worker"))

import viennaps as ps
import mesh3d
import simulate
from stage5_geometry_reference import build_domain as direct_build_gds


def signature(domain: Any, dimension: int) -> dict[str, Any]:
    mapping = domain.getMaterialMap()
    mesh = domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=0.01)
    data = mesh.getCellData()
    material_ids = None
    for index in range(data.getScalarDataSize()):
        if str(data.getScalarDataLabel(index)) == "MaterialIds":
            material_ids = [int(value) for value in data.getScalarData(index)]
    if material_ids is None:
        raise RuntimeError("native GDS surface mesh has no MaterialIds")
    return {
        "points": [[float(value) for value in row] for row in mesh.getNodes()],
        "cells": [[int(value) for value in row] for row in
                  (mesh.getLines() if dimension == 2 else mesh.getTriangles())],
        "materialIds": material_ids,
        "materialIdsByLevelSetIndex": [int(mapping.getMaterialIdAtIdx(index))
                                        for index in range(mapping.size())],
    }


def main() -> int:
    project_path, source_path, output_path, scratch = map(Path, sys.argv[1:5])
    order = sys.argv[5] if len(sys.argv) > 5 else "product-first"
    project = json.loads(project_path.read_text(encoding="utf-8"))
    source_path = source_path.resolve()
    scratch.mkdir(parents=True, exist_ok=True)
    worker_dir = scratch / "worker"
    direct_dir = scratch / "direct"
    worker_dir.mkdir(exist_ok=True)
    direct_dir.mkdir(exist_ok=True)
    worker_source = worker_dir / "source.gds"
    shutil.copyfile(source_path, worker_source)
    if hashlib.sha256(worker_source.read_bytes()).hexdigest() != project["geometry"]["assetId"]:
        raise RuntimeError("worker GDS source hash does not match project")

    dimension = int(project["dimension"])
    def make_product() -> tuple[dict[str, Any], dict[str, Any]]:
        ps.setDimension(dimension)
        ps.setNumThreads(int(project["solver"]["cpuThreads"]))
        simulate.configure_native_materials(ps, [])
        mesh3d.configure_material_catalog(simulate.MATERIAL_ROWS)
        product_domain = mesh3d.make_domain(ps, project)
        product_readback = simulate.make_v3_geometry(
            ps, product_domain, project,
            config={"gdsAssetPath": str(worker_source)}, run_dir=worker_dir)
        return product_readback, signature(product_domain, dimension)

    def make_direct() -> tuple[dict[str, Any], dict[str, Any]]:
        direct_domain, direct_readback = direct_build_gds(project, source_path, direct_dir)
        return direct_readback, signature(direct_domain, dimension)

    if order == "direct-first":
        direct_readback, direct_signature = make_direct()
        product_readback, product_signature = make_product()
    elif order == "direct-entry":
        ps.setDimension(dimension)
        ps.setNumThreads(int(project["solver"]["cpuThreads"]))
        from stage7_multiparticle_reference import register_custom_materials
        register_custom_materials(project.get("materialDefinitions", []))
        direct_readback, direct_signature = make_direct()
        product_readback, product_signature = make_product()
    elif order == "native-registry-entry":
        ps.setDimension(dimension)
        ps.setNumThreads(int(project["solver"]["cpuThreads"]))
        simulate.configure_native_materials(ps, [])
        direct_readback, direct_signature = make_direct()
        product_readback, product_signature = make_product()
    else:
        product_readback, product_signature = make_product()
        direct_readback, direct_signature = make_direct()
    result = {
        "versions": {"viennaps": importlib.metadata.version("viennaps"),
                     "viennals": importlib.metadata.version("viennals")},
        "productReadback": product_readback, "directReadback": direct_readback,
        "productSignature": product_signature, "directSignature": direct_signature,
        "exact": product_signature == direct_signature,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"order": order, "exact": result["exact"], "productPoints": len(product_signature["points"]),
                      "directPoints": len(direct_signature["points"]),
                      "productReadback": product_readback, "directReadback": direct_readback}, ensure_ascii=False))
    return 0 if result["exact"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
