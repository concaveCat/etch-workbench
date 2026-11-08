"""Inspect installed Stage 6 material/output bindings; execute only under bounded_child."""
import importlib.metadata, inspect, json, sys
import viennaps as ps

def docs(obj):
    names=[n for n in dir(obj) if not n.startswith('_')]
    return {'doc':(getattr(obj,'__doc__','') or '')[:6000],
      'members':{n:str(getattr(obj,n))[:500] for n in names if not callable(getattr(obj,n,None))},
      'constructorDoc':(getattr(getattr(obj,'__init__',None),'__doc__','') or '')[:4000],
      'methods':{n:(getattr(getattr(obj,n),'__doc__','') or '')[:2500] for n in names if callable(getattr(obj,n,None))}}

out={'moduleMaterialNames':[n for n in dir(ps) if 'Material' in n or 'Category' in n], 'versions':{'viennaps':importlib.metadata.version('viennaps'),'viennals':importlib.metadata.version('viennals')},'bindings':{}}
for n in ['Material','MaterialInfo','MaterialRegistry','MaterialMap','Domain','Writer','Reader','OutputType','Data','DataType','MetaDataLevel']:
    obj=getattr(ps,n,None)
    if obj is not None:out['bindings'][n]=docs(obj)
for n in ['VTKWriter','FileFormatEnum','ToMesh','ToLevelSet','Mesh','Domain']:
    if n in dir(ps.ls):
        obj=getattr(ps.ls,n)
        out['bindings']['ls.'+n]=docs(obj)
reg=ps.MaterialRegistry.instance()
custom=reg.registerMaterial('Stage6 Probe')
info=reg.getInfo(custom)
out['customRuntime']={'display':str(custom),'isCustom':custom.isCustom(),'customId':custom.customId(),'legacyId':custom.legacyId(),'name':reg.getName(custom),'infoType':type(info).__name__,'infoMembers':{n:str(getattr(info,n))[:500] for n in dir(info) if not n.startswith('_') and not callable(getattr(info,n,None))},'infoMethods':{n:(getattr(getattr(info,n),'__doc__','') or '')[:1500] for n in dir(info) if not n.startswith('_') and callable(getattr(info,n,None))}}
out['registryRuntime']={'type':type(reg).__name__,'methods':{n:(getattr(getattr(reg,n),'__doc__','') or '')[:2500] for n in dir(reg) if not n.startswith('_') and callable(getattr(reg,n,None))},'currentMaterialLookup':{name:(str(reg.findMaterial(name)) if reg.findMaterial(name) is not None else None) for name in ['Si','SiO2','Mask','NonexistentStage6']}}
rendered=json.dumps(out,ensure_ascii=False,indent=2,default=str)
if len(sys.argv)>1: open(sys.argv[1],'w',encoding='utf-8').write(rendered+'\n')
print(rendered)
