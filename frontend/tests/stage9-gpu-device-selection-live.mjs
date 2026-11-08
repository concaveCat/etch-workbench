/** Real Chromium/API/ViennaPS GPU selection, persistence, replay and renderer readback. */
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';
import { assertGpuReadback } from './helpers/gpu-readback.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const baseUrl = (process.env.WORKBENCH_URL ?? 'http://127.0.0.1:8803').replace(/\/$/, '');
const dataDir = path.resolve(process.env.VIENNAPS_WORKBENCH_DATA_DIR ?? path.join(root, 'var/browser-gpu-device-selection'));
const evidenceDir = path.join(root, 'docs/stage9-evidence/gpu-coverage-audit/gpu-device-selection/browser');
const sourceProjectPath = path.join(root, 'docs/stage9-evidence/gpu-coverage-audit/gpu-device-selection/rtx3060-3d-product-direct.project.json');
const directPath = path.join(root, 'docs/stage9-evidence/gpu-coverage-audit/gpu-device-selection/rtx3060-3d-product-direct.direct.json');
const inputPath = path.join(evidenceDir, 'gpu-device-auto-input.project.json');
const reportPath = path.join(evidenceDir, 'acceptance.json');
await fs.mkdir(evidenceDir, { recursive: true });

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

function compareSnapshot(actual, expected, label) {
  assert.deepEqual(actual.points, expected.points, `${label}: coordinates`);
  assert.deepEqual(actual.triangles, expected.cells, `${label}: topology`);
  assert.deepEqual(actual.cellData.materialIds, expected.materialIds, `${label}: cell material ids`);
  assert.deepEqual(actual.materialIdsByLevelSetIndex, expected.materialIdsByLevelSetIndex,
    `${label}: level-set material mapping`);
}

const project = JSON.parse(await fs.readFile(sourceProjectPath, 'utf8'));
delete project.solver.gpuDeviceId;
project.solver.enginePreference = 'auto';
project.name = `GPU 设备选择 Chromium ${Date.now()}`;
await fs.writeFile(inputPath, `${JSON.stringify(project, null, 2)}\n`);
const direct = JSON.parse(await fs.readFile(directPath, 'utf8'));

const inventory = await api('/api/gpu-devices');
assert.equal(inventory.response.status, 200);
assert.equal(inventory.body.status, 'available', JSON.stringify(inventory.body));
assert.equal(inventory.body.probe?.bounded, true);
const device = inventory.body.devices.find(row => row.name === 'NVIDIA GeForce RTX 3060 Laptop GPU');
assert.ok(device, `RTX 3060 Laptop GPU missing from CUDA device list: ${JSON.stringify(inventory.body.devices)}`);

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const context = await browser.newContext({ viewport: { width: 1680, height: 1100 } });
await context.addInitScript(() => {
  const prototype = HTMLCanvasElement.prototype;
  const originalGetContext = prototype.getContext;
  const requests = [];
  Object.defineProperty(window, '__workbenchWebGLContextRequests', { value: requests });
  prototype.getContext = function(type, options) {
    if (type === 'webgl2' || type === 'webgl') {
      requests.push({ canvas: this, type, options: options ? { ...options } : null });
    }
    return Reflect.apply(originalGetContext, this, [type, options]);
  };
});
const page = await context.newPage();
page.setDefaultTimeout(30000);
const pageErrors = [];
page.on('pageerror', error => pageErrors.push({ message: error.message, stack: error.stack }));

