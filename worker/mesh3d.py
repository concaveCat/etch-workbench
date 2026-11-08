"""Verified ViennaPS 4.6.2 three dimensional domain and surface adapter."""
import math

MATERIAL_NAMES={}


def configure_material_catalog(rows):
    global MATERIAL_NAMES
    MATERIAL_NAMES={int(row['id']):row['name'] for row in rows}


def make_domain(ps, requested):
    d=requested['domain']
    b=ps.BoundaryType.REFLECTIVE_BOUNDARY if d['boundaryXY']=='reflective' else ps.BoundaryType.PERIODIC_BOUNDARY
    if d.get('bounds') is not None:
        bounds=[float(value) for value in d['bounds']]
        return ps.Domain(bounds,[b,b,ps.BoundaryType.INFINITE_BOUNDARY],float(d['gridDelta']))
    return ps.Domain(float(d['gridDelta']),float(d['xExtent']),float(d['yExtent']),b)


def make_geometry(ps, domain, g):
    if g['kind']=='plane':
        ps.MakePlane(domain,height=float(g['height']),material=ps.Material.Si,addToExisting=False).apply()
    elif g['kind']=='trench':
        ps.MakeTrench(domain=domain,trenchWidth=float(g['width']),trenchDepth=float(g['depth']),trenchTaperAngle=float(g['taperAngle']),maskHeight=float(g['maskHeight']),maskTaperAngle=float(g['maskTaperAngle']),halfTrench=False,material=ps.Material.Si,maskMaterial=ps.Material.Mask).apply()
    else:
        ps.MakeHole(domain=domain,holeRadius=float(g['radius']),holeDepth=float(g['depth']),holeTaperAngle=float(g['taperAngle']),maskHeight=float(g['maskHeight']),maskTaperAngle=float(g['maskTaperAngle']),holeShape=ps.HoleShape.FULL,material=ps.Material.Si,maskMaterial=ps.Material.Mask).apply()


def mesh_report(mesh):
    # The installed bindings expose list accessors, no pre-extraction size API.
    # The whole operation runs within the bounded process RSS supervision.
    points=mesh.getNodes()
    triangles=mesh.getTriangles()
    if mesh.getLines() or mesh.getTetras() or mesh.getHexas() or mesh.getVerticies():
        raise RuntimeError('unexpected non-triangle surface cells; material association cannot be assumed')
    if not points or not triangles:raise RuntimeError('empty 3D surface')
    # Keep the comprehension form: a hand-written single pass with per-element
    # checks measured 268 ms vs 179 ms for this 45k-point surface.
    if any(len(p)!=3 or not all(math.isfinite(x) for x in p) for p in points):raise RuntimeError('invalid 3D coordinates')
    if any(len(t)!=3 or len(set(t))!=3 or any(i<0 or i>=len(points) for i in t) for t in triangles):raise RuntimeError('invalid triangles')
    data=mesh.getCellData(); ids=None
    for i in range(data.getScalarDataSize()):
        if str(data.getScalarDataLabel(i))=='MaterialIds': ids=data.getScalarData(i)
    if ids is None or len(ids)!=len(triangles):raise RuntimeError('unaligned triangle MaterialIds')
    if any(int(value) not in MATERIAL_NAMES for value in ids):raise RuntimeError('ViennaPS surface mesh has an unknown registered material ID')
    def arrays(data):
        scalars={str(data.getScalarDataLabel(i)):[float(x) for x in data.getScalarData(i)] for i in range(data.getScalarDataSize())}
        vectors={str(data.getVectorDataLabel(i)):[[float(x) for x in row] for row in data.getVectorData(i)] for i in range(data.getVectorDataSize())}
        return scalars,vectors
    point_scalars,point_vectors=arrays(mesh.getPointData())
    cell_scalars,cell_vectors=arrays(mesh.getCellData())
    return {'points':points,'triangles':triangles,'cellData':cell_scalars,'pointData':point_scalars,
            'cellVectorData':cell_vectors,'pointVectorData':point_vectors}


