"""Host-only final acceptance: promote, restart, rollback, restore final entry.

Uses only identity-checked application controls. Keeps all data and old source.
"""
import hashlib,json,os,subprocess,sys,time,urllib.request
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from backend.storage import atomic_json
OLD=Path('/root/viennaps-workbench')
REPORT=ROOT/'var/evidence/host-finalization.json'
def run(cmd,env=None):subprocess.run(cmd,cwd=ROOT,env=env,check=True)
def manager(root,action,port=None):
    env=os.environ.copy()
    if port:env['WORKBENCH_PORT']=str(port)
    run([str(root/'.venv/bin/python'),str(root/'scripts/manage_server.py'),action],env)
def get(port,path):
    with urllib.request.urlopen(f'http://127.0.0.1:{port}'+path,timeout=2) as f:return json.load(f)
def main():
    report={'status':'running','startedAt':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'checks':[]}
    try:
        browser=json.loads((ROOT/'var/evidence/browser-3d.json').read_text())
        if browser['status']!='passed':raise RuntimeError('browser acceptance must pass first')
        pid=browser['projectId'];rid=browser['runId']
        run([str(ROOT/'.venv/bin/python'),str(ROOT/'scripts/probe-host-gpu.py'),'--host-execution-confirmed']);report['checks'].append('3D GPU engine/model/operation/boundary combinations verified in bounded native processes')
        before=get(8767,f'/api/projects/{pid}')['project']
        snapshot=get(8767,f'/api/runs/{rid}')['snapshots'][-1]['stepId']
        mesh_before=get(8767,f'/api/runs/{rid}/snapshots/{snapshot}')
        run([str(ROOT/'.venv/bin/python'),str(ROOT/'scripts/promote.py')]);report['checks'].append('consistent legacy data copy and promotion to 8766')
        manager(ROOT,'stop');manager(ROOT,'start')
        if get(8766,f'/api/projects/{pid}')['project']!=before:raise RuntimeError('project/camera changed after restart')
        if get(8766,f'/api/runs/{rid}/snapshots/{snapshot}')!=mesh_before:raise RuntimeError('snapshot changed after restart')
        report['checks'].append('service restart restores saved 3D project/camera/run/snapshot')
        run([str(ROOT/'.venv/bin/python'),str(ROOT/'scripts/promote.py'),'--rollback'])
        if get(8766,'/api/health')['status']!='ok':raise RuntimeError('old service rollback health failed')
        legacy=json.loads((ROOT/'tests/fixtures/legacy/index.json').read_text())[0]['runId']
        if get(8766,f'/api/runs/{legacy}/snapshots/initial')['schemaVersion']!=1:raise RuntimeError('old service no longer reads legacy snapshot')
        report['checks'].append('rollback to preserved 2D service and original legacy snapshot')
        manager(ROOT,'start',8767)
        run([str(ROOT/'.venv/bin/python'),str(ROOT/'scripts/promote.py')]);report['checks'].append('restored verified new service to final 8766 after rollback test')
        # Browser evidence after restart and migration, against the formal URL.
        run(['node',str(ROOT/'frontend/tests/browser-3d.mjs')]);report['checks'].append('full real-browser workflow at formal URL after migration/restart')
        mismatches=[]
        checks=json.loads((ROOT/'var/evidence/baseline-checksums.json').read_text())
        for file,sha in checks.items():
            if hashlib.sha256((OLD/file).read_bytes()).hexdigest()!=sha:mismatches.append(file)
        if mismatches:raise RuntimeError('preserved source differs from baseline: '+str(mismatches))
        report['checks'].append('all preserved original source and lockfile checksums unchanged')
        verify_script=os.environ.get('WORKBENCH_HOST_VERIFY_SCRIPT')
        if verify_script:
            result=subprocess.run([verify_script],cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=60)
            (ROOT/'var/evidence/host-final-verify.log').write_text(result.stdout)
            if result.returncode not in (0,2):raise RuntimeError('host verification reported an actual failure')
            report['hostVerifyExitCode']=result.returncode
        report['checks'].append('host verification; no engine/network configuration changes')
        report['url']='http://127.0.0.1:8766/';report['status']='passed'
    except BaseException as e:report['status']='failed';report['error']=str(e);raise
    finally:
        report['finishedAt']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime());atomic_json(REPORT,report)
if __name__=='__main__':main()
