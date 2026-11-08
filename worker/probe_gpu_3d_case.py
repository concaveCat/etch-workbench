"""One concrete host GPU case, ONLY through bounded_child.py."""
import argparse,json,math,sys,time,subprocess
from pathlib import Path
from importlib.metadata import version
parser=argparse.ArgumentParser();parser.add_argument('engine',choices=['GPU_DISK','GPU_TRIANGLE']);parser.add_argument('operation',choices=['etch','deposit']);parser.add_argument('boundary',choices=['reflective','periodic']);parser.add_argument('output',type=Path);args=parser.parse_args()
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'worker'))
from tests.vtk_legacy import read_legacy_vtk
from mesh3d import mesh_report,bounds,configure_material_catalog
import viennaps as ps
catalog=json.loads((ROOT/'docs/materials-catalog.json').read_text())
configure_material_catalog(catalog['materials'])
ps.setDimension(3);ps.setNumThreads(1)
args.output.mkdir(parents=True,exist_ok=True)
available=ps.gpuAvailable();print(json.dumps({'gpuAvailable':available}),flush=True)
if not available:raise RuntimeError('ViennaPS gpuAvailable returned false on this host')
outputs=[];started=time.monotonic()
for repeat in range(2):
 b=ps.BoundaryType.REFLECTIVE_BOUNDARY if args.boundary=='reflective' else ps.BoundaryType.PERIODIC_BOUNDARY
 d=ps.Domain(.2,6.,4.,b)
 ps.MakeHole(domain=d,holeRadius=1.,holeDepth=2.,holeTaperAngle=0.,maskHeight=.4,maskTaperAngle=0.,holeShape=ps.HoleShape.FULL,material=ps.Material.Si,maskMaterial=ps.Material.Mask).apply()
 initial=mesh_report(d.getSurfaceMesh(addInterfaces=True,sharpCorners=False,minNodeDistanceFactor=.01))
 if args.operation=='deposit':d.duplicateTopLevelSet(ps.Material.SiO2)
 rate=.2 if args.operation=='deposit' else -.2
 m=ps.SingleParticleProcess(rate=rate,stickingProbability=.5,sourceExponent=1.,maskMaterial=ps.Material.Mask)
 process=ps.Process(d,m,.2);process.setFluxEngineType(getattr(ps.FluxEngineType,args.engine))
 advection=ps.AdvectionParameters();advection.timeStepRatio=.25;process.setParameters(advection)
 ray=ps.RayTracingParameters();ray.raysPerPoint=64;ray.rngSeed=314159;ray.useRandomSeeds=False;process.setParameters(ray)
 process.apply()
 report=mesh_report(d.getSurfaceMesh(addInterfaces=True,sharpCorners=False,minNodeDistanceFactor=.01))
 if args.operation=='deposit' and 30 not in report['cellData']['MaterialIds']:raise RuntimeError('GPU deposition did not produce SiO2')
 if initial['points']==report['points']:raise RuntimeError('GPU process did not change the geometry')
 vtk=args.output/f'repeat-{repeat}.vtk';d.saveSurfaceMesh(str(vtk),addInterfaces=True,sharpCorners=False,minNodeDistanceFactor=.01)
 decoded=read_legacy_vtk(vtk)
 if decoded['cells']!=report['triangles'] or decoded['cellData']['MaterialIds']!=report['cellData']['MaterialIds']:raise RuntimeError('GPU VTK triangle/material association mismatch')
 (args.output/f'repeat-{repeat}.json').write_text(json.dumps(report,allow_nan=False))
 outputs.append(report)
a,b=outputs
# Fixed GPU replay tolerance is one thousandth of a grid interval, separate
# from CPU precision. Topology and material arrays must also match exactly.
if a['triangles']!=b['triangles'] or a['cellData']!=b['cellData'] or len(a['points'])!=len(b['points']):raise RuntimeError('GPU fixed-seed repeats changed topology or cell materials')
distance=max(math.dist(p,q) for p,q in zip(a['points'],b['points']))
if distance>2e-4:raise RuntimeError(f'GPU fixed-seed repeated geometry distance {distance} exceeds 0.0002 um')
device='unknown (CUDA context passed; nvidia-smi did not report device name)'
try:
 device=subprocess.run(['/usr/lib/wsl/lib/nvidia-smi','--query-gpu=name','--format=csv,noheader'],capture_output=True,text=True,check=True,timeout=3).stdout.strip().splitlines()[0]
except (OSError,subprocess.SubprocessError,IndexError):pass
result={'status':'verified','dimension':3,'model':'singleParticle','operation':args.operation,'boundaryXY':args.boundary,'engine':args.engine,'versions':{'viennaps':version('viennaps'),'viennals':version('viennals')},'cpuThreads':1,'seed':314159,'raysPerPoint':64,'gridDelta':.2,'repeatMaxCorrespondingPointDistance':distance,'gpuRepeatToleranceUm':.0002,'pointCount':len(a['points']),'triangleCount':len(a['triangles']),'surfaceBounds':bounds(a['points']),'materials':sorted(set(a['cellData']['MaterialIds'])),'device':device,'host':{'hostname':__import__('platform').node(),'platform':__import__('platform').platform()},'elapsedSeconds':time.monotonic()-started}
(args.output/'result.json').write_text(json.dumps(result,indent=2));print(json.dumps(result),flush=True)
