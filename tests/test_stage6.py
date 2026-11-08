"""Stage 6 acceptance: stable custom ViennaPS materials and native Domain outputs."""
from __future__ import annotations
import asyncio, hashlib, json, math, subprocess, unittest, zipfile, io
from pathlib import Path
from urllib.parse import quote
from backend.engine import engine_version
from backend import storage
from backend.main import app
from tests.test_api_integration import asgi_request, json_body

ROOT=Path(__file__).resolve().parents[1]
EVIDENCE=ROOT/'docs/stage6-evidence'


def make_project(dim,material):
    p={'schemaVersion':7,'name':f'自定义材料 {dim}D','dimension':dim,
       'units':{'length':'um','time':'s','rate':'um/s','angle':'deg'},
       'domain':{'gridDelta':.2,'xExtent':3.,**({'xBoundary':'reflective'} if dim==2 else {'yExtent':3.,'boundaryXY':'reflective'})},
       'geometry':{'kind':'plane','height':.4,'substrateMaterial':material},
       'steps':[{'id':'custom_etch_step','name':'显式配置的各向同性刻蚀','enabled':True,'operation':'etch','model':'isotropic','rateMagnitude':.2,'duration':.2,'rateMode':'uniform'}],
       'solver':{'timeStepRatio':.25,'raysPerPoint':32,'cpuThreads':1,'random':{'mode':'fixed','seed':314159},'enginePreference':'cpu','timeoutSeconds':60,
          'advection':{'spatialScheme':'ENGQUIST_OSHER_1ST_ORDER','temporalScheme':'FORWARD_EULER','adaptiveTimeStepping':False,'adaptiveTimeStepSubdivisions':20,'calculateIntermediateVelocities':False,'checkDissipation':True,'dissipationAlpha':1.,'ignoreVoids':False,'velocityOutput':False},
          'rayTracing':{'diskRadius':0.,'ignoreFluxBoundaries':False,'maxBoundaryHits':1000,'maxReflections':4294967295,'minNodeDistanceFactor':.05,'normalizationType':'SOURCE','smoothingNeighbors':1}},
       'outputSettings':{'levelSets':True,'levelSetMeshes':True,'surfaceMeshVtk':True,'volumeMesh':dim==3,'nativeMetadata':True}}
    if dim==2:p['view']={'panX':0.,'panH':0.,'zoom':1.,'showGrid':True}
    else:p['view3D']={'projection':'parallel','camera':None,'displayMode':'surface','visibleMaterialIds':[],'opacityByMaterial':{},'clip':{'enabled':False,'axis':'x','position':0.,'keep':'positive'},'section':{'enabled':False,'axis':'y','position':0.}}
    return p


