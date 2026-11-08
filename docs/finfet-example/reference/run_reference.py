#!/usr/bin/env python3
"""Run the verbatim native ``FinFET.py`` and record deterministic checkpoints.

The original script is copied next to this runner unchanged. To observe it we
temporarily wrap ``viennaps.d2/d3.Domain.saveSurfaceMesh`` and ``saveVolumeMesh``:
the wrapped method calls the original native writer first (so the run is still
the exact script behavior), then records a canonical digest, counts, material
map and material-cell distribution. With ``--keep-meshes`` the written meshes
are retained; by default they are deleted after recording because the final
volume alone is about 94 MB.

Usage::

    .venv/bin/python docs/finfet-example/reference/run_reference.py \\
        --output-dir var/finfet-reference \\
        --expected docs/finfet-example/expected.json

The committed ``expected.json`` is the parity gate: it contains no timestamps
and is byte-stable for a fixed ViennaPS version and script hash.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import runpy
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import viennaps as ps

sys.path.insert(0, str(Path(__file__).resolve().parent))
from canonical import surface_digest, volume_digest  # noqa: E402

SURFACE_RE = re.compile(r"FinFET_(\d+)\.vtp$")

#: Coordinates are recorded in the script's own native unit. The caller states
#: that unit with ``--coordinate-unit`` so the expected document can describe
#: the exact frame without rescaling any mesh.


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scalar_cell_data(mesh: Any, label: str) -> list[int]:
    data = mesh.getCellData()
    for index in range(data.getScalarDataSize()):
        if str(data.getScalarDataLabel(index)) == label:
            return [int(value) for value in data.getScalarData(index)]
    raise RuntimeError(f"native mesh has no {label!r} cell data")


def material_map(domain: Any) -> list[int]:
    mapping = domain.getMaterialMap()
    return [int(mapping.getMaterialIdAtIdx(index)) for index in range(mapping.size())]


def material_names(material_ids: list[int]) -> dict[str, str]:
    registry = ps.MaterialRegistry.instance()
    return {str(material_id): registry.getName(ps.Material(material_id)) for material_id in material_ids}


def distribution(values: list[int]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        key = str(int(value))
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: int(item[0])))


def bounds(points: Any) -> list[float]:
    array = np.asarray(points, dtype=np.float64)
    return [
        float(array[:, 0].min()),
        float(array[:, 0].max()),
        float(array[:, 1].min()),
        float(array[:, 1].max()),
        float(array[:, 2].min()),
        float(array[:, 2].max()),
    ]


def read_volume(path: Path) -> dict[str, Any]:
    """Read a native VTU and return its canonical summary."""
    mesh = ps.ls.Mesh()
    reader = ps.ls.VTKReader()
    reader.setFileName(str(path))
    reader.setMesh(mesh)
    reader.apply()
    nodes = [[float(value) for value in point] for point in mesh.getNodes()]
    tetras = mesh.getTetras()
    hexas = mesh.getHexas()
    if tetras and hexas:
        raise RuntimeError(f"native volume {path.name} mixes tetrahedra and hexahedra")
    cells = tetras or hexas
    if not cells:
        raise RuntimeError(f"native volume {path.name} has no volume cells")
    cell_type = "tetra" if tetras else "hexa"
    material_ids = scalar_cell_data(mesh, "Material")
    if len(material_ids) != len(cells):
        raise RuntimeError("volume material data is not aligned with its cells")
    return {
        "file": path.name,
        "fileSha256": sha256_file(path),
        "fileBytes": int(path.stat().st_size),
        "nodeCount": len(nodes),
        "cellCount": len(cells),
        "cellType": cell_type,
        "materialCounts": distribution(material_ids),
        "boundsUm": bounds(nodes),
        "digest": volume_digest(nodes, cells, material_ids),
    }


def run(script: Path, output_dir: Path, keep_meshes: bool,
        coordinate_unit: str = "nm") -> tuple[dict[str, Any], float]:
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)
    script = script.resolve()
    script_hash = sha256_file(script)

    surfaces: list[dict[str, Any]] = []
    volumes: list[dict[str, Any]] = []
    original_surface = {}
    original_volume = {}
    for module_name in ("d2", "d3"):
        module = getattr(ps, module_name)
        original_surface[module_name] = module.Domain.saveSurfaceMesh
        original_volume[module_name] = module.Domain.saveVolumeMesh

    def record_surface(domain: Any, filename: str) -> None:
        match = SURFACE_RE.search(filename)
        if match is None:
            raise RuntimeError(f"unexpected surface filename {filename!r}")
        index = int(match.group(1))
        mesh = domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=0.01)
        points = [[float(value) for value in point] for point in mesh.getNodes()]
        triangles = mesh.getTriangles()
        material_ids = scalar_cell_data(mesh, "MaterialIds")
        order = material_map(domain)
        surfaces.append(
            {
                "index": index,
                "file": Path(filename).name,
                "pointCount": len(points),
                "triangleCount": len(triangles),
                "materialMap": order,
                "materialNames": material_names(order),
                "surfaceMaterialCounts": distribution(material_ids),
                "boundsUm": bounds(points),
                "digest": surface_digest(points, triangles, material_ids),
            }
        )

    def record_volume(domain: Any, filename: str) -> None:
        del domain
        path = Path(filename + "_volume.vtu")
        if not path.is_file() or path.stat().st_size <= 0:
            raise RuntimeError(f"native saveVolumeMesh did not produce {path}")
        volumes.append(read_volume(path))

    for module_name in ("d2", "d3"):
        module = getattr(ps, module_name)
        original = original_surface[module_name]

        def surface(self: Any, filename: str, addInterfaces: bool = True, sharpCorners: bool = False,
                    minNodeDistanceFactor: float = 0.01, _original: Any = original) -> None:
            _original(self, filename, addInterfaces, sharpCorners, minNodeDistanceFactor)
            record_surface(self, filename)
            if not keep_meshes:
                Path(filename).unlink(missing_ok=True)

        module.Domain.saveSurfaceMesh = surface
        original = original_volume[module_name]

        def volume(self: Any, filename: str, wrappingLayerEpsilon: float = 0.01,
                   _original: Any = original) -> None:
            _original(self, filename, wrappingLayerEpsilon)
            record_volume(self, filename)
            path = Path(filename + "_volume.vtu")
            if not keep_meshes:
                path.unlink(missing_ok=True)

        module.Domain.saveVolumeMesh = volume

    started = time.monotonic()
    cwd = Path.cwd()
    os.chdir(output_dir)
    try:
        runpy.run_path(str(script), run_name="__main__")
    finally:
        os.chdir(cwd)
        for module_name in ("d2", "d3"):
            module = getattr(ps, module_name)
            module.Domain.saveSurfaceMesh = original_surface[module_name]
            module.Domain.saveVolumeMesh = original_volume[module_name]
    elapsed = time.monotonic() - started

    surfaces.sort(key=lambda item: item["index"])
    if [item["index"] for item in surfaces] != list(range(len(surfaces))):
        raise RuntimeError(f"surface checkpoints are not contiguous: {[item['index'] for item in surfaces]}")
    if len(volumes) != 1:
        raise RuntimeError(f"expected exactly one final volume mesh, got {len(volumes)}")
    expected = {
        "schemaVersion": 1,
        "referenceScript": {
            "file": script.name,
            "sha256": script_hash,
            "lineCount": len(script.read_text(encoding="utf-8").splitlines()),
        },
        "engine": {
            "viennaps": ps.__version__,
            "dimension": 3,
            "threads": 1,
        },
        "domain": {
            "coordinateUnit": coordinate_unit,
            "bounds": ([0.0, 90.0, 0.0, 100.0, 0.0, 70.0] if coordinate_unit == "nm"
                       else [0.0, 0.09, 0.0, 0.1, 0.0, 0.07]),
            "gridDelta": 0.79 if coordinate_unit == "nm" else 0.00079,
            "boundsUm": [0.0, 0.09, 0.0, 0.1, 0.0, 0.07],
            "gridDeltaUm": 0.00079,
            "boundaries": ["reflective", "reflective", "infinite"],
            "unitScaleNmToUm": 0.001 if coordinate_unit == "nm" else 1.0,
        },
        "surfaces": surfaces,
        "volume": volumes[0],
    }
    return expected, elapsed


def main() -> int:
    root = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--script", type=Path, default=Path(__file__).resolve().parent / "FinFET.py")
    parser.add_argument("--output-dir", type=Path, default=root / "var" / "finfet-reference")
    parser.add_argument("--expected", type=Path, default=None)
    parser.add_argument("--keep-meshes", action="store_true", help="keep the 26 VTP files and the final VTU")
    parser.add_argument("--coordinate-unit", choices=("nm", "um"), default="nm",
                        help="native unit of the script (default nm)")
    args = parser.parse_args()

    ps.Logger.setLogLevel(ps.LogLevel.ERROR)
    ps.setNumThreads(1)
    expected, elapsed = run(args.script.resolve(), args.output_dir.resolve(), args.keep_meshes,
                            args.coordinate_unit)
    text = json.dumps(expected, indent=2, sort_keys=False, allow_nan=False) + "\n"
    target = args.expected.resolve() if args.expected is not None else args.output_dir / "expected.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    print(
        f"reference: {len(expected['surfaces'])} surfaces, "
        f"{expected['volume']['nodeCount']} volume nodes, "
        f"{expected['volume']['cellCount']} volume cells, {elapsed:.1f} s",
        file=sys.stderr,
    )
    print(f"expected: {target}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
