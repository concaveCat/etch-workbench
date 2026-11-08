"""Independent native 4.6.2 two-step reference; invoke through bounded_child."""
import json,sys
from pathlib import Path
import viennaps as ps
ps.setDimension(3);ps.setNumThreads(1)
p=Path(sys.argv[1]);p.mkdir(exist_ok=True)
d=ps.Domain(.2,6.,4.,ps.BoundaryType.REFLECTIVE_BOUNDARY)
ps.MakeHole(domain=d,holeRadius=1.,holeDepth=2.,holeTaperAngle=0.,maskHeight=0.,maskTaperAngle=0.,holeShape=ps.HoleShape.FULL,material=ps.Material.Si,maskMaterial=ps.Material.Mask).apply()
for operation in ['deposit','etch']:
 if operation=='deposit':d.duplicateTopLevelSet(ps.Material.SiO2)
 m=ps.SingleParticleProcess(rate=.2 if operation=='deposit' else -.2,stickingProbability=.5,sourceExponent=1.,maskMaterial=ps.Material.Mask)
 process=ps.Process(d,m,.2);process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
 a=ps.AdvectionParameters();a.timeStepRatio=.25;process.setParameters(a)
 r=ps.RayTracingParameters();r.raysPerPoint=64;r.rngSeed=314159;r.useRandomSeeds=False;process.setParameters(r)
 process.apply()
 mesh=d.getSurfaceMesh(addInterfaces=True,sharpCorners=False,minNodeDistanceFactor=.01)
 data={'points':mesh.getNodes(),'triangles':mesh.getTriangles(),'materials':mesh.getCellData().getScalarData('MaterialIds',True)}
 (p/(operation+'.json')).write_text(json.dumps(data))
 d.saveSurfaceMesh(str(p/(operation+'.vtk')),addInterfaces=True,sharpCorners=False,minNodeDistanceFactor=.01)
