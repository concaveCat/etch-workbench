"""Independent equivalence check for installed native rate-map APIs."""
import json,math
from pathlib import Path
from materials_direct_reference import build,capture
import viennaps as ps

output=[]
for dim in (2,3):
 p={'dimension':dim,'domain':{'gridDelta':.2,'xExtent':4.,**({'xBoundary':'reflective'} if dim==2 else {'yExtent':4.,'boundaryXY':'reflective'})},'solver':{'cpuThreads':1},
    'geometry':{'kind':'stack','baseHeight':0.,'substrateMaterial':'Si','layers':[{'material':'SiO2','thickness':.6,'role':'film'},{'material':'Si3N4','thickness':.6,'role':'mask'}],'pattern':{'kind':'trench','width':1.6,'taperAngle':0.}}}
 for family in ('isotropic','singleParticle'):
  snapshots=[]
  for path in ('map-constructor','setters'):
   domain,_=build(p);reg=ps.MaterialRegistry.instance();signed={'Si':-.1,'SiO2':-.03,'Si3N4':-.02};mapping={reg.getMaterial(k):v for k,v in signed.items()}
   if path=='map-constructor':
    if family=='isotropic':model=ps.IsotropicProcess(materialRates=mapping,defaultRate=0.)
    else:
     model=ps.SingleParticleProcess(materialRates=mapping,stickingProbability=.5,sourceExponent=1.);model.setDefaultRate(0.)
   else:
    if family=='isotropic':model=ps.IsotropicProcess(rate=0.,maskMaterials=[])
    else:model=ps.SingleParticleProcess(rate=0.,stickingProbability=.5,sourceExponent=1.,maskMaterials=[])
    for name,rate in signed.items():model.setMaterialRate(reg.getMaterial(name),rate)
   process=ps.Process(domain,model,.5);process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
   adv=ps.AdvectionParameters();adv.timeStepRatio=.25;process.setParameters(adv)
   if family=='singleParticle':
    ray=ps.RayTracingParameters();ray.raysPerPoint=32;ray.rngSeed=314159;ray.useRandomSeeds=False;process.setParameters(ray)
   process.apply();snapshots.append(capture(domain,dim))
  a,b=snapshots;key='lines' if dim==2 else 'triangles';assert a[key]==b[key];assert a['materialIds']==b['materialIds'];assert a['materialMap']==b['materialMap'];assert len(a['points'])==len(b['points'])
  error=max(math.dist(x,y) for x,y in zip(a['points'],b['points']));assert error<=1e-10
  output.append({'dimension':dim,'model':family,'fallback':0,'rates':signed,'constructorVsSettersMaxError':error,'sameCellsAndMaterialIds':True})
Path('docs/selectivity-evidence/native-rate-map.json').write_text(json.dumps(output,indent=2)+'\n');print(json.dumps(output))
