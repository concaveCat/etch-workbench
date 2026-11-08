"""Native 4.6.2 probe. Only invoke through bounded_child.py."""
import json,sys,time
from pathlib import Path
from importlib.metadata import version
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tests.vtk_legacy import read_legacy_vtk
import viennaps as ps
ps.setDimension(3); ps.setNumThreads(1)
out=ROOT/'var/evidence/g1'; out.mkdir(parents=True,exist_ok=True)
rows=[]
def mesh(d,name):
 m=d.getSurfaceMesh(addInterfaces=True,sharpCorners=False,minNodeDistanceFactor=.01)
 pts=[list(p) for p in m.getNodes()]; tri=[list(t) for t in m.getTriangles()]
 ids=list(m.getCellData().getScalarData('MaterialIds', True))
 d.saveSurfaceMesh(str(out/(name+'.vtk')),addInterfaces=True,sharpCorners=False,minNodeDistanceFactor=.01)
 vtk=read_legacy_vtk(out/(name+'.vtk'))
 assert vtk['cells']==tri and vtk['cellData']['MaterialIds']==ids, ('topology/material mismatch',len(tri),vtk['cellCount'])
 assert len(pts)==len(vtk['points'])
 error=max(abs(a-b) for p,q in zip(pts,vtk['points']) for a,b in zip(p,q))
 assert error<1e-4, error
 assert set(vtk['cellTypes'])=={5} and len(ids)==len(tri) and tri
 bounds=[f(p[a] for p in pts) for a in range(3) for f in (min,max)]
 return {'pointCount':len(pts),'triangleCount':len(tri),'materials':sorted(set(ids)),'bounds':bounds,'gridDelta':d.getGridDelta(),'boundary':[str(x) for x in d.getBoundaryConditions()],'boundingBox':d.getBoundingBox()}
for boundary in ['reflective','periodic']:
 b=ps.BoundaryType.REFLECTIVE_BOUNDARY if boundary=='reflective' else ps.BoundaryType.PERIODIC_BOUNDARY
 for kind in ['plane','trench','hole']:
  for taper in ([0,10,-10] if kind!='plane' else [0]):
   d=ps.Domain(.2,6.,4.,b)
   if kind=='plane': ps.MakePlane(d,height=.6,material=ps.Material.Si,addToExisting=False).apply()
   elif kind=='trench': ps.MakeTrench(domain=d,trenchWidth=2.,trenchDepth=2.,trenchTaperAngle=taper,maskHeight=.6,maskTaperAngle=taper,halfTrench=False,material=ps.Material.Si,maskMaterial=ps.Material.Mask).apply()
   else: ps.MakeHole(domain=d,holeRadius=1.,holeDepth=2.,holeTaperAngle=taper,maskHeight=.6,maskTaperAngle=taper,holeShape=ps.HoleShape.FULL,material=ps.Material.Si,maskMaterial=ps.Material.Mask).apply()
   name=f'{boundary}-{kind}-{taper}'
   row={'case':name,**mesh(d,name)}; rows.append(row); print(json.dumps(row),flush=True)
 for model in ['isotropic','singleParticle']:
  for operation in ['deposit','etch']:
   d=ps.Domain(.2,6.,4.,b)
   ps.MakeHole(domain=d,holeRadius=1.,holeDepth=2.,holeTaperAngle=0.,maskHeight=0.,maskTaperAngle=0.,holeShape=ps.HoleShape.FULL,material=ps.Material.Si,maskMaterial=ps.Material.Mask).apply()
   rate=.2 if operation=='deposit' else -.2
   if operation=='deposit':d.duplicateTopLevelSet(ps.Material.SiO2)
   m=ps.IsotropicProcess(rate=rate,maskMaterial=ps.Material.Mask) if model=='isotropic' else ps.SingleParticleProcess(rate=rate,stickingProbability=.5,sourceExponent=1.,maskMaterial=ps.Material.Mask)
   p=ps.Process(d,m,.2); p.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
   a=ps.AdvectionParameters(); a.timeStepRatio=.25; p.setParameters(a)
   if model=='singleParticle':
    r=ps.RayTracingParameters();r.raysPerPoint=64;r.rngSeed=314159;r.useRandomSeeds=False;p.setParameters(r)
   started=time.monotonic();p.apply()
   name=f'{boundary}-{model}-{operation}'
   row={'case':name,'dimension':3,'model':model,'operation':operation,'boundaryXY':boundary,'engine':'CPU_DISK' if model=='singleParticle' else 'not-applicable','elapsedSeconds':time.monotonic()-started,**mesh(d,name)};rows.append(row);print(json.dumps(row),flush=True)
(out/'results.json').write_text(json.dumps({'versions':{'viennaps':version('viennaps'),'viennals':version('viennals')},'rows':rows},indent=2))
