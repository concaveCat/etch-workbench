"""Bounded probe for one native 3D GDS layer conversion."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import viennaps as ps

path = Path(sys.argv[1]).resolve()
layer = int(sys.argv[2])
ps.setDimension(3)
geometry = ps.d3.GDSGeometry(0.25)
geometry.setBoundaryConditions([ps.BoundaryType.REFLECTIVE_BOUNDARY,
                                ps.BoundaryType.REFLECTIVE_BOUNDARY,
                                ps.BoundaryType.INFINITE_BOUNDARY])
ps.d3.GDSReader(geometry, str(path)).apply()
print(json.dumps({"structureCount": int(geometry.getNumberOfStructures()),
                  "layers": sorted(int(value) for value in geometry.getAllLayers()),
                  "bounds": [float(value) for value in geometry.getBounds()]}), flush=True)
level_set = geometry.layerToLevelSet(layer, 0.2, 1.0, False, False)
print(json.dumps({"layer": layer, "pointCount": int(level_set.getNumberOfPoints())}), flush=True)
