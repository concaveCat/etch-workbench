"""Verify concrete 3D GPU combinations in the host shell, without system changes.

Use --host-execution-confirmed only from the host WSL/Linux shell. Each native
case is a fresh bounded child; the API process never initializes CUDA.
"""
import argparse,json,platform,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from backend.storage import atomic_json
OUT=ROOT/'var/evidence/gpu-host';PYTHON=ROOT/'.venv/bin/python'
def idle():
    # Do not probe while either the formal installation or any isolated
    # candidate data directory owns a live run. This is read-only and avoids
    # touching an existing service or its user data.
    roots=(ROOT,Path('/root/viennaps-workbench-selectivity'),Path('/root/viennaps-workbench'))
    for root in roots:
        var=root/'var'
        if not var.is_dir():
            continue
        for p in var.glob('**/runs/*/manifest.json'):
            try:
                m=json.loads(p.read_text())
            except (OSError,ValueError):
                continue
            if m.get('status') in ('pending','running','cancelling'):
                raise RuntimeError(f'先等待活动任务结束: {p}')
def run_case(engine,boundary,operation):
    name=f'{engine}-{boundary}-{operation}';OUT.mkdir(parents=True,exist_ok=True)
    case=OUT/name
    if case.exists():
        suffix=time.strftime('%Y%m%d-%H%M%S',time.gmtime());case=OUT/(name+'-'+suffix)
    case.mkdir()
    record=case/'guard.json';log=case/'run.log'
    cmd=[str(PYTHON),str(ROOT/'scripts/bounded_child.py'),'--cwd',str(ROOT),'--record',str(record),'--log-path',str(log),
         '--timeout-seconds','45','--max-rss-mib','2048','--max-log-bytes','1048576','--',str(PYTHON),
         str(ROOT/'worker/probe_gpu_3d_case.py'),engine,operation,boundary,str(case)]
    completed=subprocess.run(cmd,cwd=ROOT,check=False)
    if completed.returncode==0 and (case/'result.json').is_file():
        row=json.loads((case/'result.json').read_text());row['evidence']=str((case/'result.json').relative_to(ROOT))
        row['host']={'hostname':platform.node(),'platform':platform.platform(),'device':row['device']}
        print('PASS',name,flush=True);return row,None
    tail=log.read_text(errors='replace')[-12000:] if log.exists() else 'no native log'
    failure={'case':name,'returnCode':completed.returncode,'guard':str(record.relative_to(ROOT)),'log':str(log.relative_to(ROOT)),'errorTail':tail}
    print('UNAVAILABLE',name,tail[-600:],flush=True);return None,failure
def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host-execution-confirmed',action='store_true',help='confirm that this command is running in the host shell')
    args=parser.parse_args()
    if not args.host_execution_confirmed:parser.error('host probe requires --host-execution-confirmed; this check must run outside the isolated agent sandbox')
    idle();rows=[];failures=[];engine_name=None;gpu_init_failed=False
    # Try DISK for all four model/operation/boundary rows. Use TRIANGLE only
    # for combinations not supported by GPU_DISK. Every supported row is real.
    disk_failures=[]
    for boundary in ['reflective','periodic']:
        for operation in ['etch','deposit']:
            idle();row,failure=run_case('GPU_DISK',boundary,operation)
            if row:rows.append(row)
            elif failure:
                failures.append(failure)
                if 'cuInit' in failure['errorTail'] or 'gpuAvailable returned false' in failure['errorTail']:
                    gpu_init_failed=True;break
                disk_failures.append((boundary,operation))
        if gpu_init_failed:break
    if not gpu_init_failed:
        for boundary,operation in disk_failures:
            idle();row,failure=run_case('GPU_TRIANGLE',boundary,operation)
            if row:rows.append(row)
            elif failure:failures.append(failure)
    summary={'status':'verified-combinations' if rows else 'no-verified-gpu-combinations',
       'executionScope':'explicit host-shell invocation','rows':rows,'failures':failures,'gpuInitializationFailed':gpu_init_failed,
       'noFallback':True,'checkedAt':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())}
    OUT.mkdir(parents=True,exist_ok=True);atomic_json(OUT/'summary.json',summary)
    p=ROOT/'docs/capabilities-3d.json';cap=json.loads(p.read_text())
    cap['rows']=[r for r in cap['rows'] if not r['engine'].startswith('GPU_')]+rows
    cap['gpu']={'exposed':bool(rows),'status':'verified-host' if rows else 'host-probe-no-verified-combination','evidence':'var/evidence/gpu-host/summary.json'}
    atomic_json(p,cap);p=ROOT/'docs/capabilities.json';whole=json.loads(p.read_text());whole['threeDimensional']=cap;atomic_json(p,whole)
    print(json.dumps({'status':summary['status'],'verifiedRows':len(rows),'summary':str(OUT/'summary.json')},ensure_ascii=False))
if __name__=='__main__':main()
