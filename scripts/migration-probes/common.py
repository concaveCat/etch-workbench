"""Shared helpers for bounded ViennaPS probes (stdlib only)."""
import json


def mesh_stats(mesh, axis=2):
    """Summarize a surface mesh in 2D (line cells) or 3D (triangle cells).

    ``axis`` is the coordinate index of the open/depth direction: 2 for 3D z,
    1 for 2D y.
    """
    pts = list(mesh.getNodes())
    tris = list(mesh.getTriangles())
    lines = list(mesh.getLines())
    cells, kind = (tris, "triangles") if tris else (lines, "lines")
    cd = mesh.getCellData()
    ids = None
    for i in range(cd.getScalarDataSize()):
        if str(cd.getScalarDataLabel(i)) == "MaterialIds":
            ids = [int(x) for x in cd.getScalarData(i)]
    out = {"points": len(pts), kind: len(cells), "kind": kind, "materials": {}}
    if not pts or not cells or ids is None or len(ids) != len(cells):
        raise RuntimeError(f"invalid surface mesh: pts={len(pts)} cells={len(cells)} ids={None if ids is None else len(ids)}")
    for cell, mid in zip(cells, ids):
        vs = [pts[i] for i in cell]
        a = [v[axis] for v in vs]
        x = [v[0] for v in vs]
        rec = out["materials"].setdefault(mid, {"cells": 0, "amin": 1e9, "amax": -1e9, "xmin": 1e9, "xmax": -1e9})
        rec["cells"] += 1
        rec["amin"] = min(rec["amin"], min(a)); rec["amax"] = max(rec["amax"], max(a))
        rec["xmin"] = min(rec["xmin"], min(x)); rec["xmax"] = max(rec["xmax"], max(x))
    # 3D probe compatibility aliases
    if kind == "triangles":
        for rec in out["materials"].values():
            rec["tris"] = rec["cells"]; rec["zmin"] = rec["amin"]; rec["zmax"] = rec["amax"]
    return out


def write(path, payload):
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
