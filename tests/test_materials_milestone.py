"""Parent-authored interface acceptance; no experimental physics calibration."""
import asyncio,copy,json,math,subprocess,unittest,zipfile,io
from pathlib import Path
from backend import snapshots as snapshot_store
from backend import storage
from backend.main import app
from backend.models import parse_project
from backend.hashing import requested_input_hash
from backend.validation import validate_project,ProjectInputError,select_engine
from tests.test_api_integration import asgi_request,json_body

ROOT=Path(__file__).resolve().parents[1]
EVIDENCE=ROOT/'docs/materials-evidence'

def project(dim=2):
    p={'schemaVersion':3,'name':'材料与多层结构验收','dimension':dim,
       'units':{'length':'um','time':'s','rate':'um/s','angle':'deg'},
       'domain':{'gridDelta':.2,'xExtent':4.,**({'xBoundary':'reflective'} if dim==2 else {'yExtent':4.,'boundaryXY':'reflective'})},
       'geometry':{'kind':'stack','baseHeight':0.,'substrateMaterial':'Si','layers':[
           {'id':'oxide_layer','name':'氧化层','material':'SiO2','thickness':.6,'role':'film'},
           {'id':'nitride_layer','name':'掩膜','material':'Si3N4','thickness':.6,'role':'mask'}],
           'pattern':{'kind':'trench','width':1.6,'taperAngle':0.}},
       'steps':[],'solver':{'timeStepRatio':.25,'raysPerPoint':32,'cpuThreads':1,'random':{'mode':'fixed','seed':314159},'enginePreference':'cpu','timeoutSeconds':30}}
    if dim==2:p['view']={'panX':0.,'panH':0.,'zoom':1.,'showGrid':True}
    else:p['view3D']={'projection':'parallel','camera':None,'displayMode':'surface','visibleMaterialIds':[],'opacityByMaterial':{},'clip':{'enabled':False,'axis':'x','position':0.,'keep':'positive'},'section':{'enabled':False,'axis':'y','position':0.}}
    return p

def step(model='isotropic',operation='etch',material='W',rate=.2):
    s={'id':'step_materials','name':'材料接口测试','enabled':True,'model':model,'operation':operation,'rateMagnitude':rate,'duration':.5}
    if model=='singleParticle':s.update(stickingProbability=.5,sourceExponent=1.)
    if operation=='deposit':s['depositMaterial']=material
    return s

class MaterialsContract(unittest.TestCase):
    def test_validation_and_hash(self):
        for dim in (2,3):
            p=project(dim);base=requested_input_hash(validate_project(p)[0]);q=copy.deepcopy(p)
            q['geometry']['layers'][0].update(id='renamed_id',name='renamed')
            self.assertEqual(base,requested_input_hash(validate_project(q)[0]))
            q['geometry']['layers'][0]['thickness']=.8
            self.assertNotEqual(base,requested_input_hash(validate_project(q)[0]))
            invalid=[]
            q=copy.deepcopy(p);q['geometry']['layers'][0]['thickness']=.05;invalid.append(q)
            q=copy.deepcopy(p);q['geometry']['layers'][0]['material']='Si3N4';invalid.append(q)
            q=copy.deepcopy(p);q['geometry']['substrateMaterial']='not_a_material';invalid.append(q)
            q=copy.deepcopy(p);q['geometry']['pattern']['width']=8.;invalid.append(q)
            q=copy.deepcopy(p);q['geometry']['layers'][0]['unexpected']=True;invalid.append(q)
            q=copy.deepcopy(p);q['geometry']['layers'][0]['thickness']=True;invalid.append(q)
            for q in invalid:
                with self.assertRaises(ProjectInputError):validate_project(q)
            q=copy.deepcopy(p);q['steps']=[step('singleParticle')];q['solver']['enginePreference']='gpu'
            normalized,_=validate_project(q)
            normalized=normalized.model_dump(mode='json',exclude_none=True)
            self.assertEqual(select_engine(normalized,normalized['steps'][0]),'GPU_DISK')

