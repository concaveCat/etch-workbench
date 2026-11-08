"""Promote the verified local candidate, or restore the preserved 2D service.

Only signals services matched by their own identity scripts. Never kills an
unknown listener, never changes networking, and never deletes project/run data.
"""
import argparse,json,os,subprocess,sys,urllib.request
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];OLD=Path('/root/viennaps-workbench')
def command(root,action,port=None):
    env=os.environ.copy()
    if port:env['WORKBENCH_PORT']=str(port)
    subprocess.run([str(root/'.venv/bin/python'),str(root/'scripts/manage_server.py'),action],cwd=root,env=env,check=True)
def health(root):
    r=json.loads((root/'var/server.json').read_text());url=f'http://127.0.0.1:{r["port"]}/api/health'
    with urllib.request.urlopen(url,timeout=2) as f:return json.load(f)
def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--rollback',action='store_true');p.add_argument('--port',type=int,default=8766);a=p.parse_args()
    if a.rollback:
        if (ROOT/'var/server.json').exists():
            if health(ROOT).get('activeRunId'):raise RuntimeError('candidate has active work; cancel or finish it before rollback')
            command(ROOT,'stop')
        from backend.storage import atomic_json
        atomic_json(ROOT/'var/listen-port.json',{'port':8767})
        command(OLD,'start');print('Preserved 2D service restored. Candidate 3D data retained.');return
    result=json.loads((ROOT/'var/evidence/browser-3d.json').read_text())
    if result['status']!='passed':raise RuntimeError('real-browser acceptance has not passed; preserve original entry point')
    if health(OLD).get('activeRunId'):raise RuntimeError('preserved workbench has active computation')
    if health(ROOT).get('activeRunId'):raise RuntimeError('candidate has active computation')
    # Release only the two applications that we own. Port collision at new startup
    # is a failure, never an excuse to terminate an unrelated process.
    for log in ['backend-final-tests.log','regression-r3.log']:
        if '\nOK' not in (ROOT/'var/evidence'/log).read_text():raise RuntimeError('required backend checks have not passed')
    command(ROOT,'stop');command(OLD,'stop')
    try:
        subprocess.run([str(ROOT/'.venv/bin/python'),str(ROOT/'scripts/migrate_legacy.py'),'--apply'],cwd=ROOT,check=True)
        from backend.storage import atomic_json
        atomic_json(ROOT/'var/listen-port.json',{'port':a.port})
        command(ROOT,'start',a.port)
        with urllib.request.urlopen(f'http://127.0.0.1:{a.port}/api/projects',timeout=2) as f:projects=json.load(f)['projects']
        with urllib.request.urlopen(f'http://127.0.0.1:{a.port}/api/runs',timeout=2) as f:runs=json.load(f)['runs']
        report={'status':'promoted','url':f'http://127.0.0.1:{a.port}/','projectCount':len(projects),'runCount':len(runs),'migrationReport':'var/evidence/migration-copy.json','oldDirectoryRetained':str(OLD),'rollback':f'{ROOT}/.venv/bin/python {ROOT}/scripts/promote.py --rollback'}
        from backend.storage import atomic_json
        atomic_json(ROOT/'var/evidence/deployment-3d.json',report);print(json.dumps(report,ensure_ascii=False))
    except BaseException:
        command(ROOT,'stop')
        atomic_json(ROOT/'var/listen-port.json',{'port':8767})
        command(OLD,'start');raise
if __name__=='__main__':
    sys.path.insert(0,str(ROOT));main()
