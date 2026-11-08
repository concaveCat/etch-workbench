import json
from pathlib import Path
import viennaps as ps
r=ps.MaterialRegistry.instance();out=[]
for dim in [2,3]:
 ps.setDimension(dim);ps.setNumThreads(1)
 for cls in [ps.IsotropicProcess,ps.SingleParticleProcess]:
  for keyword in ['maskMaterials','maskMaterial']:
   try:
    kw={'rate':-.1,keyword:[r.getMaterial('Si3N4'),r.getMaterial('PMMA')]}
    if cls==ps.SingleParticleProcess:kw.update(stickingProbability=.5,sourceExponent=1.)
    model=cls(**kw)
    d=ps.Domain(.2,4.,4.,ps.BoundaryType.REFLECTIVE_BOUNDARY) if dim==3 else ps.Domain(.2, 4., boundary=ps.BoundaryType.REFLECTIVE_BOUNDARY)
    ps.MakePlane(d,height=0.,material=r.getMaterial('Si3N4'),addToExisting=False).apply()
    p=ps.Process(d,model,.1);p.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
    if cls==ps.SingleParticleProcess:
     ray=ps.RayTracingParameters();ray.raysPerPoint=16;ray.rngSeed=42;ray.useRandomSeeds=False;p.setParameters(ray)
    p.apply()
    heights=sorted(set(round(x[dim-1],10) for x in d.getSurfaceMesh(addInterfaces=True,sharpCorners=False,minNodeDistanceFactor=.01).getNodes()))
    out.append({'dim':dim,'model':cls.__name__,'keyword':keyword,'status':'passed','heights':heights})
    break
   except Exception as e:out.append({'dim':dim,'model':cls.__name__,'keyword':keyword,'status':'failed','error':str(e)[:300]})
Path('docs/materials-evidence/multiple-mask-probe.json').write_text(json.dumps(out,indent=2))
print(json.dumps(out),flush=True)
