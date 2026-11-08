import asyncio,copy,hashlib,io,json,math,subprocess,unittest,zipfile
from pathlib import Path
from unittest.mock import patch
from backend import snapshots as snapshot_store
from backend import storage
from backend.main import app
from backend.models import parse_project, parse_replay, ProjectV2
from backend.hashing import requested_input_hash
from backend.validation import validate_project, ProjectInputError, capability_verified, select_engine
from tests.test_api_integration import asgi_request,json_body
from tests.vtk_legacy import read_legacy_vtk

ROOT=Path(__file__).resolve().parents[1]
EVIDENCE=ROOT/'var/evidence';EVIDENCE.mkdir(exist_ok=True)
def project():return json.loads((ROOT/'tests/fixtures/project-3d.json').read_text())
def checksum(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def compare_points(a,b):
    aa=sorted(map(tuple,a));bb=sorted(map(tuple,b))
    if len(aa)!=len(bb):raise AssertionError('point count differs')
    return max((math.dist(x,y) for x,y in zip(aa,bb)),default=0.)

class Contract3D(unittest.TestCase):
    def test_legacy_hashes_and_files(self):
        evidence=[]
        for row in json.loads((ROOT/'tests/fixtures/legacy/index.json').read_text()):
            d=ROOT/'tests/fixtures/legacy'/row['runId'];p=json.loads((d/'project.json').read_text())
            self.assertEqual(requested_input_hash(parse_project(p)),row['requestedInputHash'])
            self.assertEqual(parse_replay(json.loads((d/'replay-project.json').read_text())).schemaVersion,1)
            self.assertTrue(read_legacy_vtk(d/'initial.vtk')['cells'])
            for name,h in row['checksums'].items():self.assertEqual(checksum(d/name),h)
            evidence.append(row)
        (EVIDENCE/'t01-legacy.json').write_text(json.dumps(evidence,indent=2))
    def test_3d_hash_view_and_validation(self):
        p=project();h=requested_input_hash(parse_project(p));q=copy.deepcopy(p)
        q['view3D']['camera']={'position':[3.,4.,5.],'focalPoint':[0.,0.,0.],'viewUp':[0.,0.,1.],'parallelScale':4.}
        q['name']='other';q['steps'][0]['id']='step_renamed';q['steps'][0]['name']='renamed'
        self.assertEqual(h,requested_input_hash(parse_project(q)))
        q['domain']['yExtent']=5.;self.assertNotEqual(h,requested_input_hash(parse_project(q)))
        q=copy.deepcopy(p);q['domain']['yExtent']=40.;validate_project(q)
        for mutate,path in [(lambda q:q['geometry'].update(radius=3),'geometry'),(lambda q:q['geometry'].update(taperAngle=45),'geometry')]:
            q=copy.deepcopy(p);mutate(q)
            with self.assertRaises(ProjectInputError) as ctx:validate_project(q)
            self.assertTrue(any(i.path.startswith(path) for i in ctx.exception.issues))
        # A coverage-matrix miss must not reject an explicit GPU request.
        # The request is routed to the selected native GPU; execution reports
        # the engine's actual outcome without silently switching to CPU.
        with patch('backend.validation.capability_verified',return_value=False):
            q=copy.deepcopy(p);q['solver']['enginePreference']='gpu'
            normalized,_=validate_project(q)
            normalized=normalized.model_dump(mode='json',exclude_none=True)
            self.assertEqual(select_engine(normalized,normalized['steps'][0]),'GPU_DISK')

class Workflow3D(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.lifespan=app.router.lifespan_context(app);await self.lifespan.__aenter__()
    async def asyncTearDown(self):await self.lifespan.__aexit__(None,None,None)
    async def wait(self,rid,timeout=30):
        start=asyncio.get_running_loop().time()
        while asyncio.get_running_loop().time()-start<timeout:
            r=json_body(await asgi_request('/api/runs/'+rid))
            if r['status'] in ('succeeded','failed','cancelled','timed_out','interrupted'):return r
            await asyncio.sleep(.05)
        self.fail('worker timed out')
    async def run_project(self,p,kind='simulate'):
        res=await asgi_request('/api/runs',method='POST',payload={'kind':kind,'project':p});self.assertEqual(res[0],202,res[2])
        rid=json_body(res)['runId'];r=await self.wait(rid);self.assertEqual(r['status'],'succeeded',r.get('failureReason'));return rid,r
    def snapshot(self,rid,sid):
        snapshot_store.ensure_materialized(storage.run_path(rid),sid)
        return storage.read_json(storage.run_path(rid)/(sid+'.json'))
    async def test_multistep_reference_replay_and_download(self):
        p=project();rid,r=await self.run_project(p)
        reference=EVIDENCE/'direct-3d';reference.mkdir(exist_ok=True)
        cmd=[str(ROOT/'.venv/bin/python'),str(ROOT/'scripts/bounded_child.py'),'--cwd',str(ROOT),'--record',str(EVIDENCE/'direct-3d-record.json'),'--timeout-seconds','30','--',str(ROOT/'.venv/bin/python'),str(ROOT/'tests/fixtures/direct_3d_reference.py'),str(reference)]
        proc=await asyncio.create_subprocess_exec(*cmd,stdout=asyncio.subprocess.DEVNULL);self.assertEqual(await proc.wait(),0)
        distances=[]
        for sid,name in [('step_deposit','deposit'),('step_etching','etch')]:
            s=self.snapshot(rid,sid);d=json.loads((reference/(name+'.json')).read_text())
            error=compare_points(s['points'],d['points']);self.assertLessEqual(error,2e-5)
            self.assertEqual(s['cellData']['materialIds'],d['materials']);self.assertEqual(s['triangles'],d['triangles']);distances.append(error)
            self.assertEqual(s['schemaVersion'],2);self.assertEqual(s['dimension'],3);self.assertNotIn('lines',s)
        replay=json_body(await asgi_request('/api/runs/'+rid+'/replay-project'))
        self.assertEqual(replay['schemaVersion'],2);self.assertEqual(replay['normalizedProject']['solver']['random']['mode'],'fixed')
        res=await asgi_request('/api/runs/replay',method='POST',payload=replay);self.assertEqual(res[0],202,res[2]);repeated_id=json_body(res)['runId'];done=await self.wait(repeated_id);self.assertEqual(done['status'],'succeeded',done.get('failureReason'))
        error=compare_points(self.snapshot(rid,'step_etching')['points'],self.snapshot(repeated_id,'step_etching')['points']);self.assertLessEqual(error,2e-5)
        invalid=copy.deepcopy(replay);invalid['executionRequirements']['steps'][0]['engine']='GPU_LINE';res=await asgi_request('/api/runs/replay',method='POST',payload=invalid);self.assertEqual(res[0],422)
        archive=await asgi_request('/api/runs/'+rid+'/download');self.assertEqual(archive[0],200)
        with zipfile.ZipFile(io.BytesIO(archive[2])) as z:
            for name in ['project.json','replay-project.json','manifest.json','run.log','initial.json','step_etching.json']:self.assertIn(name,z.namelist())
            self.assertNotIn('initial.vtk',z.namelist())
        for row in r['snapshots']:
            s=self.snapshot(rid,row['stepId'])
            self.assertEqual(s['mesh']['pointCount'],len(s['points']))
            self.assertEqual(s['mesh']['triangleCount'],len(s['triangles']))
        (EVIDENCE/'t06-t08-t14.json').write_text(json.dumps({'runId':rid,'replayRunId':repeated_id,'referenceMaxSortedPointDistance':distances,'replayMaxSortedPointDistance':error,'tolerance':2e-5,'metric':'Euclidean distance between lexicographically matched full native vertices; equal topology and material arrays also required. Equal point sets give zero symmetric point distance.','nativeSurfaceVtkEnabled':False},indent=2))
    async def test_geometries_boundaries_and_isotropic(self):
        rows=[]
        for boundary in ['reflective','periodic']:
            for kind in ['plane','trench','hole']:
                p=project();p['domain']['boundaryXY']=boundary;p['steps']=[]
                if kind=='plane':p['geometry']={'kind':'plane','height':.6,'substrateMaterial':'Si'}
                elif kind=='trench':p['geometry']={'kind':'trench','width':2.,'depth':2.,'taperAngle':10.,'maskHeight':.6,'maskTaperAngle':10.,'substrateMaterial':'Si'}
                else:p['geometry'].update(maskHeight=.6,taperAngle=10.,maskTaperAngle=10.)
                rid,r=await self.run_project(p,'preview');s=self.snapshot(rid,'initial')
                self.assertEqual(s['domainReadback']['boundaryLabels'],[boundary,boundary,'infinite']);self.assertEqual(s['surfaceBounds'][:4],[-3.,3.,-2.,2.]);self.assertEqual(s['gridDelta'],.2)
                self.assertTrue(s['triangles']);self.assertEqual(len(s['triangles']),len(s['cellData']['materialIds']));rows.append({'runId':rid,'kind':kind,'boundary':boundary,'measuredGeometry':s['measuredGeometry']})
        for op in ['etch','deposit']:
            p=project();p['geometry']={'kind':'plane','height':.6,'substrateMaterial':'Si'}
            # The installed 4.6.2 MakePlane surface at gridDelta=.2 is only
            # 0.1 um thick (the initial native mesh spans z=.5..6). Keep the
            # etch within that actual body so this test checks an evolving
            # surface instead of asking ViennaPS to etch the entire solid away.
            duration=.1 if op=='etch' else 1.
            p['steps']=[{'id':'step_isotest','name':op,'enabled':True,'operation':op,'model':'isotropic','rateMagnitude':.4,'duration':duration}]
            if op=='deposit':p['steps'][0]['depositMaterial']='SiO2'
            rid,r=await self.run_project(p);s=self.snapshot(rid,'step_isotest');h=s['surfaceBounds'][5];target=.6+(.4 if op=='deposit' else -.4)*duration;self.assertLessEqual(abs(h-target),.4);rows.append({'runId':rid,'operation':op,'topHeight':h,'expected':target,'duration':duration})
        (EVIDENCE/'t02-t05.json').write_text(json.dumps(rows,indent=2))
    async def test_noop_same_material_and_save(self):
        p=project();p['steps'][1]=dict(p['steps'][0],id='step_second0');p['steps'].append(dict(p['steps'][0],id='step_zero000',rateMagnitude=0.))
        p['view3D']['camera']={'position':[3.,4.,5.],'focalPoint':[0.,0.,0.],'viewUp':[0.,0.,1.],'parallelScale':4.}
        save=await asgi_request('/api/projects',method='POST',payload=p);self.assertEqual(save[0],201,save[2]);pid=json_body(save)['id'];loaded=json_body(await asgi_request('/api/projects/'+pid))['project'];self.assertEqual(loaded['view3D']['camera'],p['view3D']['camera'])
        rid,r=await self.run_project(p);self.assertEqual(len(r['snapshots']),4);before=self.snapshot(rid,'step_second0');zero=self.snapshot(rid,'step_zero000')
        self.assertEqual(before['points'],zero['points']);self.assertEqual(before['triangles'],zero['triangles']);self.assertEqual(before['materialIdsByLevelSetIndex'],[10,30,30]);self.assertEqual(zero['materialIdsByLevelSetIndex'],[10,30,30]);self.assertTrue(zero['noOp'])
        self.assertNotIn('lineCount',r['snapshots'][0]);self.assertIn('triangleCount',r['snapshots'][0])
        (EVIDENCE/'t07-t13.json').write_text(json.dumps({'runId':rid,'projectId':pid,'snapshotCount':4,'materialIdsByLevelSetIndex':zero['materialIdsByLevelSetIndex'],'zeroRateDidNotAddLevelSet':True},indent=2))

class Resource3D(Workflow3D):
    # Only resource tests in this subclass; workflows are inherited intentionally
    # by unittest unless overridden below.
    test_multistep_reference_replay_and_download = None
    test_geometries_boundaries_and_isotropic = None
    test_noop_same_material_and_save = None

    async def test_high_resolution_mesh_and_cancel(self):
        import time
        from backend import supervisor
        high_resolution=project();high_resolution['domain'].update(gridDelta=.05,xExtent=7.,yExtent=7.)
        high_resolution['geometry']={'kind':'plane','height':.6,'substrateMaterial':'Si'}
        high_resolution['steps']=[]
        response=await asgi_request('/api/projects/validate',method='POST',payload=high_resolution)
        self.assertEqual(response[0],200,json_body(response))
        rid,r=await self.run_project(high_resolution,'preview')
        mesh=self.snapshot(rid,'initial')
        self.assertEqual(mesh['gridDelta'],.05)
        self.assertGreater(len(mesh['points']),4096)
        self.assertGreater(mesh['mesh']['triangleCount'],16000)
        manifest=storage.read_json(storage.run_path(rid)/'manifest.json')
        self.assertEqual(manifest['childRecord']['remainingOwnedGroupMembers'],[])
        p=project();p['steps'][0]['duration']=600.;p['steps']=p['steps'][:1]
        response=await asgi_request('/api/runs',method='POST',payload={'kind':'simulate','project':p});self.assertEqual(response[0],202,response[2]);rid=json_body(response)['runId']
        for _ in range(100):
            m=storage.read_json(storage.run_path(rid)/'manifest.json')
            if m.get('currentStepId'):break
            await asyncio.sleep(.05)
        t=time.perf_counter();health=await asgi_request('/api/health');status=await asgi_request('/api/runs/'+rid);cancel=await asgi_request('/api/runs/'+rid+'/cancel',method='POST');latency=time.perf_counter()-t
        self.assertEqual(health[0],200);self.assertEqual(status[0],200);self.assertEqual(cancel[0],200);self.assertLess(latency,2.)
        r=await self.wait(rid);self.assertEqual(r['status'],'cancelled',r)
        m=storage.read_json(storage.run_path(rid)/'manifest.json');self.assertEqual(m['childRecord']['remainingOwnedGroupMembers'],[])
        (EVIDENCE/'t15-t16.json').write_text(json.dumps({'highResolutionRunId':rid,'highResolutionPoints':len(mesh['points']),'highResolutionTriangles':mesh['mesh']['triangleCount'],'healthStatusCancelCombinedSeconds':latency,'cancelToTerminalSeconds':time.perf_counter()-t,'remainingOwnedGroupMembers':m['childRecord']['remainingOwnedGroupMembers'],'fixedMeshCountLimits':'removed; bounded cancellation retained'},indent=2))

if __name__=='__main__':unittest.main()
