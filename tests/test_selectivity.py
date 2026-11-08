"""Parent-authored parameter and result-transfer acceptance for round three."""
import asyncio,copy,json,math,unittest
from pathlib import Path
from backend import snapshots as snapshot_store
from backend import storage
from backend.main import app
from backend.models import parse_project
from backend.validation import validate_project,ProjectInputError
from backend.hashing import requested_input_hash
from tests.test_materials_milestone import project as materials_project
from tests.test_api_integration import asgi_request,json_body

ROOT=Path(__file__).resolve().parents[1];EVIDENCE=ROOT/'docs/selectivity-evidence'

def project(dim=2,model='isotropic'):
    p=materials_project(dim);p['schemaVersion']=4
    step={'id':'step_selective','name':'按材料刻蚀','enabled':True,'operation':'etch','model':model,'rateMode':'material','rateMagnitude':0.,'materialRates':{'Si':.2,'SiO2':.05,'Si3N4':0.},'selectivityReference':'Si','duration':.5}
    if model=='singleParticle':step.update(stickingProbability=.5,sourceExponent=1.)
    p['steps']=[step];return p

class RateContract(unittest.TestCase):
    def test_hash_and_rejection(self):
        for dim in (2,3):
            p=project(dim);h=requested_input_hash(validate_project(p)[0]);q=copy.deepcopy(p)
            q['steps'][0]['selectivityReference']='SiO2';q['steps'][0]['materialRates']={'Si3N4':0.,'SiO2':.05,'Si':.2}
            self.assertEqual(h,requested_input_hash(validate_project(q)[0]))
            q['steps'][0]['materialRates']['Si3N4']=.1;self.assertNotEqual(h,requested_input_hash(validate_project(q)[0]))
            for name,value in [('Si',-1.),('Si',True),('UnknownMaterial',.1),('Si',float('inf')),('Si',3.)]:
                q=copy.deepcopy(p);q['steps'][0]['materialRates'][name]=value
                with self.assertRaises(ProjectInputError):validate_project(q)
            # Material rate tables are now valid for deposition as well as etch.
            # The deposited material is created by duplicateTopLevelSet, so it is
            # a legal table key even though it is absent at the step start.
            q=copy.deepcopy(p);q['steps'][0].update(operation='deposit',depositMaterial='W')
            q['steps'][0]['materialRates']['W']=.1
            validate_project(q)
            q=copy.deepcopy(p)
            q['geometry']={'kind':'trench','width':1.6,'depth':1.,'taperAngle':0.,'maskHeight':.6,'maskTaperAngle':0.,'substrateMaterial':'Si','maskMaterial':'UnknownMaterial'}
            with self.assertRaises(ProjectInputError):validate_project(q)
            q['geometry']['maskMaterial']='Si3N4'
            # The trench geometry now only carries Si and Si3N4; keep the rate
            # table to materials that exist at this step under the stricter
            # availability check.
            q['steps'][0]['materialRates']={'Si':.2,'Si3N4':0.}
            validate_project(q)
        q=project(2,'singleParticle');q['domain'].update(xExtent=40.,gridDelta=.005);q['solver']['raysPerPoint']=4096
        validate_project(q)
        q['steps'][0]['materialRates']={name:0. for name in q['steps'][0]['materialRates']}
        validate_project(q)