class Stage6MaterialsAndOutputs(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        EVIDENCE.mkdir(parents=True,exist_ok=True)
        self.life=app.router.lifespan_context(app);await self.life.__aenter__()
    async def asyncTearDown(self):await self.life.__aexit__(None,None,None)
    async def wait(self,rid):
        for _ in range(1200):
            result=json_body(await asgi_request('/api/runs/'+rid))
            if result['status'] in ('succeeded','failed','cancelled','timed_out','interrupted'):
                self.assertEqual(result['status'],'succeeded',result.get('failureReason'));return result
            await asyncio.sleep(.05)
        self.fail('native child exceeded acceptance timeout')
    async def snapshot(self,rid,sid):return json_body(await asgi_request(f'/api/runs/{rid}/snapshots/{sid}'))
    async def direct_reference(self,project,label):
        directory=EVIDENCE/label;directory.mkdir(parents=True,exist_ok=True)
        project_file=directory/'project.json';project_file.write_text(json.dumps(project,ensure_ascii=False,indent=2)+'\n')
        custom_file=directory/'custom-materials.json';custom_file.write_text(json.dumps(project['materialDefinitions'],ensure_ascii=False,indent=2)+'\n')
        output=directory/'direct-output'
        command=[str(ROOT/'.venv/bin/python'),str(ROOT/'scripts/bounded_child.py'),'--cwd',str(ROOT),'--record',str(directory/'direct.guard.json'),'--log-path',str(directory/'direct.log'),'--timeout-seconds','60','--max-rss-mib','1536','--max-log-bytes','300000','--',str(ROOT/'.venv/bin/python'),str(ROOT/'tests/fixtures/stage6_material_output_reference.py'),str(project_file),str(custom_file),str(output)]
        proc=await asyncio.create_subprocess_exec(*command,stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL);self.assertEqual(await proc.wait(),0,(directory/'direct.log').read_text(errors='replace')[-2000:])
        return json.loads((output/'reference.json').read_text()),output
    async def test_custom_registry_materials_replay_and_native_output_roundtrip(self):
        catalog=json_body(await asgi_request('/api/materials'))
        self.assertEqual(catalog['engineVersion'],engine_version())
        first=await asgi_request('/api/materials',method='POST',payload={'name':'WB_Custom_Oxide','color':'#123456','densityGcm3':2.1,'conductive':False,'description':'Stage 6 reference material'})
        self.assertEqual(first[0],201,first[2]);first=json_body(first)
        second=await asgi_request('/api/materials',method='POST',payload={'name':'WB_Custom_Film','color':'#8b5cf6','densityGcm3':1.6,'conductive':True,'description':'second ordered native ID'})
        self.assertEqual(second[0],201,second[2]);second=json_body(second)
        self.assertEqual((first['id'],first['customId']),(177,0));self.assertEqual((second['id'],second['customId']),(178,1))
        self.assertEqual((await asgi_request('/api/materials',method='POST',payload={'name':'aC','color':'#ffffff'}))[0],422)
        self.assertEqual((await asgi_request('/api/materials',method='POST',payload={'name':'wb_custom_film','color':'#ffffff'}))[0],422)
        updated=await asgi_request('/api/materials/WB_Custom_Film',method='PATCH',payload={'color':'#2468ac','densityGcm3':2.25,'conductive':True,'description':'updated, still label-only'})
        self.assertEqual(updated[0],200,updated[2]);updated=json_body(updated)
        self.assertEqual((updated['id'],updated['customId'],updated['color'],updated['densityGcm3']),(178,1,'#2468ac',2.25))
        material_rows=json_body(await asgi_request('/api/materials'))['materials']
        custom=[row for row in material_rows if row.get('isCustom')]
        self.assertEqual([row['id'] for row in custom],[177,178])
        self.assertEqual((await asgi_request('/api/materials/Si',method='PATCH',payload={'color':'#ffffff'}))[0],404)

        for dim in (2,3):
            project=make_project(dim,'WB_Custom_Film')
            validation=await asgi_request('/api/projects/validate',method='POST',payload=project)
            self.assertEqual(validation[0],200,validation[2]);normalized=json_body(validation)['normalizedProject']
            self.assertEqual([(row['name'],row['id'],row['customId']) for row in normalized['materialDefinitions']],[(row['name'],row['id'],row['customId']) for row in custom])
            direct,refdir=await self.direct_reference(normalized,f'custom-{dim}d')
            response=await asgi_request('/api/runs',method='POST',payload={'kind':'simulate','project':normalized})
            self.assertEqual(response[0],202,response[2]);rid=json_body(response)['runId'];await self.wait(rid)
            reference=direct['snapshots']
            for sid in ('initial','custom_etch_step'):
                snapshot=await self.snapshot(rid,sid);expected=reference[sid]['mesh']
                cell_key='triangles' if dim==3 else 'lines'
                self.assertEqual(snapshot[cell_key],expected['cells'],sid)
                self.assertEqual(snapshot['cellData']['materialIds'],expected['materialIds'],sid)
                self.assertEqual(snapshot['materialIdsByLevelSetIndex'],expected['materialIdsByLevelSetIndex'],sid)
                self.assertEqual(snapshot['materialIdsByLevelSetIndex'],[178])
                self.assertEqual(snapshot['points'],expected['points'],sid)
                self.assertEqual(snapshot['outputs'] and sorted(row['kind'] for row in snapshot['outputs']),
                    sorted(row['kind'] for row in reference[sid]['outputs']))
                expected_outputs={(row['kind'],row.get('levelSetIndex')):row for row in reference[sid]['outputs']}
                actual_outputs={(row['kind'],row.get('levelSetIndex')):row for row in snapshot['outputs']}
                self.assertEqual(set(actual_outputs),set(expected_outputs))
                for key,artifact in actual_outputs.items():
                    actual_path=storage.run_path(rid)/artifact['fileName']
                    direct_path=refdir/sid/expected_outputs[key]['fileName']
                    self.assertEqual(actual_path.stat().st_size,artifact['sizeBytes'])
                    self.assertEqual(storage.file_sha256(actual_path),artifact['sha256'])
                    self.assertEqual(actual_path.read_bytes(),direct_path.read_bytes(),(dim,sid,key))
                    self.assertEqual(hashlib.sha256(actual_path.read_bytes()).hexdigest(),artifact['sha256'])
                metadata_file=next(row for row in snapshot['outputs'] if row['kind']=='nativeMetadata')
                native_meta=json.loads((storage.run_path(rid)/metadata_file['fileName']).read_text())
                expected_meta=json.loads((refdir/sid/next(row for row in reference[sid]['outputs'] if row['kind']=='nativeMetadata')['fileName']).read_text())
                self.assertEqual(native_meta['values'],expected_meta['values'])
                self.assertEqual(native_meta['metadataLevel'],expected_meta['metadataLevel'])
                self.assertIsNone(native_meta['nativeUnit'])
                self.assertIn('no point/cell association',native_meta['association'])
            manifest=storage.read_json(storage.run_path(rid)/'manifest.json')
            self.assertEqual([(row['name'],row['id']) for row in manifest['materialRegistry']],[('WB_Custom_Oxide',177),('WB_Custom_Film',178)])
            actual_parameters=manifest['actualExecution'][0]['actualParameters']
            self.assertEqual(actual_parameters['actualDefaultRate'],-.2)
            self.assertEqual(actual_parameters['actualMaterialRatesByName'],{})
            saved=await asgi_request('/api/projects',method='POST',payload=normalized);self.assertEqual(saved[0],201,saved[2]);pid=json_body(saved)['id']
            reopened=json_body(await asgi_request('/api/projects/'+pid))['project']
            self.assertEqual([(row['name'],row['customId']) for row in reopened['materialDefinitions']],[('WB_Custom_Oxide',0),('WB_Custom_Film',1)])
            replay=json_body(await asgi_request(f'/api/runs/{rid}/replay-project'))
            rerun=await asgi_request('/api/runs/replay',method='POST',payload=replay);self.assertEqual(rerun[0],202,rerun[2]);rid2=json_body(rerun)['runId'];await self.wait(rid2)
            for sid in ('initial','custom_etch_step'):
                original=await self.snapshot(rid,sid);replayed=await self.snapshot(rid2,sid)
                cell_key='triangles' if dim==3 else 'lines'
                for key in ('points','materialIdsByLevelSetIndex'):
                    self.assertEqual(original[key],replayed[key],(dim,sid,key))
                self.assertEqual(original[cell_key],replayed[cell_key],(dim,sid,cell_key))
                self.assertEqual(original['cellData'],replayed['cellData'],(dim,sid,'materials'))
            archive=await asgi_request(f'/api/runs/{rid}/download')
            self.assertEqual(archive[0],200,archive[2][:200])
            with zipfile.ZipFile(io.BytesIO(archive[2])) as zipped:
                names=zipped.namelist();self.assertTrue(any(name.endswith('.lvst') for name in names));self.assertTrue(any(name.endswith('.vtp') for name in names))
                if dim==3:self.assertTrue(any(name.endswith('.vtu') for name in names))
                project_json=json.loads(zipped.read('project.json'))
                self.assertEqual([(row['name'],row['id']) for row in project_json['materialDefinitions']],[('WB_Custom_Oxide',177),('WB_Custom_Film',178)])
            browser_evidence={'runId':rid,'replayRunId':rid2,'dimension':dim,'customMaterialId':178,'customRegistryStableAcrossWorkerProcesses':True,
                'directViennaPSMeshExact':True,'nativeOutputKinds':sorted({row['kind'] for sid in ('initial','custom_etch_step') for row in (await self.snapshot(rid,sid))['outputs']}),
                'nativeOutputFilesHashAndBytesMatchDirectReference':True,'saveReopenReplayRunZip':True}
            (EVIDENCE/f'custom-{dim}d-acceptance.json').write_text(json.dumps(browser_evidence,ensure_ascii=False,indent=2)+'\n')
