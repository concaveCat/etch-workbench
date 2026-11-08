"""Native Stage 6 custom-material, metadata and file-output probe; bounded_child only."""
import importlib.metadata, json, sys
from pathlib import Path
import viennaps as ps

outdir=Path(sys.argv[1]);outdir.mkdir(parents=True,exist_ok=True)
report={'versions':{'viennaps':importlib.metadata.version('viennaps'),'viennals':importlib.metadata.version('viennals')},
 'materialCategories':{n:int(getattr(ps.MaterialCategory,n)) for n in dir(ps.MaterialCategory) if n.isupper()},'custom':{},'dimensions':{}}
registry=ps.MaterialRegistry.instance()
for name in ['Stage6 Alpha','Stage6 Beta']:
 m=registry.registerMaterial(name)
 report['custom'][name]={'display':str(m),'isCustom':m.isCustom(),'customId':m.customId(),'legacyId':m.legacyId(),'name':registry.getName(m),'count':registry.customMaterialCount()}
a=registry.getMaterial('Stage6 Alpha');b=registry.getMaterial('Stage6 Beta')
info=registry.getInfo(a)
report['materialInfoBefore']={n:(str(getattr(info,n)) if not callable(getattr(info,n,None)) else None) for n in ['name','category','density_gcm3','conductive','color_hex','color_rgb']}
report['materialInfoWriteAttempts']={}
for name,value in [('category',ps.MaterialCategory.__members__['DIELECTRIC'] if hasattr(ps.MaterialCategory,'__members__') and 'DIELECTRIC' in ps.MaterialCategory.__members__ else getattr(ps.MaterialCategory,'DIELECTRIC',None)),('density_gcm3',2.25),('conductive',False),('color_hex',0x123456)]:
 try:
  if value is None: raise RuntimeError('no DIELECTRIC enum member')
  setattr(info,name,value);report['materialInfoWriteAttempts'][name]='assigned'
 except Exception as exc:report['materialInfoWriteAttempts'][name]=f'{type(exc).__name__}: {exc}'
try:
 registry.setInfo(a,info)
 info2=registry.getInfo(a)
 report['materialInfoAfterSet']={n:(str(getattr(info2,n)) if not callable(getattr(info2,n,None)) else None) for n in ['name','category','density_gcm3','conductive','color_hex','color_rgb']}
except Exception as exc:report['materialInfoSetError']=f'{type(exc).__name__}: {exc}'

for dim in (2,3):
 ps.setDimension(dim);ps.setNumThreads(1)
 boundary=ps.BoundaryType.REFLECTIVE_BOUNDARY
 domain=ps.Domain(.2,2.,2.,boundary) if dim==3 else ps.Domain(.2,2.,boundary)
 ps.MakePlane(domain,height=.4,material=a,addToExisting=False).apply()
 domain.duplicateTopLevelSet(b)
 ps.MakePlane(domain,height=.8,material=b,addToExisting=True).apply()
 domain.enableMetaData(ps.MetaDataLevel.FULL)
 domain.addMetaData('Stage6CustomMaterial',[3.,4.])
 row={'materialMap':[{'index':i,'legacyId':int(domain.getMaterialMap().getMaterialIdAtIdx(i)),'name':domain.getMaterialMap().toString(domain.getMaterialMap().getMaterialAtIdx(i))} for i in range(domain.getMaterialMap().size())],
      'levelSetCount':int(domain.getNumberOfLevelSets()),'metadata':domain.getMetaData(),'metadataLevel':str(domain.getMetaDataLevel()),'outputs':{}}
 suffix=f'{dim}d'
 outputs={'levelSets':outdir/f'levelsets-{suffix}.vtk','levelSetMesh':outdir/f'levelset-mesh-{suffix}.vtk','surfaceMesh':outdir/f'surface-{suffix}.vtk','domainState':outdir/f'domain-{suffix}.vpsd'}
 if dim==3:outputs['volumeMesh']=outdir/f'volume-{suffix}.vtk'
 for kind,path in outputs.items():
  try:
   if kind=='levelSets':domain.saveLevelSets(str(path))
   elif kind=='levelSetMesh':domain.saveLevelSetMesh(str(path),1)
   elif kind=='surfaceMesh':domain.saveSurfaceMesh(str(path),True,False,.01)
   elif kind=='volumeMesh':domain.saveVolumeMesh(str(path),.01)
   elif kind=='domainState':ps.Writer(domain,str(path)).apply()
   row['outputs'][kind]={'path':str(path.relative_to(outdir)),'files':sorted(p.name for p in outdir.glob(path.stem+'*')),'size':path.stat().st_size if path.exists() else 0,'header':path.read_bytes()[:160].decode('latin-1') if path.exists() else None}
  except Exception as exc:row['outputs'][kind]={'error':f'{type(exc).__name__}: {exc}'}
 try:
  restored=ps.Domain(.2,2.,2.,boundary) if dim==3 else ps.Domain(.2,2.,boundary)
  ps.Reader(restored,str(outputs['domainState'])).apply()
  m=restored.getMaterialMap()
  row['readerRoundTrip']={'materialMap':[{'index':i,'legacyId':int(m.getMaterialIdAtIdx(i)),'name':m.toString(m.getMaterialAtIdx(i))} for i in range(m.size())],'levelSetCount':int(restored.getNumberOfLevelSets()),'metadata':restored.getMetaData(),'metadataLevel':str(restored.getMetaDataLevel())}
 except Exception as exc:row['readerRoundTripError']=f'{type(exc).__name__}: {exc}'
 report['dimensions'][str(dim)]=row
(outdir/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str)+'\n')
print(json.dumps({'versions':report['versions'],'custom':report['custom'],'dimensions':{d:{'map':v['materialMap'],'outputs':v['outputs'],'reader':v.get('readerRoundTrip',v.get('readerRoundTripError'))} for d,v in report['dimensions'].items()},'materialInfo':report.get('materialInfoAfterSet',report['materialInfoWriteAttempts'])},ensure_ascii=False),flush=True)