def bounds(points):return [f(p[a] for p in points) for a in range(3) for f in (min,max)]


def measure_initial(report,g,delta,substrate_id=10):
    p=report['points']; ids=report['cellData']['MaterialIds']
    if g['kind']=='plane':
        h=sorted(v[2] for v in p)[len(p)//2]
        if abs(h-g['height'])>2*delta:raise RuntimeError('3D plane height mismatch')
        return {'heightMedian':h,'tolerance':2*delta}
    si={i for t,m in zip(report['triangles'],ids) if m==substrate_id for i in t}
    top=[p[i] for i in si if abs(p[i][2])<delta*.1]
    if not top:raise RuntimeError('cannot measure initial opening from the Si top surface')
    depth=-min(p[i][2] for i in si)
    if g['kind']=='hole':
        opening=min(math.hypot(v[0],v[1]) for v in top); target=g['radius']; label='openingRadius'
        shape=str(g.get('holeShape') or 'FULL')
        tol=delta*.5
        quadrants={(v[0]>= -tol,v[1]>= -tol) for v in p if v[2]<-delta and math.hypot(v[0],v[1])<g['radius']+g['depth']+delta}
        if shape=='FULL':
            required={(True,True),(True,False),(False,True),(False,False)}
        elif shape=='HALF':
            required={(True,True),(True,False)}
        elif shape=='QUARTER':
            required={(True,True)}
        else:
            raise RuntimeError(f'unknown native HoleShape {shape!r}')
        if not required.issubset(quadrants):
            raise RuntimeError(f'{shape} hole is missing its required quadrant(s): got {sorted(quadrants)}')
        if shape in ('HALF','QUARTER') and any(v[0] < -tol for v in p):
            raise RuntimeError(f'{shape} hole contains points on the removed x<0 half')
        if shape=='QUARTER' and any(v[1] < -tol for v in p):
            raise RuntimeError('QUARTER hole contains points on the removed y<0 half')
    else:
        half_trench=bool(g.get('halfTrench'))
        tol=delta*.5
        neg=[v[0] for v in top if v[0] < -tol];pos=[v[0] for v in top if v[0] >= -tol]
        if half_trench:
            if not pos:raise RuntimeError('half trench requires the retained x>=0 sidewall')
            opening=min(pos);target=g['width']/2
        else:
            if not pos or not neg:raise RuntimeError('3D trench requires both sidewalls')
            opening=min(pos)-max(neg);target=g['width']
        label='openingWidth'
    if abs(depth-g['depth'])>2*delta+1e-9 or abs(opening-target)>2*delta+1e-9:raise RuntimeError('rasterized 3D opening/depth differs by more than two grid intervals')
    return {label:opening,'depth':depth,'tolerance':2*delta}


def readback(domain,requested,report):
    d=requested['domain']; b=[str(v) for v in domain.getBoundaryConditions()]
    expected='0' if d['boundaryXY']=='reflective' else '2'
    if b!=[expected,expected,'1']:raise RuntimeError('3D boundary readback mismatch')
    # ViennaPS 4.6.2 candidate builds can mutate the 3D Domain in
    # getBoundingBox(); keep readback observational and use the already
    # extracted native surface points for bounds instead.
    surface=bounds(report['points'])
    return {'requested':d,'gridDelta':float(domain.getGridDelta()),'boundaryLabels':[d['boundaryXY'],d['boundaryXY'],'infinite'],
            'nativeActiveBandBounds':None,'nativeActiveBandBoundsStatus':'not-read: Domain.getBoundingBox mutates the ViennaPS 4.6.2 3D Domain',
            'lateralSurfaceBounds':surface[:4],
            'note':'Surface bounds are measured from the emitted native surface mesh. Native active-band bounds are omitted because the installed binding getter mutates the Domain; z computational boundary is infinite.'}
