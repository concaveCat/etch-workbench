"""Read a saved ViennaPS state and report its native surface topology."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import viennaps as ps

state_path = Path(sys.argv[1]).resolve()
output_path = Path(sys.argv[2]).resolve()
ps.Length.setUnit("um")
ps.Time.setUnit("s")
ps.setDimension(3)
domain = ps.Domain(0.2, 2.4, 2.4, ps.BoundaryType.REFLECTIVE_BOUNDARY)
ps.Reader(domain, str(state_path)).apply()
mesh = domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=0.01)
points = [[float(value) for value in row] for row in mesh.getNodes()]
triangles = [[int(value) for value in row] for row in mesh.getTriangles()]
data = mesh.getCellData()
materials: list[int] = []
for index in range(data.getScalarDataSize()):
    if str(data.getScalarDataLabel(index)) == "MaterialIds":
        materials = [int(value) for value in data.getScalarData(index)]
payload = json.dumps({"points": points, "triangles": triangles, "materials": materials},
                     sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
result = {
    "viennaps": ps.__file__,
    "state": str(state_path),
    "pointCount": len(points),
    "triangleCount": len(triangles),
    "levelSetCount": int(domain.getNumberOfLevelSets()),
    "materialIdsByLevelSetIndex": [int(domain.getMaterialMap().getMaterialIdAtIdx(i))
                                    for i in range(domain.getMaterialMap().size())],
    "materialCellCounts": {str(value): materials.count(value) for value in sorted(set(materials))},
    "surfaceSha256": hashlib.sha256(payload).hexdigest(),
    "points": points,
    "triangles": triangles,
    "materialIds": materials,
}
output_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
print(json.dumps(result), flush=True)