class RateWorkflow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):self.life=app.router.lifespan_context(app);await self.life.__aenter__()
    async def asyncTearDown(self):await self.life.__aexit__(None,None,None)
    async def wait(self,rid):
        for _ in range(600):
            r=json_body(await asgi_request('/api/runs/'+rid))
            if r['status'] in ('succeeded','failed','cancelled','timed_out','interrupted'):
                self.assertEqual(r['status'],'succeeded',r.get('failureReason'));return r
            await asyncio.sleep(.05)
        self.fail('acceptance timeout')
    async def execute(self,p):
        response=await asgi_request('/api/runs',method='POST',payload={'kind':'simulate','project':p})
        self.assertEqual(response[0],202,response[2]);rid=json_body(response)['runId'];return rid,await self.wait(rid)
    def snap(self,rid,sid):
        snapshot_store.ensure_materialized(storage.run_path(rid),sid)
        return storage.read_json(storage.run_path(rid)/(sid+'.json'))
    async def compare_reference(self,p,label):
        rid,info=await self.execute(p);source=EVIDENCE/(label+'-input.json');dest=EVIDENCE/(label+'-reference.json');source.write_text(json.dumps(p))
        cmd=[str(ROOT/'.venv/bin/python'),str(ROOT/'scripts/bounded_child.py'),'--cwd',str(ROOT),'--record',str(EVIDENCE/(label+'-guard.json')),'--log-path',str(EVIDENCE/(label+'-reference.log')),'--timeout-seconds','30','--',str(ROOT/'.venv/bin/python'),str(ROOT/'tests/fixtures/selectivity_direct_reference.py'),str(source),str(dest)]
        child=await asyncio.create_subprocess_exec(*cmd,stdout=asyncio.subprocess.DEVNULL);self.assertEqual(await child.wait(),0)
        direct=json.loads(dest.read_text());errors=[];key='lines' if p['dimension']==2 else 'triangles'
        for row in info['snapshots']:
            s=self.snap(rid,row['stepId']);d=direct[row['stepId']]
            self.assertEqual(s['schemaVersion'],4);self.assertEqual(s['dimension'],p['dimension'])
            self.assertEqual(s[key],d[key]);self.assertEqual(s['cellData']['materialIds'],d['materialIds']);self.assertEqual(s['materialIdsByLevelSetIndex'],d['materialMap'])
            self.assertEqual(len(s['points']),len(d['points']));error=max((math.dist(a,b) for a,b in zip(s['points'],d['points'])),default=0.)
            self.assertLessEqual(error,1e-10);errors.append(error)
        (EVIDENCE/(label+'-comparison.json')).write_text(json.dumps({'runId':rid,'maximumCoordinateError':max(errors),'topologyAndMaterialsEqual':True,'dimension':p['dimension'],'model':p['steps'][0]['model']},indent=2))
        return rid,info
    async def test_zero_default_and_mask_rates(self):
        for dim in (2,3):
            for model in ('isotropic','singleParticle'):
                p=project(dim,model);rid,_=await self.compare_reference(p,f'zero-fallback-{dim}-{model}')
                s=self.snap(rid,'step_selective');self.assertFalse(s['noOp'])
                manifest=storage.read_json(storage.run_path(rid)/'manifest.json')
                if model=='singleParticle':self.assertEqual(manifest['actualSeed'],314159)
                p['steps'][0]['materialRates']['Si3N4']=.1;p['steps'][0]['rateMagnitude']=.02
                await self.compare_reference(p,f'mask-erosion-{dim}-{model}')
                p['geometry']['layers'][-1]['material']='Mask'
                p['steps'][0]['materialRates']['Mask']=p['steps'][0]['materialRates'].pop('Si3N4')
                await self.compare_reference(p,f'native-mask-{dim}-{model}')
    async def test_noop_and_replay(self):
        for dim in (2,3):
            p=project(dim,'singleParticle');p['steps'].append({**p['steps'][0],'id':'step_allzero','rateMagnitude':0.,'materialRates':{'Si':0.,'SiO2':0.,'Si3N4':0.}})
            rid,_=await self.compare_reference(p,f'replay-{dim}')
            self.assertTrue(self.snap(rid,'step_allzero')['noOp']);self.assertEqual(self.snap(rid,'step_selective')['points'],self.snap(rid,'step_allzero')['points'])
            replay=json_body(await asgi_request('/api/runs/'+rid+'/replay-project'));self.assertEqual(replay['schemaVersion'],4)
            response=await asgi_request('/api/runs/replay',method='POST',payload=replay);self.assertEqual(response[0],202,response[2]);rid2=json_body(response)['runId'];await self.wait(rid2)
            self.assertEqual(self.snap(rid,'step_selective')['points'],self.snap(rid2,'step_selective')['points'])
            saved=await asgi_request('/api/projects',method='POST',payload=p);self.assertEqual(saved[0],201,saved[2]);pid=json_body(saved)['id'];loaded=json_body(await asgi_request('/api/projects/'+pid))['project']
            self.assertEqual(loaded['steps'][0]['materialRates'],p['steps'][0]['materialRates'])
