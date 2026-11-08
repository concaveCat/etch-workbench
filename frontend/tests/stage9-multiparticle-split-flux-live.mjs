/** Real UI/API, GPU_DISK, replay, save/open, history, and field-display coverage for multi-ion flux. */
import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';
import { chromium } from 'playwright';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const baseUrl = (process.env.WORKBENCH_URL ?? 'http://127.0.0.1:8789').replace(/\/$/, '');
const dataDir = path.resolve(process.env.VIENNAPS_WORKBENCH_DATA_DIR
  ?? path.join(root, 'var/stage9-multiparticle-flux-acceptance-2026-10-28'));
const evidenceDir = path.join(root, 'docs/stage9-evidence/gpu-coverage-audit/multiparticle-species-probe/operator-split/single-pass-flux-species');
const execFileAsync = promisify(execFile);
await fs.mkdir(evidenceDir, { recursive: true });

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

function compareFluxToCpu(actual, cpu, step, raysPerPoint) {
  assert.deepEqual(actual.points, cpu.points, 'CPU/GPU flux coordinates');
  assert.deepEqual(actual.topology, cpu.topology, 'CPU/GPU flux topology');
  assert.deepEqual(actual.cellData.scalars.MaterialIds, cpu.cellData.scalars.MaterialIds, 'flux MaterialIds');
  assert.deepEqual(actual.cellData.scalars.LSValues, cpu.cellData.scalars.LSValues, 'flux LSValues');
  const maximum = Math.max(0.08, 0.15 * Math.sqrt(256 / raysPerPoint));
  const comparisons = [];
  for (let index = 0; index < step.species.length; index += 1) {
    const label = step.species[index].label;
    const cpuName = `${label}${index}`;
    const gpuName = `${label}00`;
    const reference = cpu.cellData.scalars[cpuName];
    const values = actual.cellData.scalars[gpuName];
    assert.ok(reference && values, `missing CPU/GPU species flux field ${label}`);
    assert.equal(reference.length, values.length, `${label} field tuple count`);
    const errors = reference.map((value, tuple) => Math.abs(value - values[tuple]) / Math.max(Math.abs(value), 1e-12));
    const maxRelativeError = Math.max(...errors);
    assert.ok(maxRelativeError <= maximum, `${label} field error ${maxRelativeError} exceeds ${maximum}`);
    comparisons.push({ label, cpuField: cpuName, gpuField: gpuName,
      meanRelativeError: errors.reduce((sum, value) => sum + value, 0) / errors.length,
      maxRelativeError, maxRelativeErrorAccepted: maximum });
  }
  return comparisons;
}

async function importProject(page, projectPath, projectName, { assertGpuProcessCapability = false } = {}) {
  const validation = page.waitForResponse(response => response.url().endsWith('/api/projects/validate')
    && response.request().method() === 'POST');
  await page.locator('nav[aria-label="项目操作"] input[type="file"]').setInputFiles(projectPath);
  const result = await validation;
  assert.equal(result.status(), 200, await result.text());
  await page.waitForFunction(name => document.querySelector('input[aria-label="项目名称"]')?.value === name, projectName);
  await page.locator('.activity-rail').getByRole('button', { name: '设置', exact: true }).click();
  assert.equal(await page.getByLabel('计算 GPU', { exact: true }).inputValue(), RTX_UUID);
  const gpuOption = page.getByLabel('引擎偏好', { exact: true }).locator('option[value="gpu"]');
  if (assertGpuProcessCapability) {
    assert.equal(await gpuOption.isDisabled(), false, 'this exact multi-ion GPU process signature must be enabled');
  }
  assert.equal(await page.getByLabel('引擎偏好', { exact: true }).inputValue(), 'gpu');
  await page.locator('.activity-rail').getByRole('button', { name: '配方', exact: true }).click();
  await page.locator('.step-main').first().click();
}

