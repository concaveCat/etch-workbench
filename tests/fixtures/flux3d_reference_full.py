"""3D CPU/GPU flux comparison fixture; invoke only beneath bounded_child.py."""
import argparse, json, math
from pathlib import Path
import viennaps as ps

PROJ = "/tmp/viennaps-native-multiion-workbench-candidate-2026-10-28/docs/stage9-evidence/gpu-coverage-audit/multiparticle-native-product/3d-project.json"

def mat(name): return ps.MaterialRegistry.instance().getMaterial(name)

def add_species(model, step):
    labels=[]
    for sp in step["species"]:
        label=sp["label"]; labels.append(label)
        if sp["kind"] == "ion":
            model.addIonParticle(sourcePower=float(sp["sourcePower"]), thetaRMin=float(sp["thetaRMin"]), thetaRMax=float(sp["thetaRMax"]), minAngle=float(sp["minAngle"]), B_sp=float(sp["B_sp"]), meanEnergy=float(sp["meanEnergy"]), sigmaEnergy=float(sp["sigmaEnergy"]), thresholdEnergy=float(sp["thresholdEnergy"]), inflectAngle=float(sp["inflectAngle"]), n=int(sp["n"]), label=label)
        else:
            sticking={mat(k):float(v) for k,v in sp.get("stickingProbabilityByMaterial",{}).items()}
            model.addNeutralParticle(sticking,defaultStickingProbability=float(sp["defaultStickingProbability"]),label=label)
    return labels

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--project",default=PROJ); ap.add_argument("--state",required=True); ap.add_argument("--engine",default="CPU_DISK"); ap.add_argument("--rays",type=int,default=96); ap.add_argument("--seed",type=int,default=314159); ap.add_argument("--out",required=True); a=ap.parse_args()
    project=json.loads(Path(a.project).read_text()); step=project["steps"][0]
    ps.Length.setUnit("um"); ps.Time.setUnit("s"); ps.setDimension(3); ps.setNumThreads(1)
    d=project["domain"]; domain=ps.Domain(float(d["gridDelta"]),float(d["xExtent"]),float(d["yExtent"]),ps.BoundaryType.REFLECTIVE_BOUNDARY); ps.Reader(domain,a.state).apply()
    if step["operation"]=="deposit": domain.duplicateTopLevelSet(mat(step["depositMaterial"]))
    model=ps.MultiParticleProcess(); labels=add_species(model,step); model.setRateFunction(lambda fluxes,material:0.0)
    process=ps.Process(domain,model,float(step["duration"])); process.setFluxEngineType(getattr(ps.FluxEngineType,a.engine))
    ray=ps.RayTracingParameters(); ray.raysPerPoint=a.rays; ray.rngSeed=a.seed; ray.useRandomSeeds=False
    rt=project["solver"]["rayTracing"]
    for k in ("diskRadius","ignoreFluxBoundaries","maxBoundaryHits","maxReflections","minNodeDistanceFactor","smoothingNeighbors"):
        if k in rt: setattr(ray,k,rt[k])
    ray.normalizationType=getattr(ps.NormalizationType,rt["normalizationType"]); process.setParameters(ray); mesh=process.calculateFlux(); cd=mesh.getCellData()
    result={"engine":a.engine,"viennapsModulePath":str(Path(ps.__file__).resolve()),"state":a.state,"species":labels,"points":[[float(x) for x in row] for row in mesh.getNodes()]}
    for kind in ("getLines","getTriangles","getTetras","getHexas","getVerticies"):
        try: val=getattr(mesh,kind)()
        except Exception: val=[]
        if val: result["cells_kind"]=kind; result["cells"]=[[int(i) for i in row] for row in val]; break
    for j in range(cd.getScalarDataSize()):
        name=str(cd.getScalarDataLabel(j)); vals=[float(v) for v in cd.getScalarData(j)]; bad=[i for i,v in enumerate(vals) if not math.isfinite(v)]
        result[name]={"values":vals,"nonfinite":len(bad),"badIdx":bad,"min":min((v for v in vals if math.isfinite(v)),default=None),"max":max((v for v in vals if math.isfinite(v)),default=None)}
    Path(a.out).write_text(json.dumps(result,indent=1,allow_nan=False)); print(json.dumps({"engine":a.engine,"module":ps.__file__,**{k:v["nonfinite"] for k,v in result.items() if isinstance(v,dict)}}))

if __name__=="__main__": main()
