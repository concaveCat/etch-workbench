import json
from pathlib import Path
from materials_direct_reference import build,capture

results=[]
for dim in [2,3]:
 for kind in (['trench'] if dim==2 else ['trench','hole']):
  for angle in [0.,10.,-10.]:
   pattern={'kind':kind,'taperAngle':angle,'width':1.6} if kind=='trench' else {'kind':kind,'taperAngle':angle,'radius':.8}
   p={'dimension':dim,'domain':{'gridDelta':.2,'xExtent':4.,**({'xBoundary':'reflective'} if dim==2 else {'yExtent':4.,'boundaryXY':'reflective'})},'solver':{'cpuThreads':1},
      'geometry':{'kind':'stack','baseHeight':0.,'substrateMaterial':'Si','layers':[{'material':'SiO2','thickness':.6,'role':'film'},{'material':'Si3N4','thickness':.6,'role':'mask'}],'pattern':pattern}}
   domain,_=build(p);mesh=capture(domain,dim)
   assert mesh['materialMap']==[10,30,16]
   assert set(mesh['materialIds'])=={10,30,16}
   results.append({'dimension':dim,'pattern':pattern,'pointCount':len(mesh['points']),'cells':len(mesh.get('triangles',mesh.get('lines',[]))),'materialMap':mesh['materialMap']})
Path('docs/materials-evidence/stack-pattern-probe.json').write_text(json.dumps(results,indent=2))
print(json.dumps(results))
