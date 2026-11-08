"""Compare real isolated WebUI GPU runs to separately guarded ViennaPS calls."""
from __future__ import annotations
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tests/fixtures'))
from compare_multiparticle_product_worker import parse_vtk

EVIDENCE = ROOT / 'docs/stage9-evidence/gpu-coverage-audit/multiparticle-native-product'
DATA = Path(os.environ['VIENNAPS_WORKBENCH_DATA_DIR'])
RUNTIME = Path(os.environ['VIENNAPS_PATCHED_RUNTIME_DIR'])
URL = os.environ.get('WORKBENCH_URL', 'http://127.0.0.1:8809')
UUID = 'GPU-5532bb0f-735e-bc93-98ca-b7662a8ebf6d'
SOURCE = ROOT / 'docs/stage9-evidence/gpu-coverage-audit/multiparticle-species-probe/operator-split'
EVIDENCE.mkdir(parents=True, exist_ok=True)

def request(path, body=None):
    payload = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(URL + path, data=payload, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f'{path}: HTTP {exc.code}: {exc.read().decode()}') from exc

def finish(run):
    run_id = run['runId']
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        result = request('/api/runs/' + run_id)
        if result['status'] in ('succeeded', 'failed', 'cancelled', 'timed_out', 'interrupted'):
            if result['status'] != 'succeeded':
                raise RuntimeError(f'{run_id}: {result["status"]}: {result.get("failureReason")}')
            return run_id, result
        time.sleep(.5)
    raise TimeoutError(run_id)

def reference(name, fixture, args, engine='GPU_DISK', candidate=True):
    output = EVIDENCE / (name + '.json')
    guard = EVIDENCE / (name + '.guard.json')
    log = EVIDENCE / (name + '.log')
    command = [sys.executable, str(ROOT / 'scripts/bounded_child.py'), '--cwd', str(ROOT),
               '--record', str(guard), '--log-path', str(log), '--parent-pid', str(os.getpid()),
               '--timeout-seconds', '180', '--max-rss-mib', '2400', '--max-log-bytes', '3000000',
               '--', sys.executable, str(ROOT / 'tests/fixtures' / fixture), *map(str, args), str(output), engine]
    import_path = os.pathsep.join((str(RUNTIME), str(ROOT))) if candidate else str(ROOT)
    env = dict(os.environ, PYTHONPATH=import_path,
               CUDA_DEVICE_ORDER='PCI_BUS_ID', CUDA_VISIBLE_DEVICES=UUID,
               VIENNAPS_DIRECT_INTERMEDIATE_DIR=str(EVIDENCE / f'{name}.intermediate'))
    subprocess.run(command, cwd=ROOT, env=env, check=True, timeout=200, stdout=subprocess.DEVNULL)
    record = json.loads(guard.read_text())
    if record['returnCode'] != 0 or record['remainingOwnedGroupMembers']:
        raise RuntimeError(f'guard failed: {guard}')
    result = json.loads(output.read_text())
    imported = Path(result['viennapsModulePath']).resolve()
    if candidate and not imported.is_relative_to(RUNTIME.resolve()):
        raise RuntimeError(f'{name} imported the wrong ViennaPS package')
    if not candidate and imported.is_relative_to(RUNTIME.resolve()):
        raise RuntimeError(f'{name} unexpectedly imported the patched runtime')
    if result['versions'] != {'viennaps': '4.6.2', 'viennals': '5.8.5'}:
        raise RuntimeError(f'{name} used unexpected engine versions: {result["versions"]}')
    return result

def compare_geometry(direct, run_dir, step_id):
    snap = json.loads((run_dir / (step_id + '.json')).read_text())
    actual = parse_vtk(run_dir / (step_id + '.vtk'), snap['materialIdsByLevelSetIndex'], len(snap['materialIdsByLevelSetIndex']))
    ref = direct['steps'][step_id]['snapshot']
    for key in ('cells', 'materialIds', 'materialIdsByLevelSetIndex'):
        if ref[key] != actual[key]:
            raise AssertionError(f'geometry {step_id} {key} differs from direct native GPU')
    if len(ref['points']) != len(actual['points']):
        raise AssertionError('geometry point count differs from direct native GPU')
    delta = max((abs(a-b) for lhs, rhs in zip(ref['points'], actual['points']) for a,b in zip(lhs,rhs)), default=0)
    if delta > 1e-5:
        raise AssertionError(f'geometry coordinate delta {delta} exceeds VTK precision tolerance')
    return {'pointCount': len(ref['points']), 'cellCount': len(ref['cells']), 'maxCoordinateDelta': delta,
            'topologyAndMaterialsMatch': True}

