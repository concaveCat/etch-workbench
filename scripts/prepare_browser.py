"""Prepare a small pair of larger real meshes and a legacy history entry for T17."""
import asyncio,copy,json,sys,shutil
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from backend import storage
from backend.main import app
from tests.test_api_integration import asgi_request,json_body
async def main():
    old=Path('/root/viennaps-workbench/var/runs')
    for f in old.glob('*/manifest.json'):
        if storage.read_json(f).get('status') in ('pending','running','cancelling'):raise RuntimeError('旧工作台有活动计算')
    storage.initialize()
    report={'runIds':[]}
    async with app.router.lifespan_context(app):
        p=storage.read_json(ROOT/'tests/fixtures/project-3d.json');p['domain']={'gridDelta':.1,'xExtent':6.,'yExtent':4.,'boundaryXY':'reflective'};p['steps']=[]
        for radius in [1.,1.2]:
            p['geometry']['radius']=radius
            response=await asgi_request('/api/runs',method='POST',payload={'kind':'preview','project':p})
            if response[0]!=202:raise RuntimeError(response)
            rid=json_body(response)['runId']
            for _ in range(600):
                r=json_body(await asgi_request('/api/runs/'+rid))
                if r['status'] in ('succeeded','failed','timed_out'):break
                await asyncio.sleep(.05)
            if r['status']!='succeeded':raise RuntimeError(r)
            report['runIds'].append(rid)
    fixture=storage.read_json(ROOT/'tests/fixtures/legacy/index.json')[0]
    rid=fixture['runId'];source=ROOT/'tests/fixtures/legacy'/rid;target=storage.RUNS/rid
    if target.exists():
        # This is the specifically identified G0 compatibility fixture. Refuse
        # unrelated content rather than overwriting an unknown candidate run.
        for name in ['project.json','replay-project.json','initial.json','initial.vtk']:
            if (target/name).read_bytes()!=(source/name).read_bytes():raise RuntimeError('legacy fixture collision')
    shutil.copytree(source,target,dirs_exist_ok=True)
    report['legacyRunId']=rid
    storage.atomic_json(storage.VAR/'evidence/browser-large.json',report)
asyncio.run(main())
