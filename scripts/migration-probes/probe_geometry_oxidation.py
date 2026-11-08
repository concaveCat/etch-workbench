"""Bounded probe: GDS, MakeStack, Planarize, DenseCellSet scalars, Oxidation.

Each stage reports separately; the process exits non-zero only if a stage
raises. The JSON record carries per-stage measurements and checks.
"""
import json, sys, time, traceback
import viennaps as ps

from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
(ROOT / "var" / "probes").mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT / "scripts" / "migration-probes"))
from common import mesh_stats, write

ps.setDimension(2)
ps.Logger.setLogLevel(ps.LogLevel.ERROR)
ps.setNumThreads(1)

out = {"viennaps": ps.__version__, "viennals": ps.ls.version, "stages": {}, "errors": {}}
T0=time.monotonic()
def mark(name):
    print(f"[{time.monotonic()-T0:6.2f}s] {name}", flush=True)
BC = [ps.BoundaryType.REFLECTIVE_BOUNDARY, ps.BoundaryType.INFINITE_BOUNDARY]  # open top required by MakePlane

mark("gds start")
# ---------------- 1. GDS reader -> layer -> domain mask -> Planarize ----------
try:
    gds = ps.GDSGeometry(0.05, BC)
    ps.GDSReader(gds, "/root/ViennaPS/examples/GDSReader/myTest.gds").apply()
    bounds = list(gds.getBounds())
    layers = sorted(gds.getAllLayers())
    layer0 = gds.layerToLevelSet(0)
    dom = ps.Domain(bounds[:4], BC, 0.05)
    ps.MakePlane(dom, 0.0, ps.Material.Si).apply()
    dom.insertNextLevelSetAsMaterial(layer0, ps.Material.Mask)
    pre = mesh_stats(dom.getSurfaceMesh(), axis=1)
    ps.Planarize(dom, 0.1).apply()
    post = mesh_stats(dom.getSurfaceMesh(), axis=1)
    mask_id = int(ps.Material.Mask)
    out["stages"]["gds"] = {
        "bounds": bounds, "layers": layers, "structures": gds.getNumberOfStructures(),
        "pre_planarize": pre, "post_planarize": post,
    }
    out["checks_gds"] = {"gds_loaded": gds.getNumberOfStructures() > 0 and len(layers) > 0,
                         "gds_mask_inserted": mask_id in pre["materials"],
                         "planarize_ran": post["lines"] > 0}
except Exception as e:
    out["errors"]["gds"] = traceback.format_exc()[-2000:]
    out["checks_gds"] = {"gds_loaded": False, "gds_mask_inserted": False, "planarize_ran": False}

mark("gds done")
# ---------------- 2. MakeStack -------------------------------------------------
try:
    st = ps.Domain(gridDelta=0.05, xExtent=2.0, yExtent=0.6)
    ps.MakeStack(domain=st, numLayers=2, layerHeight=0.1, substrateHeight=0.2,
                 trenchWidth=0.3, maskHeight=0.05).apply()
    m = mesh_stats(st.getSurfaceMesh(), axis=1)
    out["stages"]["make_stack"] = m
    out["checks_stack"] = {"stack_built": m["lines"] > 0, "stack_multiple_materials": len(m["materials"]) >= 3}
except Exception as e:
    out["errors"]["make_stack"] = traceback.format_exc()[-2000:]
    out["checks_stack"] = {"stack_built": False, "stack_multiple_materials": False}

mark("stack done")
# ---------------- 3. DenseCellSet scalar data ----------------------------------
try:
    cd = ps.Domain(gridDelta=0.05, xExtent=1.0, yExtent=1.0)
    ps.MakePlane(cd, -1.0, ps.Material.Si).apply()
    ps.MakePlane(cd, 0.0, ps.Material.Si, addToExisting=True).apply()
    cd.generateCellSet(0.2, ps.Material.Air, True)
    cs = cd.getCellSet()
    n = cs.getNumberOfCells()
    cs.addScalarData("probe", 0.0)
    cs.setScalarData("probe", [float(i) for i in range(n)])
    got = list(cs.getScalarData("probe"))
    cs.writeVTU(str(ROOT / "var" / "probes") + "/cellset_probe.vtu")
    out["stages"]["cellset"] = {"cells": n, "labels": list(cs.getScalarDataLabels()),
                                "first": got[:3], "last": got[-3:]}
    out["checks_cellset"] = {"set_scalar_roundtrip": got == [float(i) for i in range(n)],
                             "vtu_written": True}
except Exception as e:
    out["errors"]["cellset"] = traceback.format_exc()[-2000:]
    out["checks_cellset"] = {"set_scalar_roundtrip": False, "vtu_written": False}

mark("cellset done")
# ---------------- 4. Oxidation (dry, 1000 C, 0.5 min) --------------------------
try:
    od = ps.Domain(gridDelta=0.05, xExtent=1.0, yExtent=1.0)
    ps.MakePlane(od, 0.0, ps.Material.Si).apply()
    z0 = mesh_stats(od.getSurfaceMesh(), axis=1)["materials"][int(ps.Material.Si)]["amax"]
    model = ps.Oxidation()
    model.setOxidant(ps.OxidantType.DRY)
    model.setTemperature(1000.0)
    model.setPressure(1.0)
    model.setTime(0.5)
    model.setOrientation(ps.SiliconOrientation.Si100)
    model.setGpuMode(ps.GpuMode.Cpu)
    model.setInitialTimeStep(0.01)
    est = float(model.estimatePlanarOxideThickness(0.0))
    mark("oxidation model built")
    ps.Process(od, model, 0.5).apply()
    mark("oxidation applied")
    st2 = mesh_stats(od.getSurfaceMesh(), axis=1)
    z1 = max(v["amax"] for v in st2["materials"].values())
    oxide_id = int(ps.Material.SiO2)
    out["stages"]["oxidation"] = {"z_before": z0, "z_after": z1, "estimate": est,
                                  "surface": st2}
    out["checks_oxidation"] = {"oxide_grew": z1 > z0 + 1e-6,
                               "oxide_material_present": oxide_id in st2["materials"],
                               "estimate_positive": est > 0.0}
except Exception as e:
    out["errors"]["oxidation"] = traceback.format_exc()[-2000:]
    out["checks_oxidation"] = {"oxide_grew": False, "oxide_material_present": False, "estimate_positive": False}

all_checks = {}
for k, v in out.items():
    if k.startswith("checks_"):
        all_checks.update(v)
out["checks"] = all_checks
out["status"] = "ok" if all(all_checks.values()) else "check-failed"
write(str(ROOT / "var" / "probes") + "/geometry_oxidation.json", out)
print(json.dumps({"status": out["status"], "checks": all_checks, "errors": list(out["errors"])}))
if out["errors"]:
    raise SystemExit(3)
