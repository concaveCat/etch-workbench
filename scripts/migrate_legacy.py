"""Copy only persistent legacy data, preserving checksums and refusing conflicts.

Default is a read-only plan. --apply requires the preserved app to be stopped.
It never imports ViennaPS and never uses historical PID records to signal a process.
"""
import argparse,hashlib,json,os,sys,tempfile,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from backend.storage import atomic_json
SOURCE=Path('/root/viennaps-workbench')
SKIP={'.guard-ready'}
def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def source_running():
    try:
        r=json.loads((SOURCE/'var/server.json').read_text());raw=Path(f'/proc/{int(r["pid"])}/stat').read_text();fields=raw[raw.rfind(')')+2:].split()
        cmd=Path(f'/proc/{r["pid"]}/cmdline').read_bytes()
        return fields[0]!='Z' and int(fields[19])==r['startTicks'] and str(SOURCE/'backend/serve.py').encode() in cmd
    except (OSError,ValueError,KeyError):return False

def inventory():
    rows=[]
    for kind in ('projects','runs'):
        for p in (SOURCE/'var'/kind).rglob('*'):
            if p.is_symlink():raise RuntimeError(f'refuse symlink: {p}')
            if not p.is_file() or p.name in SKIP or p.name=='.keep':continue
            if kind=='runs' and p.name=='manifest.json':
                if json.loads(p.read_text()).get('status') in ('pending','running','cancelling'):raise RuntimeError('legacy application has active computation')
            relative=p.relative_to(SOURCE/'var');target=ROOT/'var'/relative;sha=digest(p)
            if target.exists() and (not target.is_file() or digest(target)!=sha):raise RuntimeError(f'candidate ID/file collision, refusing overwrite: {relative}')
            rows.append({'file':str(relative),'bytes':p.stat().st_size,'sha256':sha,'existsIdentical':target.exists()})
    return sorted(rows,key=lambda r:r['file'])
def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    if args.apply and source_running():raise RuntimeError('stop the verified preserved workbench service before copying persistent data')
    rows=inventory();copied=[]
    if args.apply:
        for r in rows:
            src=SOURCE/'var'/r['file'];dst=ROOT/'var'/r['file'];dst.parent.mkdir(parents=True,exist_ok=True)
            data=src.read_bytes()
            if hashlib.sha256(data).hexdigest()!=r['sha256']:raise RuntimeError('source changed during migration; rerun with the old service stopped')
            if not dst.exists():
                # Exclusive creation prevents a candidate write being overwritten.
                fd=os.open(dst,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
                with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
                copied.append(r['file'])
            if digest(dst)!=r['sha256']:raise RuntimeError('target checksum mismatch')
        after=inventory()
        if [(r['file'],r['sha256']) for r in after]!=[(r['file'],r['sha256']) for r in rows]:raise RuntimeError('legacy data changed during copy')
    report={'status':'copied-and-verified' if args.apply else 'plan-only','source':str(SOURCE/'var'),'target':str(ROOT/'var'),'files':rows,'copiedFiles':copied,
        'projectCount':sum(r['file'].startswith('projects/') and r['file'].endswith('.json') for r in rows),'runCount':sum(r['file'].startswith('runs/') and r['file'].endswith('/manifest.json') for r in rows),
        'serviceStateExcluded':['server.json','server.log','workbench.lock','PID/readiness files'],'historicalProcessRecords':'retained as evidence only; all copied source runs terminal'}
    atomic_json(ROOT/'var/evidence'/('migration-copy.json' if args.apply else 'migration-plan.json'),report)
    print(json.dumps({k:v for k,v in report.items() if k not in ('files','copiedFiles')},ensure_ascii=False))
if __name__=='__main__':main()