try {
  await page.goto(`${baseUrl}/`);
  await page.getByText('本机服务已连接').waitFor();
  const importResponse = page.waitForResponse(response => response.url().endsWith('/api/projects/validate')
    && response.request().method() === 'POST');
  await page.locator('nav[aria-label="项目操作"] input[type="file"]').setInputFiles(inputPath);
  const imported = await importResponse;
  assert.equal(imported.status(), 200, await imported.text());
  await page.waitForFunction(name => document.querySelector('input[aria-label="项目名称"]')?.value === name, project.name);

  const selector = page.getByLabel('计算 GPU', { exact: true });
  assert.ok((await selector.locator('option').evaluateAll(options => options.map(option => option.value))).includes(device.uuid));
  assert.equal(await selector.inputValue(), 'auto', 'a new/automatically imported project should keep host-preferred GPU selection');
  assert.equal(inventory.body.preferredGpuDeviceId, device.uuid);

  const defaultRequestPromise = page.waitForRequest(request => request.url().endsWith('/api/runs') && request.method() === 'POST');
  const defaultResponsePromise = page.waitForResponse(response => response.url().endsWith('/api/runs') && response.request().method() === 'POST');
  await page.getByRole('button', { name: '运行工艺', exact: true }).click();
  const [defaultRequest, defaultResponse] = await Promise.all([defaultRequestPromise, defaultResponsePromise]);
  assert.equal(defaultResponse.status(), 202, await defaultResponse.text());
  const defaultProject = defaultRequest.postDataJSON().project;
  assert.equal(defaultProject.solver.enginePreference, 'auto');
  assert.equal(Object.hasOwn(defaultProject.solver, 'gpuDeviceId'), false);
  const defaultRunId = (await defaultResponse.json()).runId;
  const defaultRun = await waitForRun(defaultRunId);
  const defaultActual = defaultRun.actualExecution.find(row => row.stepId === defaultProject.steps[0].id);
  assert.equal(defaultActual.actualEngine, 'GPU_DISK');
  assertGpuReadback(defaultActual.actualParameters.cudaDeviceReadback, device.uuid, 'automatic GPU run');
  const defaultInitial = await api(`/api/runs/${defaultRunId}/snapshots/initial`);
  const defaultFinal = await api(`/api/runs/${defaultRunId}/snapshots/${encodeURIComponent(defaultProject.steps[0].id)}`);
  compareSnapshot(defaultInitial.body, direct.initial, 'host-preferred GPU initial');
  compareSnapshot(defaultFinal.body, direct.final, 'host-preferred GPU final');

  const defaultReplay = await api(`/api/runs/${defaultRunId}/replay-project`);
  assert.equal(defaultReplay.response.status, 200);
  assert.equal(defaultReplay.body.normalizedProject.solver.gpuDeviceId, device.uuid,
    'replay must pin the physical device selected by automatic mode');
  const defaultReplayResponse = await api('/api/runs/replay', {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(defaultReplay.body),
  });
  assert.equal(defaultReplayResponse.response.status, 202, JSON.stringify(defaultReplayResponse.body));
  const defaultReplayRun = await waitForRun(defaultReplayResponse.body.runId);
  const defaultReplayActual = defaultReplayRun.actualExecution.find(row => row.stepId === defaultProject.steps[0].id);
  assert.equal(defaultReplayActual.actualEngine, 'GPU_DISK');
  assertGpuReadback(defaultReplayActual.actualParameters.cudaDeviceReadback, device.uuid, 'automatic GPU replay');

  await selector.selectOption(device.uuid);
  assert.equal(await selector.inputValue(), device.uuid);
  assert.equal(await page.getByRole('button', { name: '运行工艺', exact: true }).isDisabled(), false);

  const saveResponse = page.waitForResponse(response => response.url().endsWith('/api/projects')
    && response.request().method() === 'POST');
  await page.getByRole('button', { name: '保存项目', exact: true }).click();
  const saved = await saveResponse;
  assert.equal(saved.status(), 201, await saved.text());
  const savedBody = await saved.json();
  const projectId = savedBody.id;
  assert.equal(savedBody.project.solver.gpuDeviceId, device.uuid);

  const exportDownload = page.waitForEvent('download');
  const exportValidation = page.waitForResponse(response => response.url().endsWith('/api/projects/validate')
    && response.request().method() === 'POST');
  await page.getByRole('button', { name: '导出项目', exact: true }).click();
  const [download, validation] = await Promise.all([exportDownload, exportValidation]);
  assert.equal(validation.status(), 200, await validation.text());
  const exportedProjectPath = path.join(evidenceDir, 'gpu-device-exported-project.json');
  await download.saveAs(exportedProjectPath);
  const exportedProject = JSON.parse(await fs.readFile(exportedProjectPath, 'utf8'));
  assert.equal(exportedProject.solver.gpuDeviceId, device.uuid,
    'project export must preserve the selected CUDA device UUID');

  await selector.selectOption('auto');
  const reimportValidation = page.waitForResponse(response => response.url().endsWith('/api/projects/validate')
    && response.request().method() === 'POST');
  await page.locator('nav[aria-label="项目操作"] input[type="file"]').setInputFiles(exportedProjectPath);
  const reimported = await reimportValidation;
  assert.equal(reimported.status(), 200, await reimported.text());
  await page.waitForFunction(uuid => document.querySelector('select[aria-label="计算 GPU"]')?.value === uuid, device.uuid);

  await page.getByLabel('已保存项目', { exact: true }).selectOption(projectId);
  const openResponse = page.waitForResponse(response => response.url().endsWith(`/api/projects/${projectId}`)
    && response.request().method() === 'GET');
  await page.getByRole('button', { name: '打开', exact: true }).click();
  const reopened = await openResponse;
  assert.equal(reopened.status(), 200);
  await page.waitForFunction(uuid => document.querySelector('select[aria-label="计算 GPU"]')?.value === uuid, device.uuid);

  const requestPromise = page.waitForRequest(request => request.url().endsWith('/api/runs') && request.method() === 'POST');
  const responsePromise = page.waitForResponse(response => response.url().endsWith('/api/runs') && response.request().method() === 'POST');
  await page.getByRole('button', { name: '运行工艺', exact: true }).click();
  const [runRequest, runResponse] = await Promise.all([requestPromise, responsePromise]);
  assert.equal(runResponse.status(), 202, await runResponse.text());
  const submitted = runRequest.postDataJSON().project;
  assert.equal(submitted.solver.enginePreference, 'auto');
  assert.equal(submitted.solver.gpuDeviceId, device.uuid);
  const runId = (await runResponse.json()).runId;
  const run = await waitForRun(runId);
  const actual = run.actualExecution.find(row => row.stepId === submitted.steps[0].id);
  assert.equal(actual.actualEngine, 'GPU_DISK');
  assertGpuReadback(actual.actualParameters.cudaDeviceReadback, device.uuid, 'explicit GPU run');
  assert.equal(actual.actualParameters.cudaDeviceReadback.pciBusId.replace(/^0000/, '0000'), '0000:01:00.0');

  const initial = await api(`/api/runs/${runId}/snapshots/initial`);
  const final = await api(`/api/runs/${runId}/snapshots/${encodeURIComponent(submitted.steps[0].id)}`);
  assert.equal(initial.response.status, 200);
  assert.equal(final.response.status, 200);
  compareSnapshot(initial.body, direct.initial, 'browser GPU initial');
  compareSnapshot(final.body, direct.final, 'browser GPU final');

  const replay = await api(`/api/runs/${runId}/replay-project`);
  assert.equal(replay.response.status, 200);
  assert.equal(replay.body.normalizedProject.solver.gpuDeviceId, device.uuid);
  assert.equal(replay.body.normalizedProject.solver.enginePreference, 'gpu');
  const replayRunRequest = await api('/api/runs/replay', {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(replay.body),
  });
  assert.equal(replayRunRequest.response.status, 202, JSON.stringify(replayRunRequest.body));
  const replayRun = await waitForRun(replayRunRequest.body.runId);
  const replayActual = replayRun.actualExecution.find(row => row.stepId === submitted.steps[0].id);
  assert.equal(replayActual.actualEngine, 'GPU_DISK');
  assertGpuReadback(replayActual.actualParameters.cudaDeviceReadback, device.uuid, 'explicit GPU replay');

  await page.locator('.vtk-canvas canvas').waitFor();
  const renderer = await page.locator('.vtk-canvas canvas').evaluate(canvas => {
    const gl = canvas.getContext('webgl2') ?? canvas.getContext('webgl');
    if (!gl) return { available: false };
    const extension = gl.getExtension('WEBGL_debug_renderer_info');
    const contextRequest = window.__workbenchWebGLContextRequests
      .find(request => request.canvas === canvas && request.type === 'webgl2' && request.options?.powerPreference);
    return { available: true,
      vendor: extension ? gl.getParameter(extension.UNMASKED_VENDOR_WEBGL) : null,
      renderer: extension ? gl.getParameter(extension.UNMASKED_RENDERER_WEBGL) : null,
      contextAttributes: gl.getContextAttributes(),
      requestedAttributes: contextRequest?.options ?? null };
  });
  assert.equal(renderer.available, true);
  assert.equal(renderer.requestedAttributes?.powerPreference, 'high-performance',
    'VTK.js must request a high-performance WebGL context for the 3D viewer');
  assert.deepEqual(pageErrors, []);

  const runDir = path.join(dataDir, 'runs', runId);
  const worker = JSON.parse(await fs.readFile(path.join(runDir, 'worker-result.json'), 'utf8'));
  const guard = JSON.parse(await fs.readFile(path.join(runDir, 'child-record.json'), 'utf8'));
  assert.equal(worker.cudaDeviceReadback.uuid, device.uuid);
  assert.equal(worker.selectedGpuDevice.uuid, device.uuid);
  assert.equal(guard.returnCode, 0);
  assert.deepEqual(guard.remainingOwnedGroupMembers, []);
  const report = {
    status: 'passed', browser: 'Chromium', service: baseUrl, isolatedDataDir: dataDir,
    installedVersions: worker.versions, device, deviceInventoryProbe: inventory.body.probe,
    projectSaveReopen: { passed: true, projectId, gpuDeviceId: device.uuid },
    projectExportImport: { passed: true, file: path.basename(exportedProjectPath), gpuDeviceId: device.uuid },
    hostPreferredDefault: { passed: true, requestedDevice: 'auto', requestedEngine: 'auto',
      actualEngine: defaultActual.actualEngine,
      actualGpuDeviceId: defaultActual.actualParameters.cudaDeviceReadback.uuid,
      runId: defaultRunId, replayRunId: defaultReplayRun.runId,
      replayPinnedGpuDeviceId: defaultReplayActual.actualParameters.cudaDeviceReadback.uuid,
      initialAndFinalExactlyMatchIndependentViennaPS: true },
    computation: { engine: actual.actualEngine, runtimeReadback: actual.actualParameters.cudaDeviceReadback,
      processApplySeconds: actual.actualParameters.processApplySeconds,
      initialAndFinalExactlyMatchIndependentViennaPS: true, runId },
    replay: { passed: true, runId: replayRun.runId,
      gpuDeviceId: replayActual.actualParameters.cudaDeviceReadback.uuid,
      engine: replayActual.actualEngine },
    rendering: { source: 'VTK.js WebGL2', requestedPowerPreference: renderer.requestedAttributes.powerPreference, actualReadback: renderer,
      adapterChoice: 'browser-and-driver-controlled; independent of ViennaPS CUDA compute UUID' },
    childGuard: { returnCode: guard.returnCode, remainingOwnedGroupMembers: guard.remainingOwnedGroupMembers },
    pageErrors,
  };
  await fs.writeFile(path.join(evidenceDir, 'submitted-project.json'), `${JSON.stringify(submitted, null, 2)}\n`);
  await fs.writeFile(path.join(evidenceDir, 'product-worker.json'), `${JSON.stringify(worker, null, 2)}\n`);
  await fs.writeFile(path.join(evidenceDir, 'product.guard.json'), `${JSON.stringify(guard, null, 2)}\n`);
  await fs.writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`);
  console.log(JSON.stringify({ status: report.status, runId, actualEngine: actual.actualEngine,
    gpuDeviceId: device.uuid, renderer: renderer.renderer, reportPath: path.relative(root, reportPath) }, null, 2));
} catch (error) {
  await fs.writeFile(path.join(evidenceDir, 'acceptance.failure.json'), `${JSON.stringify({
    status: 'failed', failure: { message: error.message, stack: error.stack }, pageErrors,
  }, null, 2)}\n`);
  throw error;
} finally {
  await context.close();
  await browser.close();
}