class MaterialsWorkflow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.life=app.router.lifespan_context(app);await self.life.__aenter__()
    async def asyncTearDown(self):await self.life.__aexit__(None,None,None)
    async def wait(self,rid):
        for _ in range(600):
            r=json_body(await asgi_request('/api/runs/'+rid))
            if r['status'] in ('succeeded','failed','cancelled','timed_out','interrupted'):
                self.assertEqual(r['status'],'succeeded',r.get('failureReason'));return r
            await asyncio.sleep(.05)
        self.fail('native worker exceeded acceptance timeout')
    async def execute(self,p):
        response=await asgi_request('/api/runs',method='POST',payload={'kind':'simulate' if p['steps'] else 'preview','project':p})
        self.assertEqual(response[0],202,response[2]);rid=json_body(response)['runId'];return rid,await self.wait(rid)
    def snap(self,rid,sid):
        snapshot_store.ensure_materialized(storage.run_path(rid),sid)
        return storage.read_json(storage.run_path(rid)/(sid+'.json'))
    async def reference(self,p,label):
        path=EVIDENCE/(label+'-input.json');path.write_text(json.dumps(p));out=EVIDENCE/(label+'-reference.json')
        command=[str(ROOT/'.venv/bin/python'),str(ROOT/'scripts/bounded_child.py'),'--cwd',str(ROOT),'--record',str(EVIDENCE/(label+'-guard.json')),'--log-path',str(EVIDENCE/(label+'-reference.log')),'--timeout-seconds','30','--',str(ROOT/'.venv/bin/python'),str(ROOT/'tests/fixtures/materials_direct_reference.py'),str(path),str(out)]
        proc=await asyncio.create_subprocess_exec(*command,stdout=asyncio.subprocess.DEVNULL);self.assertEqual(await proc.wait(),0)
        return json.loads(out.read_text())
    async def assert_reference(self,p,label):
        rid,r=await self.execute(p);reference=await self.reference(p,label);errors=[]
        key='triangles' if p['dimension']==3 else 'lines'
        for row in r['snapshots']:
            s=self.snap(rid,row['stepId']);direct=reference[row['stepId']]
            self.assertEqual(s['schemaVersion'],3);self.assertEqual(s['dimension'],p['dimension'])
            self.assertFalse(s['sameMaterialInternalInterfacesHidden'])
            self.assertEqual(s[key],direct[key]);self.assertEqual(s['cellData']['materialIds'],direct['materialIds'])
            self.assertEqual(s['materialIdsByLevelSetIndex'],direct['materialMap'])
            self.assertEqual(len(s['points']),len(direct['points']))
            error=max((math.dist(a,b) for a,b in zip(s['points'],direct['points'])),default=0.)
            self.assertLessEqual(error,1e-10);errors.append(error)
        EVIDENCE.mkdir(exist_ok=True)
        (EVIDENCE/(label+'-comparison.json')).write_text(json.dumps({'runId':rid,'dimension':p['dimension'],'rawNativeTopologyAndMaterialArraysEqual':True,'maxCoordinateErrors':errors,'tolerance':1e-10},indent=2))
        return rid,r
    async def test_catalog_and_stack_reference(self):
        catalog=await asgi_request('/api/materials');self.assertEqual(catalog[0],200)
        names={x['name'] for x in json_body(catalog)['materials']}
        self.assertTrue({'Si','SiO2','Si3N4','PolySi','TiN','W'}<=names)
        for dim in (2,3):
            p=project(dim);await self.assert_reference(p,f'stack-{dim}d')
            p['geometry']['layers'].reverse();p['geometry']['pattern']={'kind':'none'}
            await self.assert_reference(p,f'reordered-{dim}d')
        p=project(3);p['geometry']['pattern']={'kind':'hole','radius':.8,'taperAngle':10.}
        await self.assert_reference(p,'stack-hole-3d')
    async def test_material_selection_and_deposition(self):
        for dim in (2,3):
            p=project(dim);p['geometry']={'kind':'plane','height':.4,'substrateMaterial':'TiN'}
            p['steps']=[step('isotropic','deposit','W')]
            rid,r=await self.assert_reference(p,f'tin-w-{dim}d')
            self.assertEqual(self.snap(rid,'step_materials')['materialIdsByLevelSetIndex'],[79,70])
            p['geometry']={'kind':'trench','width':1.6,'depth':1.2,'taperAngle':0.,'maskHeight':.6,'maskTaperAngle':0.,'substrateMaterial':'PolySi','maskMaterial':'Si3N4'}
            p['steps']=[step('singleParticle','etch')]
            await self.assert_reference(p,f'polysi-mask-{dim}d')
    async def test_save_replay_and_repeated_materials(self):
        for dim in (2,3):
            p=project(dim);p['geometry']['layers'][1].update(material='SiO2',role='film');p['geometry']['pattern']={'kind':'none'}
            p['steps']=[step('singleParticle','etch'),dict(step('isotropic','deposit','W',0.),id='step_nooptest')]
            rid,r=await self.assert_reference(p,f'repeated-materials-{dim}d')
            response=await asgi_request('/api/projects',method='POST',payload=p);self.assertEqual(response[0],201,response[2])
            pid=json_body(response)['id'];loaded=json_body(await asgi_request('/api/projects/'+pid))['project']
            self.assertEqual(loaded['geometry'],p['geometry'])
            replay=json_body(await asgi_request('/api/runs/'+rid+'/replay-project'));self.assertEqual(replay['schemaVersion'],3)
            response=await asgi_request('/api/runs/replay',method='POST',payload=replay);self.assertEqual(response[0],202,response[2])
            rid2=json_body(response)['runId'];await self.wait(rid2)
            self.assertEqual(self.snap(rid,'step_materials')['points'],self.snap(rid2,'step_materials')['points'])
            self.assertEqual(self.snap(rid,'step_nooptest')['materialIdsByLevelSetIndex'],[10,30,30])
            archive=await asgi_request('/api/runs/'+rid+'/download');self.assertEqual(archive[0],200)
            with zipfile.ZipFile(io.BytesIO(archive[2])) as z:self.assertIn('replay-project.json',z.namelist())
