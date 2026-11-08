/** Real UI/API acceptance for a solver-profiled multi-ion GPU split geometry. */
import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';
import { chromium } from 'playwright';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const baseUrl = (process.env.WORKBENCH_URL ?? 'http://127.0.0.1:8809').replace(/\/$/, '');
const dataDir = path.resolve(process.env.VIENNAPS_WORKBENCH_DATA_DIR
  ?? path.join(root, 'var/stage9-multiparticle-split-geometry-browser'));
const defaultEvidenceDir = path.join(root, 'docs/stage9-evidence/gpu-coverage-audit/multiparticle-species-probe/operator-split/geometry-scope/2d-plane-two-ion');
const evidenceDir = path.resolve(process.env.MULTIPARTICLE_CASE_DIR ?? defaultEvidenceDir);
const projectPath = path.join(evidenceDir, 'project.json');
const cpuReference = path.join(evidenceDir, 'cpu-reference.json');
const cpuGuard = path.join(evidenceDir, 'cpu-reference.guard.json');
const execFileAsync = promisify(execFile);
const RTX_UUID = 'GPU-5532bb0f-735e-bc93-98ca-b7662a8ebf6d';

async function api(route, options = {}) {
  const response = await fetch(`${baseUrl}${route}`, options);
  const body = await response.json();
  return { response, body };
}

