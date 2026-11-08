"""Probe ViennaLS intermediate-output path behavior; run via bounded_child.py."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import viennaps as ps


def main() -> None:
    work = Path.cwd()
    requested_path = sys.argv[1]
    ps.setDimension(2)
    ps.setNumThreads(1)
    domain = ps.Domain(0.2, 2.0, ps.BoundaryType.REFLECTIVE_BOUNDARY)
    ps.MakePlane(domain, height=0.0, material=ps.Material.Si, addToExisting=False).apply()
    process = ps.Process(domain, ps.IsotropicProcess(rate=-0.1, maskMaterials=[]), 0.1)
    process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
    params = ps.AdvectionParameters()
    params.velocityOutput = True
    params.calculateIntermediateVelocities = True
    process.setParameters(params)
    process.setIntermediateOutputPath(requested_path)
    process.apply()
    rows = [{"path": str(path.relative_to(work)), "size": path.stat().st_size}
            for path in sorted(work.rglob("*")) if path.is_file()]
    print(json.dumps({"requestedIntermediatePath": requested_path, "cwd": str(work),
                      "outputs": rows, "advectionMetadata": params.toMetaData()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
