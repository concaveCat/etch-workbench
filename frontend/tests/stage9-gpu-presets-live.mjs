/** Real Chromium GPU runs for the four geometry preset families users reported as unavailable. */
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';
import { assertGpuReadback } from './helpers/gpu-readback.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const baseUrl = (process.env.WORKBENCH_URL ?? 'http://127.0.0.1:8803').replace(/\/$/, '');
const dataDir = path.resolve(process.env.VIENNAPS_WORKBENCH_DATA_DIR
  ?? path.join(root, 'var/browser-gpu-export-import-acceptance-2026-10-28'));
const evidenceDir = path.join(root, 'docs/stage9-evidence/gpu-coverage-audit/gpu-device-selection/presets-browser');
const matrix = JSON.parse(await fs.readFile(path.join(root, 'docs/stage9-evidence/gpu-capability-matrix.json'), 'utf8'));
const materialScope = JSON.parse(await fs.readFile(path.join(root,
  'docs/stage9-evidence/gpu-coverage-audit/gpu-device-selection/material-scope/specialized-substrate-capability-report.json'), 'utf8'));
await fs.mkdir(evidenceDir, { recursive: true });

function findRow(model, dimension, geometry, predicate = () => true) {
  const row = matrix.rows.find(candidate => candidate.status === 'verified'
    && candidate.engine === 'GPU_DISK' && candidate.model === model
    && candidate.dimension === dimension && candidate.operation === 'etch'
    && candidate.boundaryXY === 'reflective' && candidate.geometryKinds.includes(geometry)
    && predicate(candidate));
  assert.ok(row, `verified GPU matrix row missing: ${model}/${dimension}D/${geometry}`);
  return row;
}

function findSubstrateCase(model, material) {
  const modelRow = materialScope.rows.find(row => row.model === model);
  const evidence = modelRow?.verifiedCases.find(row => row.material === material);
  assert.ok(evidence, `GPU substrate evidence missing: ${model}/${material}`);
  return { evidence: { productWorker: evidence.productWorker, directReference: evidence.directReference } };
}

const cases = [
  { id: 'plane-2d-specialized', label: '平面 2D · SF6/O2',
    row: findRow('specialized:sf6o2', 2, 'plane') },
  { id: 'trench-2d-single-particle', label: '沟槽 2D · SingleParticle',
    row: findRow('singleParticle', 2, 'trench') },
  { id: 'hole-3d-single-particle', label: '圆孔 3D · SingleParticle',
    row: findRow('singleParticle', 3, 'hole') },
  { id: 'fin-3d-boolean-single-particle', label: 'Fin 3D Boolean · SingleParticle',
    row: findRow('singleParticle', 3, 'fin', row => row.finConstruction === 'boolean'
      && row.finExtrusionMode === 'direct' && !row.evidence.productWorker.includes('mask-')) },
  { id: 'plane-2d-sf6o2-sio2-substrate', label: '平面 2D · SF6/O2 · SiO₂ 衬底',
    row: findSubstrateCase('sf6o2', 'SiO2') },
  { id: 'plane-2d-neutral-transport-hfo2-substrate', label: '平面 2D · 中性粒子输运 · HfO₂ 衬底',
    row: findSubstrateCase('neutralTransport', 'HfO2') },
  { id: 'plane-2d-ald-pmma-substrate', label: '平面 2D · ALD · PMMA 衬底',
    row: findSubstrateCase('ald', 'PMMA') },
];

async function api(route, options = {}) {
  const response = await fetch(`${baseUrl}${route}`, options);
  const body = await response.json();
  return { response, body };
}