async function waitForRun(runId) {
  const terminal = new Set(['succeeded', 'failed', 'cancelled', 'timed_out', 'interrupted']);
  const deadline = Date.now() + 180000;
  while (Date.now() < deadline) {
    const { response, body } = await api(`/api/runs/${runId}`);
    assert.equal(response.status, 200);
    if (terminal.has(body.status)) {
      assert.equal(body.status, 'succeeded', body.failureReason ?? body.status);
      return body;
    }
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  throw new Error(`run ${runId} did not finish within 180 seconds`);
}

async function compareRun(runId, suffix) {
  const runDir = path.join(dataDir, 'runs', runId);
  const outputPath = path.join(evidenceDir, `ui-${suffix}-comparison.json`);
  await execFileAsync(path.join(root, '.venv/bin/python'), [
    path.join(root, 'tests/fixtures/compare_multiparticle_product_worker.py'),
    projectPath, cpuReference, runDir, cpuGuard,
    path.join(runDir, 'child-record.json'), outputPath,
  ], { cwd: root, timeout: 60000, maxBuffer: 4 * 1024 * 1024 });
  const report = JSON.parse(await fs.readFile(outputPath, 'utf8'));
  assert.equal(report.status, 'passed', JSON.stringify(report.checks));
  assert.ok(Object.values(report.checks).every(value => value === true), JSON.stringify(report.checks));
  return { report, outputPath, runDir };
}

async function saveAndReopen(page, projectName) {
  const projectMenu = page.locator('.topbar-shell details.ui-menu').first();
  if (!(await projectMenu.evaluate(element => element.open))) await projectMenu.locator('summary').click();
  await page.getByRole('button', { name: '保存项目', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('.topbar-project .save-state')?.textContent?.trim() === '已保存');
  const projects = await api('/api/projects');
  const saved = projects.body.projects.find(row => row.name === projectName);
  assert.ok(saved, `UI save did not persist ${projectName}`);
  const document = await api(`/api/projects/${saved.id}`);
  assert.equal(document.body.project.solver.gpuDeviceId, RTX_UUID);
  await page.waitForFunction(name => Array.from(document.querySelectorAll('select[aria-label="已保存项目"] option'))
    .some(option => option.textContent?.includes(name)), projectName);
  await page.getByLabel('已保存项目').selectOption(saved.id);
  await page.getByRole('button', { name: '打开', exact: true }).click();
  await page.locator('.activity-rail').getByRole('button', { name: '设置', exact: true }).click();
  await page.waitForFunction(uuid => document.querySelector('select[aria-label="计算 GPU"]')?.value === uuid, RTX_UUID);
}

const project = JSON.parse(await fs.readFile(projectPath, 'utf8'));
const kind = project.geometry.kind;
project.name = `GPU ${kind} 双离子验收 ${Date.now()}`;
project.solver.enginePreference = 'gpu';
project.solver.gpuDeviceId = RTX_UUID;
const inputPath = path.join(evidenceDir, 'ui-submitted-project.json');
await fs.writeFile(inputPath, `${JSON.stringify(project, null, 2)}\n`);

const pageErrors = [];
const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const context = await browser.newContext({ viewport: { width: 1680, height: 1100 } });
const page = await context.newPage();
page.setDefaultTimeout(30000);
page.on('pageerror', error => pageErrors.push({ message: error.message, stack: error.stack }));

try {
  const devices = await api('/api/gpu-devices');
  assert.equal(devices.response.status, 200);
  assert.ok(devices.body.devices.some(row => row.uuid === RTX_UUID));
  await page.goto(`${baseUrl}/`);
  await page.getByText('本机服务已连接').waitFor();
  const validate = page.waitForResponse(response => response.url().endsWith('/api/projects/validate')
    && response.request().method() === 'POST');
  await page.locator('nav[aria-label="项目操作"] input[type="file"]').setInputFiles(inputPath);
  assert.equal((await validate).status(), 200);
  await page.waitForFunction(name => document.querySelector('input[aria-label="项目名称"]')?.value === name, project.name);
  await page.locator('.activity-rail').getByRole('button', { name: '设置', exact: true }).click();
  const preference = page.getByLabel('引擎偏好', { exact: true });
  const gpuOption = preference.locator('option[value="gpu"]');
  assert.equal(await gpuOption.isDisabled(), false,
    `the exact measured ${kind} solver profile must enable GPU`);
  assert.equal(await preference.inputValue(), 'gpu');
  assert.equal(await page.getByLabel('计算 GPU', { exact: true }).inputValue(), RTX_UUID);
  const supportedTimeStepRatio = project.solver.timeStepRatio;
  const representativeTimeStepRatio = 0.1;
  const timeStepInput = page.getByLabel('时间步比例', { exact: true });
  await timeStepInput.fill(String(representativeTimeStepRatio));
  await timeStepInput.blur();
  await page.waitForFunction(() => document.querySelector('select[aria-label="引擎偏好"] option[value="gpu"]')?.disabled === false);
  await timeStepInput.fill(String(supportedTimeStepRatio));
  await timeStepInput.blur();
  await page.waitForFunction(() => document.querySelector('select[aria-label="引擎偏好"] option[value="gpu"]')?.disabled === false);
  const raysInput = page.getByLabel('每点射线数', { exact: true });
  const supportedRayCount = Number(await raysInput.inputValue());
  await raysInput.fill('128');
  await raysInput.blur();
  await page.waitForFunction(() => document.querySelector('select[aria-label="引擎偏好"] option[value="gpu"]')?.disabled === false);
  await raysInput.fill(String(supportedRayCount));
  await raysInput.blur();
  const representativeProject = JSON.parse(JSON.stringify(project));
  representativeProject.solver.timeStepRatio = representativeTimeStepRatio;
  representativeProject.solver.raysPerPoint = 128;
  const representativeValidation = await api('/api/projects/validate', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify(representativeProject),
  });
  assert.equal(representativeValidation.response.status, 200, JSON.stringify(representativeValidation.body));
  let knownFailureProfileValidationStatus = null;
  if (project.dimension === 3 && project.geometry.kind === 'trench') {
    const knownFailureProject = JSON.parse(JSON.stringify(project));
    knownFailureProject.solver.timeStepRatio = 0.2;
    knownFailureProject.solver.raysPerPoint = 1024;
    const knownFailureValidation = await api('/api/runs', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ kind: 'simulate', project: knownFailureProject }),
    });
    knownFailureProfileValidationStatus = knownFailureValidation.response.status;
    assert.equal(knownFailureProfileValidationStatus, 422, JSON.stringify(knownFailureValidation.body));
  }
  await page.locator('.activity-rail').getByRole('button', { name: '配方', exact: true }).click();
  await page.locator('.step-main').first().click();

  const request = page.waitForRequest(item => item.url().endsWith('/api/runs') && item.method() === 'POST');
  const response = page.waitForResponse(item => item.url().endsWith('/api/runs') && item.request().method() === 'POST');
  await page.getByRole('button', { name: '运行工艺', exact: true }).click();
  const [sent, returned] = await Promise.all([request, response]);
  assert.equal(returned.status(), 202, await returned.text());
  const submitted = sent.postDataJSON().project;
  assert.equal(submitted.solver.enginePreference, 'gpu');
  assert.equal(submitted.solver.gpuDeviceId, RTX_UUID);
  const runId = (await returned.json()).runId;
  const run = await waitForRun(runId);
  const execution = run.actualExecution.find(row => row.stepId === submitted.steps[0].id);
  assert.equal(execution.actualEngine, 'GPU_DISK');
  assert.equal(execution.actualParameters.multiParticleExecution.strategy, 'symmetric-operator-splitting-v1');
  const original = await compareRun(runId, 'original');
  const worker = JSON.parse(await fs.readFile(path.join(original.runDir, 'worker-result.json'), 'utf8'));
  const guard = JSON.parse(await fs.readFile(path.join(original.runDir, 'child-record.json'), 'utf8'));
  assert.equal(worker.selectedGpuDevice.uuid, RTX_UUID);
  assert.equal(worker.cudaDeviceReadback.readbackStatus, 'verified');
  assert.equal(worker.actualSteps[0].executionStrategy, 'symmetric-operator-splitting-v1');
  assert.equal(guard.returnCode, 0);
  assert.deepEqual(guard.remainingOwnedGroupMembers, []);

  await saveAndReopen(page, project.name);
  await page.locator('.activity-rail').getByRole('button', { name: '结果', exact: true }).click();
  const history = page.locator('.history-item').filter({ hasText: runId.slice(0, 8) });
  await history.waitFor();
  await history.click();
  const replayDownload = page.waitForEvent('download');
  await page.getByRole('button', { name: '导出重放配置', exact: true }).click();
  const replayPath = path.join(evidenceDir, 'ui-process-replay.json');
  await (await replayDownload).saveAs(replayPath);
  const replayConfig = JSON.parse(await fs.readFile(replayPath, 'utf8'));
  assert.equal(replayConfig.analysisMode, 'process');
  assert.equal(replayConfig.executionRequirements.steps[0].executionStrategy, 'symmetric-operator-splitting-v1');
  assert.equal(replayConfig.normalizedProject.solver.gpuDeviceId, RTX_UUID);
  const replayRequest = page.waitForRequest(item => item.url().endsWith('/api/runs/replay') && item.method() === 'POST');
  const replayResponse = page.waitForResponse(item => item.url().endsWith('/api/runs/replay') && item.request().method() === 'POST');
  await page.locator('input[type="file"][accept*="application/json"]').setInputFiles(replayPath);
  const [replaySent, replayReturned] = await Promise.all([replayRequest, replayResponse]);
  assert.equal(replayReturned.status(), 202, await replayReturned.text());
  assert.equal(replaySent.postDataJSON().normalizedProject.solver.enginePreference, 'gpu');
  const replayRunId = (await replayReturned.json()).runId;
  const replayRun = await waitForRun(replayRunId);
  assert.equal(replayRun.actualExecution[0].actualEngine, 'GPU_DISK');
  const replayComparison = await compareRun(replayRunId, 'replay');
  const replayWorker = JSON.parse(await fs.readFile(path.join(replayComparison.runDir, 'worker-result.json'), 'utf8'));
  const replayGuard = JSON.parse(await fs.readFile(path.join(replayComparison.runDir, 'child-record.json'), 'utf8'));
  assert.equal(replayWorker.selectedGpuDevice.uuid, RTX_UUID);
  assert.equal(replayWorker.actualSteps[0].executionStrategy, 'symmetric-operator-splitting-v1');
  assert.equal(replayGuard.returnCode, 0);
  assert.deepEqual(replayGuard.remainingOwnedGroupMembers, []);
  assert.deepEqual(pageErrors, []);

  const report = { status: 'passed', dimension: project.dimension, geometry: kind,
    operation: project.steps[0].operation,
    strategy: 'symmetric-operator-splitting-v1', selectedGpuDevice: worker.selectedGpuDevice,
    processRunId: runId, replayRunId, versions: worker.versions,
    representativeProfileValidationStatus: representativeValidation.response.status,
    knownFailureProfileValidationStatus,
    directCpuReference: path.relative(root, cpuReference),
    originalComparison: path.relative(root, original.outputPath),
    replayComparison: path.relative(root, replayComparison.outputPath),
    productWorker: path.relative(root, path.join(original.runDir, 'worker-result.json')),
    productGuard: path.relative(root, path.join(original.runDir, 'child-record.json')),
    replayConfig: path.relative(root, replayPath),
    replayWorker: path.relative(root, path.join(replayComparison.runDir, 'worker-result.json')),
    replayGuard: path.relative(root, path.join(replayComparison.runDir, 'child-record.json')),
    pageErrors };
  const reportPath = path.join(evidenceDir, 'ui-acceptance.json');
  await fs.writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`);
  console.log(JSON.stringify({ status: report.status, runId, replayRunId,
    reportPath: path.relative(root, reportPath) }, null, 2));
} catch (error) {
  await fs.writeFile(path.join(evidenceDir, 'ui-acceptance.failure.json'), `${JSON.stringify({
    status: 'failed', failure: { message: error.message, stack: error.stack }, pageErrors,
    pageTextTail: (await page.locator('body').innerText().catch(() => '')).slice(-1400),
  }, null, 2)}\n`);
  throw error;
} finally {
  await context.close();
  await browser.close();
}
