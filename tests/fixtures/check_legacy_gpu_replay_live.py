import json
import pathlib
import time
import urllib.error
import urllib.request

source = pathlib.Path('/root/viennaps-workbench-cpu-gpu/var/stage9-3d-trench-cfl-015-ray512-representative-product-acceptance-2026-10-28/runs/044b5b51d0ab46e6ce6693d1')
root = pathlib.Path('/tmp/viennaps-native-multiion-workbench-candidate-2026-10-28/var/service-old-replay')
url = 'http://127.0.0.1:8809/api/runs'
request = urllib.request.Request(url + '/replay', data=(source / 'replay-project.json').read_bytes(), headers={'Content-Type': 'application/json'}, method='POST')
try:
    with urllib.request.urlopen(request, timeout=15) as response:
        started = json.load(response)
except urllib.error.HTTPError as exc:
    raise RuntimeError(f'HTTP {exc.code}: {exc.read().decode()}') from exc
print('started', json.dumps(started), flush=True)
run_id = started.get('runId') or started.get('manifest', {}).get('runId')
if not run_id:
    raise RuntimeError('missing runId')
for _ in range(180):
    with urllib.request.urlopen(url + '/' + run_id, timeout=15) as response:
        state = json.load(response)
    manifest = state.get('manifest', state)
    if manifest.get('status') in ('succeeded', 'failed', 'cancelled'):
        break
    time.sleep(1)
else:
    raise TimeoutError(run_id)
old = json.loads((source / 'manifest.json').read_text())
new = json.loads((root / 'runs' / run_id / 'manifest.json').read_text())
report = {'sourceRunId': old['runId'], 'runId': run_id, 'status': new['status'], 'failureReason': new.get('failureReason'), 'requestedInputHashMatches': old.get('requestedInputHash') == new.get('requestedInputHash'), 'executionHashMatches': old.get('executionHash') == new.get('executionHash'), 'fingerprint': new.get('nativeEngineFingerprint'), 'snapshots': []}
for snap in new.get('snapshots', []):
    match = next((s for s in old.get('snapshots', []) if s['stepId'] == snap['stepId']), None)
    report['snapshots'].append({'stepId': snap['stepId'], 'oldPoints': match.get('pointCount') if match else None, 'newPoints': snap.get('pointCount'), 'oldCells': match.get('triangleCount') if match else None, 'newCells': snap.get('triangleCount'), 'oldMaterials': match.get('materialIds') if match else None, 'newMaterials': snap.get('materialIds')})
    if match:
        before = json.loads((source / match['snapshot']).read_text())
        after = json.loads((root / 'runs' / run_id / snap['snapshot']).read_text())
        report['snapshots'][-1]['arraysMatch'] = {key: before.get(key) == after.get(key) for key in ('points', 'triangles', 'cellData', 'materialIdsByLevelSetIndex')}
report['replayRequirements'] = json.loads((root / 'runs' / run_id / 'replay-project.json').read_text()).get('executionRequirements') if new['status'] == 'succeeded' else None
(root / 'legacy-replay-check.json').write_text(json.dumps(report, indent=2))
print(json.dumps(report, indent=2), flush=True)
if new['status'] != 'succeeded' or not report['executionHashMatches'] or report['fingerprint'] is not None:
    raise RuntimeError('legacy replay validation failed')