def compare_flux(actual, direct):
    for key in ('points', 'topology', 'pointData', 'cellData'):
        if actual[key] != direct[key]:
            raise AssertionError(f'flux {key} differs from direct native GPU')
    return {'points': len(actual['points']), 'fieldNames': [row['name'] for row in actual['fields']],
            'meshAndDataMatch': True}

def observe_cpu_gpu(cpu_process, gpu_process, cpu_flux, gpu_flux, step, project):
    if cpu_process is None or cpu_flux is None:
        return {'status': 'cpu-reference-nonfinite-or-failed',
                'cpuProcessReference': cpu_process, 'cpuFluxReference': cpu_flux,
                'gpuProcessReference': 'passed', 'gpuFluxReference': 'passed',
                'scope': 'CPU and GPU matched-input difference not reported because direct CPU reference contains NaN or failed'}
    cpu_mesh = cpu_process['steps'][step['id']]['snapshot']
    gpu_mesh = gpu_process['steps'][step['id']]['snapshot']
    same_topology = cpu_mesh['cells'] == gpu_mesh['cells']
    same_points = len(cpu_mesh['points']) == len(gpu_mesh['points'])
    point_delta = max((math.dist(a, b) for a, b in zip(cpu_mesh['points'], gpu_mesh['points'])), default=0) if same_points and same_topology else None
    nearest_vertex_delta = None
    if not (same_points and same_topology) and 0 < len(cpu_mesh['points']) * len(gpu_mesh['points']) <= 5000000:
        def directed(lhs, rhs):
            return max(min(math.dist(a, b) for b in rhs) for a in lhs)
        nearest_vertex_delta = max(directed(cpu_mesh['points'], gpu_mesh['points']), directed(gpu_mesh['points'], cpu_mesh['points']))
    flux_mesh_same = cpu_flux['points'] == gpu_flux['points'] and cpu_flux['topology'] == gpu_flux['topology']
    fields = []
    for index, species in enumerate(step['species']):
        name = f"{species['label']}{index}"
        cpu = cpu_flux['cellData']['scalars'][name]
        gpu = gpu_flux['cellData']['scalars'][name]
        cpu_mean, gpu_mean = sum(cpu) / len(cpu), sum(gpu) / len(gpu)
        row = {'name': name, 'nativeUnit': None, 'unitStatus': 'not-provided-by-native-mesh-interface',
               'cpuMean': cpu_mean, 'gpuMean': gpu_mean,
               'meanDifference': gpu_mean - cpu_mean,
               'relativeMeanDifference': ((gpu_mean - cpu_mean) / abs(cpu_mean)) if cpu_mean else None}
        if flux_mesh_same and len(cpu) == len(gpu):
            row['pointwiseRmsDifference'] = math.sqrt(sum((a-b)**2 for a,b in zip(cpu,gpu))/len(cpu))
            row['bitwiseEqual'] = cpu == gpu
        fields.append(row)
    return {'scope': 'matched direct ViennaPS references; one fixed seed and ray count, not a general error bound',
            'dimension': project['dimension'], 'operation': step['operation'],
            'seed': project['solver']['random']['seed'], 'raysPerPoint': project['solver']['raysPerPoint'],
            'cpuThreads': project['solver']['cpuThreads'], 'gridDeltaUm': project['domain']['gridDelta'],
            'sameFluxMesh': flux_mesh_same, 'speciesFlux': fields,
            'geometry': {'sameTopology': same_topology, 'samePointCount': same_points,
                         'sameMaterialCellArray': cpu_mesh['materialIds'] == gpu_mesh['materialIds'],
                         'sameMaterialMap': cpu_mesh['materialIdsByLevelSetIndex'] == gpu_mesh['materialIdsByLevelSetIndex'],
                         'maxCorrespondingVertexDeltaUm': point_delta,
                         'symmetricNearestVertexDistanceUm': nearest_vertex_delta,
                         'nearestVertexMetricNote': 'vertex-only; not a continuous surface Hausdorff distance'}}

