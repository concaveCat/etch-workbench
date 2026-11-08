"""Bounded probe: official masked Box/Sphere distribution pattern (FinFET.py).

Mirrors the official examples/emulation/FinFET.py use of
  * a Box geometry inserted as a hard Mask level set,
  * SphereDistribution(radius).addMaskMaterial(Si)  (masked isotropic etch),
  * BoxDistribution(halfAxes).addMaskMaterial(Si)   (masked directional etch),
  * Planarize.
Prints one JSON record with surface-mesh statistics per stage.
"""
import json, sys
import viennaps as ps

from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
(ROOT / "var" / "probes").mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT / "scripts" / "migration-probes"))
from common import mesh_stats, write

ps.setDimension(3)
ps.Logger.setLogLevel(ps.LogLevel.ERROR)
ps.setNumThreads(1)

out = {"viennaps": ps.__version__, "viennals": ps.ls.version, "stages": {}}
box_mask_id = int(ps.Material.Mask)
si_id = int(ps.Material.Si)

gd = 0.5
bounds = [0.0, 20.0, 0.0, 20.0, 0.0, 20.0]
bcs = [ps.BoundaryType.REFLECTIVE_BOUNDARY, ps.BoundaryType.REFLECTIVE_BOUNDARY,
       ps.BoundaryType.INFINITE_BOUNDARY]
domain = ps.Domain(bounds, bcs, gd)
ps.MakePlane(domain, 10.0, ps.Material.Si).apply()

mask = ps.LevelSet(domain.getGrid())
ps.MakeGeometry(mask, ps.Box([5.0, -1.0, 9.9], [15.0, 21.0, 20.0])).apply()
domain.insertNextLevelSetAsMaterial(mask, ps.Material.Mask)
out["stages"]["mask"] = mesh_stats(domain.getSurfaceMesh())

# addMaskMaterial() lists *protected* materials: protect the hard mask, etch Si.
dist = ps.SphereDistribution(radius=-1.5)
dist.addMaskMaterial(ps.Material.Mask)
ps.Process(domain, dist, 0.0).apply()
sphere = mesh_stats(domain.getSurfaceMesh())
out["stages"]["masked_sphere_etch"] = sphere

dist2 = ps.BoxDistribution(halfAxes=[-gd, -gd, -2.0])
dist2.addMaskMaterial(ps.Material.Mask)
ps.Process(domain, dist2, 0.0).apply()
box = mesh_stats(domain.getSurfaceMesh())
out["stages"]["masked_box_etch"] = box

ps.Planarize(domain, 10.0).apply()
st = mesh_stats(domain.getSurfaceMesh())
out["stages"]["planarize"] = st

si_sphere = sphere["materials"].get(si_id)
mk_sphere = sphere["materials"].get(box_mask_id)
si_box = box["materials"].get(si_id)
mk_box = box["materials"].get(box_mask_id)
out["checks"] = {
    "mask_present": mk_box is not None and mk_box["tris"] > 0,
    "sphere_etched_si": si_sphere is not None and si_sphere["zmin"] < 9.0,
    "sphere_preserved_mask_top": mk_sphere is not None and mk_sphere["zmax"] >= 19.9,
    "box_etched_si_deeper": si_box is not None and si_box["zmin"] <= si_sphere["zmin"],
    "box_preserved_mask_top": mk_box is not None and mk_box["zmax"] >= 19.9,
    "surface_nonempty": st["triangles"] > 0,
}
out["status"] = "ok" if all(out["checks"].values()) else "check-failed"
write(str(ROOT / "var" / "probes") + "/masked_distributions.json", out)
print(json.dumps({k: out[k] for k in ("status", "checks", "stages")}))
