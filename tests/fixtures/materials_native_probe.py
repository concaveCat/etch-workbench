"""Independent, bounded native capability reference for materials milestone."""
import json
from pathlib import Path
import viennaps as ps

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'docs/materials-evidence'
OUT.mkdir(parents=True,exist_ok=True)
registry=ps.MaterialRegistry.instance()
rows=[]
for name in ['Mask','Si','SiO2','Si3N4','SiN','PolySi','SiGe','Al2O3','HfO2','TiN','W','Cu','PMMA']:
    material=registry.findMaterial(name)
    if material is None: continue
    info=registry.getInfo(material)
    rows.append({'name':registry.getName(material),'id':int(material),'color':f'#{int(info.color_hex):06x}'})
catalog={'schemaVersion':1,'engineVersion':'4.6.2','materials':rows}
(ROOT/'docs/materials-catalog.json').write_text(json.dumps(catalog,indent=2)+'\n')
print('catalog',rows,flush=True)
evidence=[]
for dim in [2,3]:
    ps.setDimension(dim)
    ps.setNumThreads(1)
    dom=ps.Domain(.2,4.,4.,ps.BoundaryType.REFLECTIVE_BOUNDARY) if dim==3 else ps.Domain(.2, 4., boundary=ps.BoundaryType.REFLECTIVE_BOUNDARY)
    ps.MakePlane(dom,height=0.,material=registry.getMaterial('Si'),addToExisting=False).apply()
    ps.MakePlane(dom,height=.6,material=registry.getMaterial('SiO2'),addToExisting=True).apply()
    ps.MakePlane(dom,height=1.2,material=registry.getMaterial('Si3N4'),addToExisting=True).apply()
    factory=ps.GeometryFactory(dom.getSetup(),'IndependentMaterialsProbe')
    pos=[0.]*dim;pos[-1]=.6-.001
    stencil=factory.makeBoxStencil(pos,1.6,.602,0.)
    dom.applyBooleanOperation(stencil,ps.ls.BooleanOperationEnum.RELATIVE_COMPLEMENT,False)
    mesh=dom.getSurfaceMesh(addInterfaces=True,sharpCorners=False,minNodeDistanceFactor=.01)
    cells=mesh.getTriangles() if dim==3 else mesh.getLines()
    data=mesh.getCellData(); ids=None
    for i in range(data.getScalarDataSize()):
        if data.getScalarDataLabel(i)=='MaterialIds':ids=list(data.getScalarData(i))
    assert ids is not None and len(ids)==len(cells)
    mapping=dom.getMaterialMap();native_map=[int(mapping.getMaterialIdAtIdx(i)) for i in range(mapping.size())]
    assert native_map==[10,30,16],native_map
    assert set(map(int,ids))=={10,30,16},set(ids)
    dom.saveSurfaceMesh(str(OUT/f'independent-stack-{dim}d.vtk'),addInterfaces=True,sharpCorners=False,minNodeDistanceFactor=.01)
    raw={'dimension':dim,'points':mesh.getNodes(),'cells':cells,'materialIds':ids,'materialMap':native_map}
    (OUT/f'independent-stack-{dim}d.json').write_text(json.dumps(raw))
    evidence.append({'dimension':dim,'materialMap':native_map,'pointCount':len(raw['points']),'cellCount':len(cells),'cellMaterialIds':sorted(set(ids)),'pattern':'top-layer-only trench','constructor':'MakePlane(addToExisting) + GeometryFactory.makeBoxStencil + applyBooleanOperation(applyToAll=False)'})
    print(evidence[-1],flush=True)
(OUT/'native-probe-results.json').write_text(json.dumps(evidence,indent=2)+'\n')
