"""Independent direct ViennaPS call, used only inside bounded native children."""
import json,sys
from pathlib import Path
import viennaps as ps

def build(project):
    dim=project['dimension'];ps.setDimension(dim);ps.setNumThreads(project['solver']['cpuThreads'])
    d=project['domain']; b=d.get('boundaryXY',d.get('xBoundary'))
    boundary=ps.BoundaryType.REFLECTIVE_BOUNDARY if b=='reflective' else ps.BoundaryType.PERIODIC_BOUNDARY
    domain=ps.Domain(d['gridDelta'],d['xExtent'],d['yExtent'],boundary) if dim==3 else ps.Domain(d['gridDelta'], d['xExtent'], boundary=boundary)
    reg=ps.MaterialRegistry.instance();g=project['geometry'];protected=[]
    if g['kind']=='stack':
        ps.MakePlane(domain,height=g['baseHeight'],material=reg.getMaterial(g['substrateMaterial']),addToExisting=False).apply()
        height=g['baseHeight']
        for layer in g['layers']:
            height+=layer['thickness']
            ps.MakePlane(domain,height=height,material=reg.getMaterial(layer['material']),addToExisting=True).apply()
            if layer['role']=='mask':protected.append(reg.getMaterial(layer['material']))
        pattern=g['pattern']
        if pattern['kind']!='none':
            layer=g['layers'][-1];eps=d['gridDelta']*1e-3;pos=[0.]*dim;pos[-1]=height-layer['thickness']-eps
            factory=ps.GeometryFactory(domain.getSetup(),'DirectReference')
            if pattern['kind']=='trench':stencil=factory.makeBoxStencil(pos,pattern['width'],layer['thickness']+2*eps,-pattern['taperAngle'])
            else:stencil=factory.makeCylinderStencil(pos,pattern['radius'],layer['thickness']+2*eps,-pattern['taperAngle'])
            domain.applyBooleanOperation(stencil,ps.ls.BooleanOperationEnum.RELATIVE_COMPLEMENT,False)
    elif g['kind']=='plane':
        ps.MakePlane(domain,height=g['height'],material=reg.getMaterial(g['substrateMaterial']),addToExisting=False).apply()
    else:
        common=dict(domain=domain,maskHeight=g['maskHeight'],maskTaperAngle=g['maskTaperAngle'],material=reg.getMaterial(g['substrateMaterial']),maskMaterial=reg.getMaterial(g.get('maskMaterial','Mask')))
        if g['kind']=='trench':ps.MakeTrench(**common,trenchWidth=g['width'],trenchDepth=g['depth'],trenchTaperAngle=g['taperAngle'],halfTrench=False).apply()
        else:ps.MakeHole(**common,holeRadius=g['radius'],holeDepth=g['depth'],holeTaperAngle=g['taperAngle'],holeShape=ps.HoleShape.FULL).apply()
        if g['maskHeight']>0:protected.append(reg.getMaterial(g.get('maskMaterial','Mask')))
    return domain,protected

def capture(domain,dim):
    mesh=domain.getSurfaceMesh(addInterfaces=True,sharpCorners=False,minNodeDistanceFactor=.01)
    data=mesh.getCellData();ids=None
    for i in range(data.getScalarDataSize()):
        if data.getScalarDataLabel(i)=='MaterialIds':ids=[int(x) for x in data.getScalarData(i)]
    mapping=domain.getMaterialMap()
    return {'points':mesh.getNodes(),'triangles' if dim==3 else 'lines':mesh.getTriangles() if dim==3 else mesh.getLines(),
            'materialIds':ids,'materialMap':[int(mapping.getMaterialIdAtIdx(i)) for i in range(mapping.size())]}

def run(project,output):
    domain,protected=build(project);dim=project['dimension'];result={'initial':capture(domain,dim)};reg=ps.MaterialRegistry.instance()
    for step in project['steps']:
        if not step['enabled']:continue
        rate=step['rateMagnitude']*(1 if step['operation']=='deposit' else -1)
        if rate:
            if step['operation']=='deposit':domain.duplicateTopLevelSet(reg.getMaterial(step['depositMaterial']))
            if step['model']=='singleParticle':model=ps.SingleParticleProcess(rate=rate,stickingProbability=step['stickingProbability'],sourceExponent=step['sourceExponent'],maskMaterials=protected)
            else:model=ps.IsotropicProcess(rate=rate,maskMaterials=protected)
            process=ps.Process(domain,model,step['duration']);process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
            adv=ps.AdvectionParameters();adv.timeStepRatio=project['solver']['timeStepRatio'];process.setParameters(adv)
            if step['model']=='singleParticle':
                ray=ps.RayTracingParameters();ray.raysPerPoint=project['solver']['raysPerPoint'];ray.rngSeed=project['solver']['random']['seed'];ray.useRandomSeeds=False;process.setParameters(ray)
            process.apply()
        result[step['id']]=capture(domain,dim)
    Path(output).write_text(json.dumps(result))

if __name__=='__main__':run(json.loads(Path(sys.argv[1]).read_text()),sys.argv[2])