def main():
    report = {'status': 'running', 'deviceUuid': UUID, 'cases': []}
    for dim, rel in ((2, 'stack-trench-2d-multi-ion-etch.project.json'),
                     (3, 'stack-trench-3d-multi-ion-deposit.project.json')):
        project = json.loads((SOURCE / rel).read_text())
        project['name'] = f'Native GPU multi-ion product acceptance {dim}D'
        project['solver']['enginePreference'] = 'gpu'
        project['solver']['gpuDeviceId'] = UUID
        step = next(row for row in project['steps'] if row['enabled'])
        path = EVIDENCE / f'{dim}d-project.json'
        path.write_text(json.dumps(project, indent=2))
        process_id, process = finish(request('/api/runs', {'kind': 'simulate', 'project': project}))
        process_dir = DATA / 'runs' / process_id
        process_state = process_dir / 'state-initial.vpsd'
        assert process_state.is_file() and process_state.stat().st_size > 0
        worker = json.loads((process_dir / 'worker-result.json').read_text())
        manifest = json.loads((process_dir / 'manifest.json').read_text())
        guard = json.loads((process_dir / 'child-record.json').read_text())
        assert worker['selectedGpuDevice']['uuid'] == UUID
        assert worker['actualSteps'][0]['executionStrategy'] == 'native-multi-ion-v1'
        execution = next(row for row in process['actualExecution'] if row['stepId'] == step['id'])
        assert execution['actualEngine'] == 'GPU_DISK'
        device = execution['actualParameters']['cudaDeviceReadback']
        assert device['uuid'] == UUID and device['afterProcessApply']['uuid'] == UUID
        assert device['afterProcessApply']['readbackStatus'] == 'verified'
        assert execution['actualParameters']['multiParticleExecution']['nativeProcessCount'] == 1
        assert worker['nativeEngineFingerprint'] == manifest['nativeEngineFingerprint']
        assert guard['returnCode'] == 0 and not guard['remainingOwnedGroupMembers']

        # Use an independent guarded Python process calling ViennaPS directly,
        # but load the exact native Domain state the Workbench saved. Rebuilding
        # from project geometry alone can change hidden level-set samples.
        direct_process = reference(f'{dim}d-direct-process',
                                  'stage7_multiparticle_loaded_state_reference.py',
                                  [path, process_state])
        geometry = compare_geometry(direct_process, process_dir, step['id'])
        replay = request('/api/runs/' + process_id + '/replay-project')
        assert replay['executionRequirements']['nativeEngineFingerprint'] == manifest['nativeEngineFingerprint']
        replay_id, _ = finish(request('/api/runs/replay', replay))
        replay_dir = DATA / 'runs' / replay_id
        assert json.loads((replay_dir / 'manifest.json').read_text())['executionHash'] == manifest['executionHash']
        compare_geometry(direct_process, replay_dir, step['id'])
        flux_id, _ = finish(request('/api/runs/flux', {'project': project, 'stepId': step['id']}))
        flux_dir = DATA / 'runs' / flux_id
        flux_state = flux_dir / 'state-initial.vpsd'
        assert flux_state.is_file() and flux_state.stat().st_size > 0
        direct_flux = reference(f'{dim}d-direct-flux',
                                'stage7_multiparticle_loaded_state_flux_reference.py',
                                [path, step['id'], flux_state])
        cpu_process = cpu_flux = None
        cpu_errors = {}
        for label, fixture, args in (
            ('process', 'stage7_multiparticle_loaded_state_reference.py', [path, process_state]),
            ('flux', 'stage7_multiparticle_loaded_state_flux_reference.py', [path, step['id'], flux_state]),
        ):
            try:
                result = reference(f'{dim}d-direct-cpu-{label}', fixture, args,
                                   'CPU_DISK', candidate=False)
                if label == 'process': cpu_process = result
                else: cpu_flux = result
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError) as exc:
                log = EVIDENCE / f'{dim}d-direct-cpu-{label}.log'
                cpu_errors[label] = {'error': str(exc), 'log': str(log), 'tail': '\n'.join(log.read_text(errors='replace').splitlines()[-8:]) if log.is_file() else None}
        cpu_gpu = observe_cpu_gpu(cpu_process, direct_process, cpu_flux, direct_flux, step, project)
        if cpu_errors:
            cpu_gpu['cpuErrors'] = cpu_errors
        cpu_gpu['cpuViennaPSBuild'] = (cpu_process or cpu_flux or {}).get('viennapsModulePath', 'failed')
        cpu_gpu['gpuViennaPSBuild'] = direct_process['viennapsModulePath']
        flux = request('/api/runs/' + flux_id + '/flux-result')
        assert flux['execution']['actualEngine'] == 'GPU_DISK'
        assert flux['execution']['gpuDeviceId'] == UUID
        flux_compare = compare_flux(flux, direct_flux)
        flux_replay = request('/api/runs/' + flux_id + '/replay-project')
        assert flux_replay['executionRequirements']['steps'][0]['fluxStrategy'] == 'native-multi-ion-flux-v1'
        flux_replay_id, _ = finish(request('/api/runs/replay', flux_replay))
        compare_flux(request('/api/runs/' + flux_replay_id + '/flux-result'), direct_flux)
        with urllib.request.urlopen(URL + '/api/runs/' + flux_id + '/download', timeout=20) as response:
            zip_path = EVIDENCE / f'{dim}d-flux.zip'
            zip_path.write_bytes(response.read())
        with zipfile.ZipFile(zip_path) as archive:
            assert any(name.endswith('flux.json') for name in archive.namelist())
        report['cases'].append({'dimension': dim, 'processRunId': process_id, 'processReplayId': replay_id,
                                'fluxRunId': flux_id, 'fluxReplayId': flux_replay_id,
                                'engineFingerprint': manifest['nativeEngineFingerprint'],
                                'geometry': geometry, 'flux': flux_compare, 'cpuGpuObservation': cpu_gpu})
        (EVIDENCE / 'acceptance.json').write_text(json.dumps(report, indent=2))
        print(json.dumps(report['cases'][-1]), flush=True)
    neutral_source = ROOT / 'docs/stage9-evidence/gpu-coverage-audit/multiparticle-native-fix-2026-10-28/stock-single-neutral-project.json'
    project = json.loads(neutral_source.read_text())
    project['solver']['enginePreference'] = 'gpu'
    project['solver']['gpuDeviceId'] = UUID
    step = next(row for row in project['steps'] if row['enabled'])
    path = EVIDENCE / 'neutral-empty-map-project.json'
    path.write_text(json.dumps(project, indent=2))
    run_id, _ = finish(request('/api/runs', {'kind': 'simulate', 'project': project}))
    run_dir = DATA / 'runs' / run_id
    manifest = json.loads((run_dir / 'manifest.json').read_text())
    assert manifest.get('nativeEngineFingerprint')
    state_path = run_dir / 'state-initial.vpsd'
    direct = reference('neutral-direct-process', 'stage7_multiparticle_loaded_state_reference.py', [path, state_path])
    neutral_geometry = compare_geometry(direct, run_dir, step['id'])
    replay = request('/api/runs/' + run_id + '/replay-project')
    assert replay['executionRequirements']['nativeEngineFingerprint'] == manifest['nativeEngineFingerprint']
    replay_id, _ = finish(request('/api/runs/replay', replay))
    assert json.loads((DATA / 'runs' / replay_id / 'manifest.json').read_text())['executionHash'] == manifest['executionHash']
    compare_geometry(direct, DATA / 'runs' / replay_id, step['id'])
    report['neutralEmptyMap'] = {'runId': run_id, 'replayId': replay_id, 'geometry': neutral_geometry}
    report['status'] = 'passed'
    (EVIDENCE / 'acceptance.json').write_text(json.dumps(report, indent=2))
    print(json.dumps({'status': report['status'], 'cases': len(report['cases'])}), flush=True)

if __name__ == '__main__':
    main()
