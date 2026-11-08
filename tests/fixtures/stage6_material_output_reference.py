"""Independent direct ViennaPS 4.6.2 reference for custom materials and Domain outputs."""
import hashlib, importlib.metadata, json, sys
from pathlib import Path
import viennaps as ps


def register(rows):
    registry=ps.MaterialRegistry.instance()
    result=[]
    for index,row in enumerate(rows):
        material=registry.registerMaterial(row['name'])
        if material.customId()!=index or material.legacyId()!=row['id']:
            raise RuntimeError(f"unstable native material ID for {row['name']}")
        info=registry.getInfo(material);info.color_hex=int(row['color'].lstrip('#'),16)
        if row.get('category'):info.category=ps.MaterialCategory[str(row['category'])]
        if row.get('densityGcm3') is not None:info.density_gcm3=float(row['densityGcm3'])
        info.conductive=bool(row.get('conductive',False));registry.setInfo(material,info)
        result.append(material)
    return registry


def mesh_data(domain,dimension):
    mesh=domain.getSurfaceMesh(addInterfaces=True,sharpCorners=False,minNodeDistanceFactor=.01)
    data=mesh.getCellData();ids=None
    for i in range(data.getScalarDataSize()):
        if str(data.getScalarDataLabel(i))=='MaterialIds':ids=[int(x) for x in data.getScalarData(i)]
    mapping=domain.getMaterialMap()
    return {'points':[[float(v) for v in row] for row in mesh.getNodes()],
      'cells':mesh.getTriangles() if dimension==3 else mesh.getLines(),'materialIds':ids,
      'materialIdsByLevelSetIndex':[int(mapping.getMaterialIdAtIdx(i)) for i in range(mapping.size())],
      'levelSetCount':int(domain.getNumberOfLevelSets())}


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()

def emit_outputs(domain,dimension,step_id,outdir):
    outdir.mkdir(parents=True,exist_ok=True);mapping=mesh_data(domain,dimension)['materialIdsByLevelSetIndex'];rows=[]
    def add(kind,path,**extra):rows.append({'kind':kind,'fileName':path.name,'sha256':sha(path),'sizeBytes':path.stat().st_size,**extra})
    surface=outdir/f'{step_id}.vtk';domain.saveSurfaceMesh(str(surface),addInterfaces=True,sharpCorners=False,minNodeDistanceFactor=.01);add('surfaceMesh',surface)
    level_prefix=outdir/f'{step_id}-levelsets.vtk';domain.saveLevelSets(str(level_prefix))
    for i,path in enumerate(sorted(outdir.glob(level_prefix.name+'_layer*.lvst'),key=lambda p:int(p.stem.split('_layer')[-1]))):add('levelSet',path,levelSetIndex=i,materialId=mapping[i])
    mesh_prefix=outdir/f'{step_id}-levelset-mesh.vtk';ps.Domain(domain).saveLevelSetMesh(str(mesh_prefix),1)
    for i,path in enumerate(sorted(outdir.glob(mesh_prefix.name+'_layer*.vtp'),key=lambda p:int(p.stem.split('_layer')[-1]))):add('levelSetMesh',path,levelSetIndex=i,materialId=mapping[i])
    if dimension==3:
        volume_prefix=outdir/f'{step_id}-volume.vtk';domain.saveVolumeMesh(str(volume_prefix),.01)
        path=outdir/f'{volume_prefix.name}_volume.vtu';add('volumeMesh',path)
    metadata={'schemaVersion':1,'source':'viennaps.Domain.getMetaData','metadataLevel':str(domain.getMetaDataLevel()),
      'nativeUnit':None,'association':'domain metadata; native binding declares no point/cell association',
      'values':domain.getMetaData(),'dimension':dimension,'gridDelta':float(domain.getGridDelta()),
      'materialIdsByLevelSetIndex':mapping}
    path=outdir/f'{step_id}-domain-metadata.json';path.write_text(json.dumps(metadata,ensure_ascii=False,separators=(',',':'),allow_nan=False)+'\n');add('nativeMetadata',path)
    return rows


def main(project_path,custom_path,outpath):
    project=json.loads(Path(project_path).read_text());custom=json.loads(Path(custom_path).read_text());dim=project['dimension'];ps.setDimension(dim);ps.setNumThreads(project['solver']['cpuThreads']);registry=register(custom)
    boundary=ps.BoundaryType.REFLECTIVE_BOUNDARY if project['domain'].get('xBoundary',project['domain'].get('boundaryXY'))=='reflective' else ps.BoundaryType.PERIODIC_BOUNDARY
    d=project['domain'];domain=ps.Domain(d['gridDelta'],d['xExtent'],d['yExtent'],boundary) if dim==3 else ps.Domain(d['gridDelta'], d['xExtent'], boundary=boundary)
    g=project['geometry'];ps.MakePlane(domain,height=g['height'],material=registry.getMaterial(g['substrateMaterial']),addToExisting=False).apply()
    if project.get('outputSettings',{}).get('nativeMetadata'):domain.enableMetaData(ps.MetaDataLevel.FULL)
    root=Path(outpath);root.mkdir(parents=True,exist_ok=True);result={'versions':{'viennaps':importlib.metadata.version('viennaps'),'viennals':importlib.metadata.version('viennals')},'snapshots':{}}
    result['snapshots']['initial']={'mesh':mesh_data(domain,dim),'outputs':emit_outputs(domain,dim,'initial',root/'initial')}
    for step in project['steps']:
        if not step['enabled']:continue
        rate=float(step['rateMagnitude'])*(1 if step['operation']=='deposit' else -1)
        if step['operation']=='deposit':domain.duplicateTopLevelSet(registry.getMaterial(step['depositMaterial']))
        process=ps.Process(domain,ps.IsotropicProcess(rate=rate,maskMaterials=[]),float(step['duration']))
        process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
        adv=ps.AdvectionParameters();adv.timeStepRatio=float(project['solver']['timeStepRatio'])
        requested=project['solver'].get('advection')
        if requested:
            adv.spatialScheme=getattr(ps.ls.SpatialSchemeEnum,requested['spatialScheme']);adv.temporalScheme=getattr(ps.ls.TemporalSchemeEnum,requested['temporalScheme'])
            for key in ('adaptiveTimeStepping','adaptiveTimeStepSubdivisions','calculateIntermediateVelocities','checkDissipation','dissipationAlpha','ignoreVoids','velocityOutput'):setattr(adv,key,requested[key])
        process.setParameters(adv);process.apply()
        result['snapshots'][step['id']]={'mesh':mesh_data(domain,dim),'outputs':emit_outputs(domain,dim,step['id'],root/step['id'])}
    Path(outpath,'reference.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    print(json.dumps({'versions':result['versions'],'dimensions':dim,'snapshotIds':list(result['snapshots'])}),flush=True)

if __name__=='__main__':main(*sys.argv[1:])
