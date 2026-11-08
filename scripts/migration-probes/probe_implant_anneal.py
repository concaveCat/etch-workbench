"""Bounded probe: minimal official-style IonImplantation + Anneal (4.7.0)."""
import json, sys
import viennaps as ps

from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
(ROOT / "var" / "probes").mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT / "scripts" / "migration-probes"))
from common import write

ps.setDimension(2)
ps.Logger.setLogLevel(ps.LogLevel.ERROR)
ps.setNumThreads(1)

gd = 0.5
depth = 60.0
top = 10.0
oxide = 2.0
bounds = [-10.0, 10.0, -depth, top + oxide]
bc = [ps.BoundaryType.REFLECTIVE_BOUNDARY, ps.BoundaryType.INFINITE_BOUNDARY]
domain = ps.Domain(bounds, bc, gd)
ps.MakePlane(domain, height=-depth, material=ps.Material.Si).apply()
ps.MakePlane(domain, height=0.0, material=ps.Material.Si, addToExisting=True).apply()
ps.MakePlane(domain, height=oxide, material=ps.Material.SiO2, addToExisting=True).apply()
domain.generateCellSet(top, ps.Material.Air, True)
domain.getCellSet().buildNeighborhood()

out = {"viennaps": ps.__version__, "viennals": ps.ls.version}

# ---- manual (analytic) implant model, values from examples/ionImplantation/config.txt
p = ps.PearsonIVParameters()
p.mu, p.sigma, p.beta, p.gamma = 31.0, 11.0, 1.15, 6.2
head = ps.ImplantPearsonIV(p, 0.0, 5.0)
damage = ps.ImplantDamageHobler(26.9, 18.44, 24.4, 142.9, 13.2, -0.02)
implant = ps.IonImplantation()
implant.setImplantModel(head)
implant.setDamageModel(damage)
implant.setDose(1.0e14)
implant.setTiltAngle(7.0)
implant.setLengthUnit(1e-7)
implant.setDoseControl(ps.ImplantDoseControl.WaferDose)
implant.setMaskMaterials([])
implant.setScreenMaterials([ps.Material.SiO2])
implant.setConcentrationLabel("P_total")
implant.setDamageLabel("P_damage")
implant.setLastDamageLabel("P_damage_last")
ps.Process(domain, implant, 0.0).apply()

cs = domain.getCellSet()
def field(name):
    v = cs.getScalarData(name)
    return None if v is None else {"n": len(v), "max": max(v), "sum": sum(v)}
out["post_implant"] = {k: field(k) for k in ("P_total", "P_damage")}
out["post_implant"]["labels"] = list(cs.getScalarDataLabels())

# ---- anneal: 2-step schedule (ramp + soak), manual Arrhenius values
anneal = ps.Anneal()
anneal.setTemperatureSchedule([2.0, 2.0], [873.15, 1323.15, 1323.15])
anneal.setArrheniusParameters(1.23e13, 3.566)
anneal.setMode(ps.AnnealMode.GaussSeidel)
anneal.setImplicitSolverOptions(200, 1e-6, 1.0)
anneal.setDiffusionMaterials([ps.Material.Si])
anneal.setBlockingMaterials([ps.Material.Mask, ps.Material.SiO2])
anneal.setSpeciesLabel("P_total")
anneal.setActiveLabel("P_active")
anneal.enableDefectCoupling(True)
anneal.setDamageLabels("P_damage", "P_damage_last")
anneal.setDefectLabels("P_interstitial", "P_vacancy")
anneal.setDefectDiffusivities(9.242174584051406e8, 4.1788570753803325e5)
anneal.setDefectReactionRates(0.0, 0.0, 0.0)
anneal.setDefectPartition(1.0, 0.0)
anneal.enableSolidActivation(True)
anneal.setSolidSolubilityArrhenius(1.521e2, 0.7102)
ps.Process(domain, anneal, 0.0).apply()
anneal.applyActivation(domain)

out["post_anneal"] = {k: field(k) for k in
                      ("P_total", "P_active", "P_damage", "P_interstitial", "P_vacancy")}
out["post_anneal"]["labels"] = list(cs.getScalarDataLabels())
cs.writeVTU(str(ROOT / "var" / "probes") + "/implant_post_anneal.vtu")

imp = out["post_implant"]["P_total"]; tot = out["post_anneal"]["P_total"]; act = out["post_anneal"]["P_active"]
out["checks"] = {
    "implant_produced_dopant": imp is not None and imp["max"] > 0.0,
    "implant_produced_damage": (out["post_implant"]["P_damage"] or {}).get("max", 0) > 0.0,
    "anneal_produced_active": act is not None and act["max"] > 0.0,
    "anneal_preserved_total_dose": tot is not None and imp is not None and abs(tot["sum"] - imp["sum"]) / max(imp["sum"], 1e-30) < 1e-4,
    "anneal_activated_fraction_reasonable": act is not None and tot is not None and 0.0 < act["sum"] / max(tot["sum"], 1e-30) <= 1.0,
    "anneal_wrote_defect_fields": (out["post_anneal"]["P_interstitial"] or {}).get("max", 0) > 0.0,
}
out["status"] = "ok" if all(out["checks"].values()) else "check-failed"
write(str(ROOT / "var" / "probes") + "/implant_anneal.json", out)
print(json.dumps({"status": out["status"], "checks": out["checks"],
                  "post_implant": {k: out["post_implant"][k] for k in ("P_total", "P_damage")},
                  "post_anneal": {k: out["post_anneal"][k] for k in
                                  ("P_total", "P_active", "P_damage", "P_interstitial", "P_vacancy")}}))