async function waitForRun(runId) {
  const deadline = Date.now() + 180000;
  while (Date.now() < deadline) {
    const { response, body } = await api(`/api/runs/${runId}`);
    assert.equal(response.status, 200);
    if (['succeeded', 'failed', 'cancelled', 'timed_out', 'interrupted'].includes(body.status)) {
      assert.equal(body.status, 'succeeded', body.failureReason ?? `run ended ${body.status}`);
      return body;
    }
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  throw new Error(`run ${runId} did not finish within 180 seconds`);
}

function compare(actual, expected, dimension, label) {
  assert.deepEqual(actual.points, expected.points, `${label}: coordinates`);
  assert.deepEqual(dimension === 2 ? actual.lines : actual.triangles, expected.cells, `${label}: topology`);
  assert.deepEqual(actual.cellData.materialIds, expected.materialIds, `${label}: material ids`);
  assert.deepEqual(actual.materialIdsByLevelSetIndex, expected.materialIdsByLevelSetIndex,
    `${label}: level-set material map`);
}

const inventory = await api('/api/gpu-devices');
assert.equal(inventory.response.status, 200);
assert.equal(inventory.body.status, 'available', JSON.stringify(inventory.body));
assert.equal(inventory.body.probe?.bounded, true);
const device = inventory.body.devices.find(row => row.name === 'NVIDIA GeForce RTX 3060 Laptop GPU');
assert.ok(device, 'RTX 3060 Laptop GPU missing from CUDA device list');

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const context = await browser.newContext({ viewport: { width: 1680, height: 1100 } });
const page = await context.newPage();
page.setDefaultTimeout(30000);
const pageErrors = [];
page.on('pageerror', error => pageErrors.push({ message: error.message, stack: error.stack }));
const results = [];

try {
  await page.goto(`${baseUrl}/`);
  await page.getByText('本机服务已连接').waitFor();
  for (const testCase of cases) {
    const project = JSON.parse(await fs.readFile(path.join(root, testCase.row.evidence.productWorker)
      .replace(/worker-result\.json$/, 'project.json'), 'utf8'));
    const direct = JSON.parse(await fs.readFile(path.join(root, testCase.row.evidence.directReference), 'utf8'));
    project.name = `GPU preset browser ${testCase.id} ${Date.now()}`;
    project.solver.enginePreference = 'gpu';
    project.solver.gpuDeviceId = device.uuid;
    const inputPath = path.join(evidenceDir, `${testCase.id}.project.json`);
    await fs.writeFile(inputPath, `${JSON.stringify(project, null, 2)}\n`);

    const importedValidation = page.waitForResponse(response => response.url().endsWith('/api/projects/validate')
      && response.request().method() === 'POST');
    await page.locator('nav[aria-label="项目操作"] input[type="file"]').setInputFiles(inputPath);
    const imported = await importedValidation;
    assert.equal(imported.status(), 200, `${testCase.id}: ${await imported.text()}`);
    await page.waitForFunction(name => document.querySelector('input[aria-label="项目名称"]')?.value === name,
      project.name);

    const engineSelect = page.getByLabel('引擎偏好', { exact: true });
    const gpuOption = engineSelect.locator('option[value="gpu"]');
    assert.equal(await gpuOption.isDisabled(), false,
      `${testCase.id}: browser did not expose a matrix-verified GPU option`);
    assert.equal(await engineSelect.inputValue(), 'gpu');
    const deviceSelect = page.getByLabel('计算 GPU', { exact: true });
    assert.equal(await deviceSelect.inputValue(), device.uuid);

    const requestPromise = page.waitForRequest(request => request.url().endsWith('/api/runs')
      && request.method() === 'POST');
    const responsePromise = page.waitForResponse(response => response.url().endsWith('/api/runs')
      && response.request().method() === 'POST');
    await page.getByRole('button', { name: '运行工艺', exact: true }).click();
    const [request, response] = await Promise.all([requestPromise, responsePromise]);
    assert.equal(response.status(), 202, `${testCase.id}: ${await response.text()}`);
    const submitted = request.postDataJSON().project;
    assert.equal(submitted.solver.gpuDeviceId, device.uuid);
    const runId = (await response.json()).runId;
    const run = await waitForRun(runId);
    const execution = run.actualExecution.find(row => row.stepId === submitted.steps[0].id);
    assert.equal(execution.actualEngine, 'GPU_DISK');
    assertGpuReadback(execution.actualParameters.cudaDeviceReadback, device.uuid, `${testCase.id} GPU run`);

    const initial = await api(`/api/runs/${runId}/snapshots/initial`);
    const final = await api(`/api/runs/${runId}/snapshots/${encodeURIComponent(submitted.steps[0].id)}`);
    assert.equal(initial.response.status, 200);
    assert.equal(final.response.status, 200);
    compare(initial.body, direct.initial, project.dimension, `${testCase.id} initial`);
    compare(final.body, direct.final, project.dimension, `${testCase.id} final`);

    const runDirectory = path.join(dataDir, 'runs', runId);
    const worker = JSON.parse(await fs.readFile(path.join(runDirectory, 'worker-result.json'), 'utf8'));
    const guard = JSON.parse(await fs.readFile(path.join(runDirectory, 'child-record.json'), 'utf8'));
    assert.equal(worker.cudaDeviceReadback.uuid, device.uuid);
    assert.equal(guard.returnCode, 0);
    assert.deepEqual(guard.remainingOwnedGroupMembers, []);
    await fs.writeFile(path.join(evidenceDir, `${testCase.id}.worker-result.json`),
      `${JSON.stringify(worker, null, 2)}\n`);
    await fs.writeFile(path.join(evidenceDir, `${testCase.id}.guard.json`), `${JSON.stringify(guard, null, 2)}\n`);
    results.push({ id: testCase.id, label: testCase.label, runId, engine: execution.actualEngine,
      gpuDeviceId: execution.actualParameters.cudaDeviceReadback.uuid,
      installedVersions: worker.versions,
      processVisibility: execution.actualParameters.cudaDeviceReadback.process.processListStatus,
      exactInitialAndFinalMatchToIndependentViennaPS: true,
      matrixRow: testCase.row.evidence.productWorker });
  }
  assert.deepEqual(pageErrors, []);
  const report = { status: 'passed', service: baseUrl, isolatedDataDir: dataDir,
    installedVersions: results[0]?.installedVersions,
    selectedDevice: device, cases: results, pageErrors };
  const reportPath = path.join(evidenceDir, 'acceptance.json');
  await fs.writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`);
  console.log(JSON.stringify({ status: report.status, caseCount: results.length,
    geometries: results.map(row => row.id), gpuDeviceId: device.uuid,
    reportPath: path.relative(root, reportPath) }, null, 2));
} catch (error) {
  await fs.writeFile(path.join(evidenceDir, 'acceptance.failure.json'), `${JSON.stringify({
    status: 'failed', failure: { message: error.message, stack: error.stack }, pageErrors, results,
  }, null, 2)}\n`);
  throw error;
} finally {
  await context.close();
  await browser.close();
}