async function saveAndReopenProject(page, projectName, expectedUuid) {
  const projectMenu = page.locator('.topbar-shell details.ui-menu').first();
  if (!(await projectMenu.evaluate(element => element.open))) await projectMenu.locator('summary').click();
  await page.getByRole('button', { name: '保存项目', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('.topbar-project .save-state')?.textContent?.trim() === '已保存');
  const projects = await api('/api/projects');
  assert.equal(projects.response.status, 200);
  const savedRow = projects.body.projects.find(row => row.name === projectName);
  assert.ok(savedRow, `UI save did not persist project ${projectName}`);
  const savedResponse = await api(`/api/projects/${savedRow.id}`);
  assert.equal(savedResponse.response.status, 200);
  const savedProject = { ...savedResponse.body, id: savedRow.id };
  assert.equal(savedProject.project.solver.gpuDeviceId, expectedUuid);
  assert.equal(savedProject.project.name, projectName);
  const projectSelector = page.getByLabel('已保存项目');
  await page.waitForFunction(name => Array.from(document.querySelectorAll('select[aria-label="已保存项目"] option'))
    .some(option => option.textContent?.includes(name)), projectName);
  await projectSelector.selectOption(savedProject.id);
  await page.getByRole('button', { name: '打开', exact: true }).click();
  await page.locator('.activity-rail').getByRole('button', { name: '设置', exact: true }).click();
  await page.waitForFunction(uuid => {
    const input = document.querySelector('select[aria-label="计算 GPU"]');
    return input?.value === uuid;
  }, expectedUuid);
}

async function submitFlux(page, project, dimension) {
  const request = page.waitForRequest(item => item.url().endsWith('/api/runs/flux') && item.method() === 'POST');
  const response = page.waitForResponse(item => item.url().endsWith('/api/runs/flux') && item.request().method() === 'POST');
  await page.getByRole('button', { name: '单次计算所选粒子过程通量', exact: true }).click();
  const [sent, returned] = await Promise.all([request, response]);
  assert.equal(returned.status(), 202, await returned.text());
  const input = sent.postDataJSON();
  assert.equal(input.project.dimension, dimension);
  assert.equal(input.project.solver.enginePreference, 'gpu');
  assert.equal(input.project.solver.gpuDeviceId, RTX_UUID);
  return { runId: (await returned.json()).runId, input };
}

async function submitProcess(page, project, dimension) {
  const request = page.waitForRequest(item => item.url().endsWith('/api/runs') && item.method() === 'POST');
  const response = page.waitForResponse(item => item.url().endsWith('/api/runs') && item.request().method() === 'POST');
  await page.getByRole('button', { name: '运行工艺', exact: true }).click();
  const [sent, returned] = await Promise.all([request, response]);
  assert.equal(returned.status(), 202, await returned.text());
  const input = sent.postDataJSON();
  assert.equal(input.kind, 'simulate');
  assert.equal(input.project.dimension, dimension);
  assert.equal(input.project.solver.enginePreference, 'gpu');
  assert.equal(input.project.solver.gpuDeviceId, RTX_UUID);
  return { runId: (await returned.json()).runId, input };
}

async function compareBrowserProcess(testCase, projectPath, runId, evidenceSuffix) {
  const runDir = path.join(dataDir, 'runs', runId);
  const outputPath = path.join(evidenceDir, `browser-${testCase.dimension}d-process-${evidenceSuffix}-comparison.json`);
  await execFileAsync(path.join(root, '.venv/bin/python'), [
    path.join(root, 'tests/fixtures/compare_multiparticle_product_worker.py'),
    projectPath, testCase.processCpu, runDir, testCase.processCpuGuard,
    path.join(runDir, 'child-record.json'), outputPath,
  ], { cwd: root, timeout: 60000, maxBuffer: 4 * 1024 * 1024 });
  const report = JSON.parse(await fs.readFile(outputPath, 'utf8'));
  assert.equal(report.status, 'passed', JSON.stringify(report.checks));
  assert.ok(Object.values(report.checks).every(value => value === true), JSON.stringify(report.checks));
  return { report, outputPath, runDir };
}

async function replayProcess(page, replayPath, expectedUuid) {
  const request = page.waitForRequest(item => item.url().endsWith('/api/runs/replay') && item.method() === 'POST');
  const response = page.waitForResponse(item => item.url().endsWith('/api/runs/replay') && item.request().method() === 'POST');
  await page.locator('input[type="file"][accept*="application/json"]').setInputFiles(replayPath);
  const [sent, returned] = await Promise.all([request, response]);
  assert.equal(returned.status(), 202, await returned.text());
  const input = sent.postDataJSON();
  assert.equal(input.analysisMode, 'process');
  assert.equal(input.replayMode, 'from-initial');
  assert.equal(input.normalizedProject.solver.enginePreference, 'gpu');
  assert.equal(input.normalizedProject.solver.gpuDeviceId, expectedUuid);
  const runId = (await returned.json()).runId;
  const result = await waitForRun(runId);
  const execution = result.actualExecution.find(row => row.stepId === input.normalizedProject.steps[0].id);
  assert.equal(execution.actualEngine, 'GPU_DISK');
  assert.equal(execution.actualParameters.multiParticleExecution.strategy, 'symmetric-operator-splitting-v1');
  return { runId, result, input };
}

async function replayFlux(page, replayPath, expectedUuid) {
  const response = page.waitForResponse(item => item.url().endsWith('/api/runs/replay') && item.request().method() === 'POST');
  await page.locator('nav[aria-label="项目操作"] input[type="file"]').setInputFiles(replayPath);
  const started = await response;
  assert.equal(started.status(), 202, await started.text());
  const runId = (await started.json()).runId;
  const result = await waitForRun(runId);
  const actual = result.actualExecution[0];
  assert.equal(actual.actualEngine, 'GPU_DISK');
  assert.equal(actual.actualParameters.cudaDeviceReadback.uuid, expectedUuid);
  return { runId, result };
}

const RTX_UUID = 'GPU-5532bb0f-735e-bc93-98ca-b7662a8ebf6d';
const cases = [
  { dimension: 2, project: path.join(path.dirname(evidenceDir), 'stack-trench-2d-multi-ion-etch.project.json'),
    processProject: path.join(path.dirname(evidenceDir), 'stack-trench-2d-multi-ion-etch.project.json'),
    processCpu: path.join(path.dirname(evidenceDir), 'stack-trench-2d-multi-ion-etch-cpu.json'),
    processCpuGuard: path.join(path.dirname(evidenceDir), 'stack-trench-2d-multi-ion-etch-cpu.guard.json'),
    cpu: path.join(evidenceDir, 'cpu-multiparticle-reference.json'), runDir: 'product-worker/run-after-event-fix' },
  { dimension: 3, project: path.join(evidenceDir, '3d-stack-deposit/project.json'),
    processProject: path.join(path.dirname(evidenceDir), 'stack-trench-3d-multi-ion-deposit.project.json'),
    processCpu: path.join(path.dirname(evidenceDir), 'stack-trench-3d-multi-ion-deposit-cpu.json'),
    processCpuGuard: path.join(path.dirname(evidenceDir), 'stack-trench-3d-multi-ion-deposit-cpu.guard.json'),
    cpu: path.join(evidenceDir, '3d-stack-deposit/cpu-reference.json'), runDir: '3d-stack-deposit/product-run' },
];
const pageErrors = [];
const apiEvents = [];
const results = [];
const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const context = await browser.newContext({ acceptDownloads: true, viewport: { width: 1680, height: 1100 } });
const page = await context.newPage();
page.setDefaultTimeout(30000);
page.on('pageerror', error => pageErrors.push({ message: error.message, stack: error.stack }));
page.on('request', request => {
  if (/\/api\/(projects|runs)/.test(request.url())) apiEvents.push({ event: 'request', method: request.method(), url: request.url() });
});
page.on('response', response => {
  if (/\/api\/(projects|runs)/.test(response.url())) apiEvents.push({ event: 'response', method: response.request().method(), status: response.status(), url: response.url() });
});

try {
  const inventory = await api('/api/gpu-devices');
  assert.equal(inventory.response.status, 200);
  const device = inventory.body.devices.find(row => row.uuid === RTX_UUID);
  assert.ok(device, 'RTX 3060 Laptop GPU not exposed by the local CUDA inventory');
  await page.goto(`${baseUrl}/`);
  await page.getByText('本机服务已连接').waitFor();

  for (const testCase of cases) {
    const processInput = JSON.parse(await fs.readFile(testCase.processProject, 'utf8'));
    const processStep = processInput.steps.find(row => row.enabled);
    processInput.name = `多离子 GPU 工艺 ${testCase.dimension}D ${Date.now()}`;
    processInput.solver.enginePreference = 'gpu';
    processInput.solver.gpuDeviceId = RTX_UUID;
    const processInputPath = path.join(evidenceDir, `browser-${testCase.dimension}d-process-input.project.json`);
    await fs.writeFile(processInputPath, `${JSON.stringify(processInput, null, 2)}\n`);
    await importProject(page, processInputPath, processInput.name, { assertGpuProcessCapability: true });

    const processSubmission = await submitProcess(page, processInput, testCase.dimension);
    const processRun = await waitForRun(processSubmission.runId);
    const processExecution = processRun.actualExecution.find(row => row.stepId === processStep.id);
    assert.equal(processExecution.actualEngine, 'GPU_DISK');
    assert.equal(processExecution.actualParameters.multiParticleExecution.strategy, 'symmetric-operator-splitting-v1');
    const processRunDir = path.join(dataDir, 'runs', processSubmission.runId);
    const processWorker = JSON.parse(await fs.readFile(path.join(processRunDir, 'worker-result.json'), 'utf8'));
    const processGuard = JSON.parse(await fs.readFile(path.join(processRunDir, 'child-record.json'), 'utf8'));
    assert.equal(processWorker.selectedGpuDevice.uuid, RTX_UUID);
    assert.equal(processWorker.versions.viennaps, '4.6.2');
    assert.equal(processWorker.versions.viennals, '5.8.5');
    assert.equal(processWorker.actualSteps[0].executionStrategy, 'symmetric-operator-splitting-v1');
    assert.equal(processGuard.returnCode, 0);
    assert.deepEqual(processGuard.remainingOwnedGroupMembers, []);
    const processComparison = await compareBrowserProcess(testCase, processInputPath, processSubmission.runId, 'original');

    await saveAndReopenProject(page, processInput.name, RTX_UUID);
    await page.locator('.activity-rail').getByRole('button', { name: '结果', exact: true }).click();
    const processHistory = page.locator('.history-item').filter({ hasText: processSubmission.runId.slice(0, 8) });
    await processHistory.waitFor();
    await processHistory.click();
    const processReplayDownload = page.waitForEvent('download');
    await page.getByRole('button', { name: '导出重放配置', exact: true }).click();
    const processReplayPath = path.join(evidenceDir, `browser-${testCase.dimension}d-process-replay.json`);
    await (await processReplayDownload).saveAs(processReplayPath);
    const processReplayConfig = JSON.parse(await fs.readFile(processReplayPath, 'utf8'));
    assert.equal(processReplayConfig.analysisMode, 'process');
    assert.equal(processReplayConfig.replayMode, 'from-initial');
    assert.equal(processReplayConfig.executionRequirements.steps[0].executionStrategy, 'symmetric-operator-splitting-v1');
    assert.equal(processReplayConfig.executionRequirements.steps[0].fluxStrategy, undefined);
    const processReplay = await replayProcess(page, processReplayPath, RTX_UUID);
    const processReplayComparison = await compareBrowserProcess(testCase, processInputPath,
      processReplay.runId, 'replay');

    const input = JSON.parse(await fs.readFile(testCase.project, 'utf8'));
    const step = input.steps.find(row => row.enabled);
    input.name = `多离子 GPU 通量 ${testCase.dimension}D ${Date.now()}`;
    input.solver.enginePreference = 'gpu';
    input.solver.gpuDeviceId = RTX_UUID;
    const inputPath = path.join(evidenceDir, `browser-${testCase.dimension}d-input.project.json`);
    await fs.writeFile(inputPath, `${JSON.stringify(input, null, 2)}\n`);
    await importProject(page, inputPath, input.name);

    const fluxRun = await submitFlux(page, input, testCase.dimension);
    const run = await waitForRun(fluxRun.runId);
    const execution = run.actualExecution.find(row => row.stepId === step.id);
    assert.equal(execution.actualEngine, 'GPU_DISK');
    assert.equal(execution.actualParameters.multiParticleFlux.strategy, 'per-species-native-mesh-merge-v1');
    assert.deepEqual(execution.actualParameters.multiParticleFlux.speciesOrder, step.species.map(row => row.label));
    const fluxApi = await api(`/api/runs/${fluxRun.runId}/flux-result`);
    assert.equal(fluxApi.response.status, 200);
    assert.equal(fluxApi.body.execution.actualEngine, 'GPU_DISK');
    assert.equal(fluxApi.body.execution.gpuDeviceId, RTX_UUID);
    assert.equal(fluxApi.body.fields.filter(row => row.association === 'cellData').length >= step.species.length, true);
    const cpu = JSON.parse(await fs.readFile(testCase.cpu, 'utf8'));
    const fieldComparisons = compareFluxToCpu(fluxApi.body, cpu, step, input.solver.raysPerPoint);

    const panel = page.getByRole('region', { name: 'ViennaPS 通量场结果' });
    await panel.waitFor();
    await page.waitForFunction(names => {
      const options = new Set(Array.from(document.querySelectorAll('select[aria-label="通量字段"] option'))
        .map(option => option.value));
      return names.every(name => options.has(`cellData:${name}00`));
    }, step.species.map(species => species.label));
    const fieldOptions = await page.getByLabel('通量字段').locator('option').evaluateAll(options => options.map(option => option.value));
    for (const species of step.species) {
      const fieldName = `${species.label}00`;
      assert.ok(fieldOptions.includes(`cellData:${fieldName}`), `UI field selector omitted ${fieldName}`);
      await page.getByLabel('通量字段').selectOption(`cellData:${fieldName}`);
      assert.match(await panel.locator('.flux-table thead th').nth(1).innerText(), new RegExp(fieldName));
      const csvDownload = page.waitForEvent('download');
      await page.getByRole('button', { name: '导出所选字段 CSV' }).last().click();
      const download = await csvDownload;
      const csvPath = path.join(evidenceDir, `browser-${testCase.dimension}d-${fieldName}.csv`);
      await download.saveAs(csvPath);
      assert.match(await fs.readFile(csvPath, 'utf8'), new RegExp(fieldName));
    }

    const runPath = path.join(dataDir, 'runs', fluxRun.runId);
    const worker = JSON.parse(await fs.readFile(path.join(runPath, 'worker-result.json'), 'utf8'));
    const childGuard = JSON.parse(await fs.readFile(path.join(runPath, 'child-record.json'), 'utf8'));
    assert.equal(worker.selectedGpuDevice.uuid, RTX_UUID);
    assert.equal(worker.actualSteps[0].fluxStrategy, 'per-species-native-mesh-merge-v1');
    assert.equal(childGuard.returnCode, 0);
    assert.deepEqual(childGuard.remainingOwnedGroupMembers, []);
    assert.ok((await fs.stat(path.join(runPath, 'flux.vtp'))).size > 0);
    for (let i = 0; i < step.species.length; i += 1) {
      assert.ok((await fs.stat(path.join(runPath, 'flux-species', `species-${String(i).padStart(2, '0')}.vtp`))).size > 0);
    }

    const replayDownload = page.waitForEvent('download');
    await page.getByRole('button', { name: '导出重放配置', exact: true }).click();
    const replayPath = path.join(evidenceDir, `browser-${testCase.dimension}d-flux-replay.json`);
    await (await replayDownload).saveAs(replayPath);
    const replay = JSON.parse(await fs.readFile(replayPath, 'utf8'));
    assert.equal(replay.analysisMode, 'flux');
    assert.equal(replay.executionRequirements.steps[0].fluxStrategy, 'per-species-native-mesh-merge-v1');
    assert.equal(replay.executionRequirements.steps[0].executionStrategy, undefined);
    assert.equal(replay.normalizedProject.solver.gpuDeviceId, RTX_UUID);
    const replayRun = await replayFlux(page, replayPath, RTX_UUID);
    const replayFluxResult = await api(`/api/runs/${replayRun.runId}/flux-result`);
    assert.equal(replayFluxResult.response.status, 200);
    assert.deepEqual(replayFluxResult.body.points, fluxApi.body.points);
    assert.deepEqual(replayFluxResult.body.topology, fluxApi.body.topology);
    assert.deepEqual(replayFluxResult.body.cellData, fluxApi.body.cellData);
    const replayRunDir = path.join(dataDir, 'runs', replayRun.runId);
    const replayWorker = JSON.parse(await fs.readFile(path.join(replayRunDir, 'worker-result.json'), 'utf8'));
    const replayGuard = JSON.parse(await fs.readFile(path.join(replayRunDir, 'child-record.json'), 'utf8'));
    assert.equal(replayWorker.selectedGpuDevice.uuid, RTX_UUID);
    assert.equal(replayWorker.actualSteps[0].fluxStrategy, 'per-species-native-mesh-merge-v1');
    assert.equal(replayGuard.returnCode, 0);
    assert.deepEqual(replayGuard.remainingOwnedGroupMembers, []);

    const zipDownload = page.waitForEvent('download');
    await page.getByRole('button', { name: '下载运行结果 ZIP', exact: true }).click();
    const zipPath = path.join(evidenceDir, `browser-${testCase.dimension}d-flux-run.zip`);
    await (await zipDownload).saveAs(zipPath);
    assert.ok((await fs.stat(zipPath)).size > 0);
    await saveAndReopenProject(page, input.name, RTX_UUID);
    await page.locator('.activity-rail').getByRole('button', { name: '结果', exact: true }).click();
    const historyRow = page.locator('.history-item').filter({ hasText: fluxRun.runId.slice(0, 8) });
    await historyRow.waitFor();
    await historyRow.click();
    await panel.waitFor();
    await page.waitForFunction(({ runIdPrefix, meshIdentity, names }) => {
      const options = new Set(Array.from(document.querySelectorAll('select[aria-label="通量字段"] option'))
        .map(option => option.value));
      const selectedRun = document.querySelector('.run-id')?.textContent?.includes(runIdPrefix) ?? false;
      const selectedMesh = Array.from(document.querySelectorAll('.flux-result-meta'))
        .some(node => node.textContent?.includes(meshIdentity));
      return selectedRun && selectedMesh && names.every(name => options.has(`cellData:${name}00`));
    }, { runIdPrefix: fluxRun.runId.slice(0, 12), meshIdentity: fluxApi.body.meshIdentity,
      names: step.species.map(species => species.label) });
    assert.ok((await page.locator('select[aria-label="通量字段"] option').count()) >= step.species.length);

    const fluxPath = path.join(runPath, 'flux.json');
    const fluxPayload = JSON.parse(await fs.readFile(fluxPath, 'utf8'));
    results.push({ dimension: testCase.dimension, project: path.relative(root, testCase.project),
      processProject: path.relative(root, testCase.processProject), processInput: path.relative(root, processInputPath),
      processRunId: processSubmission.runId, processReplayRunId: processReplay.runId,
      processStrategy: processWorker.actualSteps[0].executionStrategy,
      processSelectedGpuDevice: processWorker.selectedGpuDevice,
      processWorker: path.relative(root, path.join(processRunDir, 'worker-result.json')),
      processGuard: path.relative(root, path.join(processRunDir, 'child-record.json')),
      processComparison: path.relative(root, processComparison.outputPath),
      processReplayConfig: path.relative(root, processReplayPath),
      processReplayWorker: path.relative(root, path.join(dataDir, 'runs', processReplay.runId, 'worker-result.json')),
      processReplayGuard: path.relative(root, path.join(dataDir, 'runs', processReplay.runId, 'child-record.json')),
      processReplayComparison: path.relative(root, processReplayComparison.outputPath),
      submittedInput: path.relative(root, inputPath), runId: fluxRun.runId, replayRunId: replayRun.runId,
      actualEngine: execution.actualEngine, selectedGpuDevice: worker.selectedGpuDevice,
      strategy: worker.actualSteps[0].fluxStrategy, fieldNames: fluxPayload.fields.map(row => row.name),
      fieldComparisons, cpuDirectReference: path.relative(root, testCase.cpu),
      productWorker: path.relative(root, path.join(runPath, 'worker-result.json')),
      productGuard: path.relative(root, path.join(runPath, 'child-record.json')),
      fluxJson: path.relative(root, fluxPath), mergedVtp: path.relative(root, path.join(runPath, 'flux.vtp')),
      replayConfig: path.relative(root, replayPath), replayFluxJson: path.relative(root,
        path.join(replayRunDir, 'flux.json')), replayWorker: path.relative(root,
        path.join(replayRunDir, 'worker-result.json')), replayGuard: path.relative(root,
        path.join(replayRunDir, 'child-record.json')), runZip: path.relative(root, zipPath),
      savedProjectId: (await api('/api/projects')).body.projects.find(row => row.name === input.name)?.id,
      historyReloaded: true });
  }
  assert.deepEqual(pageErrors, []);
  const report = { status: 'passed', browser: 'Chromium', service: baseUrl, isolatedDataDir: dataDir,
    installedVersions: { viennaps: '4.6.2', viennals: '5.8.5' }, selectedDevice: device, cases: results, pageErrors };
  const reportPath = path.join(evidenceDir, 'browser-acceptance.json');
  await fs.writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`);
  console.log(JSON.stringify({ status: report.status, caseCount: results.length,
    dimensions: results.map(row => row.dimension), reportPath: path.relative(root, reportPath) }, null, 2));
} catch (error) {
  await fs.writeFile(path.join(evidenceDir, 'browser-acceptance.failure.json'), `${JSON.stringify({
    status: 'failed', failure: { message: error.message, stack: error.stack }, cases: results, pageErrors,
    recentApiEvents: apiEvents.slice(-30), saveState: await page.locator('.topbar-project .save-state').textContent().catch(() => null),
    pageTextTail: (await page.locator('body').innerText().catch(() => '')).slice(-1200),
  }, null, 2)}\n`);
  throw error;
} finally {
  await context.close();
  await browser.close();
}
