/** Real Chromium acceptance for every ViennaPS 4.6.2 specialized model. */
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { promisify } from 'node:util';
import { execFile } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const execFileAsync = promisify(execFile);
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const evidence = path.join(root, 'docs/stage8-evidence/browser');
const baseUrl = (process.env.WORKBENCH_URL ?? 'http://127.0.0.1:8782').replace(/\/$/, '');
const python = path.join(root, '.venv/bin/python');
const models = [
  'sf6o2', 'cf4o2', 'hbro2', 'sf6c4f8', 'fluorocarbon', 'ionBeam', 'wetEtch',
  'teosDeposition', 'teospecvd', 'ald', 'oxidation', 'oxideRegrowth', 'selectiveEpitaxy', 'neutralTransport',
];
const changedInputs = {
  sf6o2: ['oxygenFlux', 0.25], cf4o2: ['polymerFlux', 0.25], hbro2: ['ionFlux', 0.9],
  sf6c4f8: ['etchantFlux', 0.9], fluorocarbon: ['polyFlux', 0.25], ionBeam: ['planeWaferRate', 0.11],
  wetEtch: ['rate100', 0.11], teosDeposition: ['rateP1', 0.055], teospecvd: ['depositionRateIon', 0.025],
  ald: ['growthPerCycle', 0.021], oxidation: ['transferCoefficientUmHr', 0.11],
  oxideRegrowth: ['oxideEtchRate', 0.006], selectiveEpitaxy: ['rate111', 0.55], neutralTransport: ['incomingFlux', 0.25],
};
const fluxModels = new Set(['sf6o2', 'cf4o2', 'hbro2', 'sf6c4f8', 'fluorocarbon', 'ionBeam', 'teosDeposition', 'teospecvd', 'ald', 'neutralTransport']);
const selectedModels = (process.env.STAGE8_MODELS ?? models.join(',')).split(',').filter(Boolean);
const selectedDimensions = (process.env.STAGE8_DIMENSIONS ?? '2,3').split(',').map(Number);
assert.ok(selectedModels.every(model => models.includes(model)), 'unknown model in STAGE8_MODELS');
assert.ok(selectedDimensions.every(dimension => dimension === 2 || dimension === 3), 'STAGE8_DIMENSIONS must be 2 and/or 3');
await fs.mkdir(evidence, { recursive: true });

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const context = await browser.newContext({ acceptDownloads: true, viewport: { width: 1680, height: 1200 } });
const page = await context.newPage();
page.setDefaultTimeout(30000);
const pageErrors = [];
const consoleErrors = [];
page.on('pageerror', error => pageErrors.push({ message: error.message, stack: error.stack }));
page.on('console', message => { if (message.type() === 'error') consoleErrors.push(message.text()); });

async function fill(label, value) {
  const input = page.getByLabel(label, { exact: true });
  await input.fill(String(value));
  await input.press('Tab');
}

async function apiJson(pathname) {
  const response = await fetch(`${baseUrl}${pathname}`, { cache: 'no-store' });
  if (!response.ok) assert.fail(`${pathname}: ${response.status} ${await response.text()}`);
  return response.json();
}

async function waitForRun(runId) {
  const terminal = new Set(['succeeded', 'failed', 'cancelled', 'timed_out', 'interrupted']);
  const deadline = Date.now() + 240000;
  let result;
  do {
    result = await apiJson(`/api/runs/${runId}`);
    if (terminal.has(result.status)) break;
    await new Promise(resolve => setTimeout(resolve, 250));
  } while (Date.now() < deadline);
  assert.ok(terminal.has(result?.status), `run ${runId} did not finish`);
  assert.equal(result.status, 'succeeded', result.failureReason ?? result.status);
  return result;
}

async function submit(buttonName, endpoint) {
  const button = page.getByRole('button', { name: buttonName, exact: true });
  await button.waitFor({ state: 'visible' });
  await page.waitForFunction(label => Array.from(document.querySelectorAll('button'))
    .some(item => item.textContent?.trim() === label && !item.disabled), buttonName);
  const request = page.waitForRequest(item => item.url().endsWith(endpoint) && item.method() === 'POST');
  const response = page.waitForResponse(item => item.url().endsWith(endpoint) && item.request().method() === 'POST');
  await button.click();
  const [sent, returned] = await Promise.all([request, response]);
  assert.equal(returned.status(), 202, returned.status() === 202 ? '' : await returned.text());
  return { input: sent.postDataJSON(), runId: (await returned.json()).runId };
}

function productSignature(snapshot) {
  return {
    points: snapshot.points,
    cells: snapshot.dimension === 3 ? snapshot.triangles : snapshot.lines,
    materialIds: snapshot.cellData.materialIds,
    materialIdsByLevelSetIndex: snapshot.materialIdsByLevelSetIndex,
  };
}

function directSignature(snapshot) {
  return { points: snapshot.points, cells: snapshot.cells, materialIds: snapshot.materialIds,
    materialIdsByLevelSetIndex: snapshot.materialIdsByLevelSetIndex };
}

async function directReference(project, label) {
  const directory = path.join(evidence, label);
  await fs.mkdir(directory, { recursive: true });
  const inputPath = path.join(directory, 'project.json');
  const outputPath = path.join(directory, 'direct-reference.json');
  await fs.writeFile(inputPath, `${JSON.stringify(project, null, 2)}\n`);
  await execFileAsync(python, [path.join(root, 'scripts/bounded_child.py'),
    '--cwd', root, '--record', path.join(directory, 'direct.guard.json'), '--log-path', path.join(directory, 'direct.log'),
    '--timeout-seconds', '150', '--max-rss-mib', '1536', '--max-log-bytes', '300000', '--',
    python, path.join(root, 'tests/fixtures/stage8_direct_reference.py'), inputPath, outputPath, path.join(directory, 'direct-scratch')],
  { cwd: root, timeout: 180000, maxBuffer: 1024 * 1024 });
  return { directory, inputPath, outputPath, result: JSON.parse(await fs.readFile(outputPath, 'utf8')) };
}

async function compareRun(runId, project, direct) {
  const initial = await apiJson(`/api/runs/${runId}/snapshots/initial`);
  assert.deepEqual(productSignature(initial), directSignature(direct.result.initial), 'initial ViennaPS mesh differs');
  const step = project.steps[0];
  const actual = await apiJson(`/api/runs/${runId}/snapshots/${encodeURIComponent(step.id)}`);
  assert.deepEqual(productSignature(actual), directSignature(direct.result.final), `${step.nativeModel} final mesh differs`);
  if (step.nativeModel === 'oxideRegrowth') {
    const cellset = actual.outputs?.find(row => row.kind === 'nativeCellSet');
    assert.ok(cellset, 'OxideRegrowth exposes its native DenseCellSet output');
    const outputResponse = await fetch(`${baseUrl}${cellset.url}`);
    assert.equal(outputResponse.status, 200);
    const productCellset = await outputResponse.json();
    assert.equal(productCellset.numberOfCells, direct.result.final.cellSet.numberOfCells);
    assert.deepEqual(productCellset.fields, direct.result.final.cellSet.fields);
  }
  return { initial, final: actual };
}

async function showResultInViewer(runId, step) {
  const history = page.locator('.history-item').filter({ hasText: runId.slice(0, 8) });
  await history.waitFor();
  await history.click();
  const selector = page.getByLabel('当前结果步骤');
  await selector.waitFor();
  await selector.selectOption(step.id);
  await page.waitForFunction(name => document.querySelector('.snapshot-chip span')?.textContent === name, step.name);
  assert.equal(await selector.inputValue(), step.id);
}

async function prepareProject(dimension, modelId) {
  const dimensionButton = page.getByLabel('工程维度').getByRole('button', { name: `切换到 ${dimension}D`, exact: true });
  if (await dimensionButton.getAttribute('aria-pressed') !== 'true') {
    await dimensionButton.click();
    await page.waitForFunction(value => document.querySelector(`[aria-label="工程维度"] [aria-label="切换到 ${value}D"]`)?.getAttribute('aria-pressed') === 'true', dimension);
  }
  await page.getByRole('button', { name: '新建工程', exact: true }).click();
  const title = `Stage 8 ${modelId} ${dimension}D ${Date.now()}`;
  await page.getByLabel('项目名称').fill(title);
  await fill('网格间距', 0.2);
  await fill('x 横向总宽度', 4);
  if (dimension === 3) await fill('y 横向总宽度', 4);
  if (modelId === 'oxideRegrowth') await page.getByLabel('顶层图案').selectOption('none');
  await page.getByLabel('选择 ViennaPS 专用模型').selectOption(modelId);
  await page.getByRole('button', { name: '＋ 添加专用模型步骤', exact: true }).click();
  await page.locator('.step-card').first().waitFor();
  await page.getByLabel('ViennaPS 4.6.2 专用模型').waitFor();
  await page.getByLabel('ViennaPS 4.6.2 专用模型').selectOption(modelId);
  if (modelId !== 'oxidation') await fill('持续时间', 0.02);
  if (fluxModels.has(modelId)) {
    await fill('每点射线数', 32);
    await fill('随机种子', 271828);
  }
  const [field, value] = changedInputs[modelId];
  if (modelId === 'wetEtch') await page.getByLabel('使用晶向速率').check();
  await fill(field, value);
  return title;
}

async function verifyExecution(run, project) {
  const step = project.steps[0];
  assert.equal(step.model, 'specialized');
  assert.equal(step.nativeModel, project.steps[0].nativeModel);
  assert.equal(step.rateMagnitude, undefined, 'specialized models must not receive a generic rate');
  assert.equal(step.rateMode, undefined, 'specialized models must not receive generic material rates');
  const execution = run.actualExecution.find(row => row.stepId === step.id);
  assert.ok(execution, `${step.nativeModel} execution appears in run history`);
  assert.equal(execution.actualParameters.nativeModel.nativeClass,
    (await apiJson('/api/specialized-models')).models.find(row => row.id === step.nativeModel).nativeClass);
  const parameters = execution.actualParameters.nativeModel.nativeParameters;
  for (const [key, value] of Object.entries(step.nativeParameters)) assert.deepEqual(parameters[key], value, `${key} reaches the native builder`);
  if (fluxModels.has(step.nativeModel)) {
    assert.equal(execution.actualEngine, 'CPU_DISK');
    assert.equal(execution.actualSeed, project.solver.random.seed);
    assert.equal(execution.actualParameters.nativeModel.actualSeed, project.solver.random.seed);
  } else {
    assert.equal(execution.actualEngine, 'not-applicable');
    assert.equal(execution.actualSeed, null);
  }
}

async function saveReopenExportReplay(runId, project, direct) {
  const saveResponse = page.waitForResponse(response => response.url().endsWith('/api/projects') && response.request().method() === 'POST');
  await page.getByRole('button', { name: '保存项目', exact: true }).click();
  assert.equal((await saveResponse).status(), 201);
  await page.waitForFunction(name => Array.from(document.querySelectorAll('select[aria-label="已保存项目"] option'))
    .some(option => option.textContent?.includes(name)), project.name);
  const options = await page.getByLabel('已保存项目').locator('option').evaluateAll(rows => rows.map(row => ({ value: row.value, text: row.textContent ?? '' })));
  const saved = options.find(row => row.value && row.text.includes(project.name));
  assert.ok(saved);
  await page.getByLabel('已保存项目').selectOption(saved.value);
  await page.getByRole('button', { name: '打开', exact: true }).click();
  await page.getByLabel('ViennaPS 4.6.2 专用模型').waitFor({ state: 'detached' });
  await page.locator('.step-card').first().locator('.step-main').click();
  await page.getByLabel('ViennaPS 4.6.2 专用模型').waitFor();
  await page.getByLabel('oxygenFlux').waitFor();
  assert.equal(await page.getByLabel('ViennaPS 4.6.2 专用模型').inputValue(), 'sf6o2');
  assert.equal(await page.getByLabel('oxygenFlux').inputValue(), String(project.steps[0].nativeParameters.oxygenFlux));

  const exportDownload = page.waitForEvent('download');
  await page.getByRole('button', { name: '导出项目', exact: true }).click();
  const exportPath = path.join(direct.directory, 'saved-project.json');
  await (await exportDownload).saveAs(exportPath);
  const exported = JSON.parse(await fs.readFile(exportPath, 'utf8'));
  assert.deepEqual(exported.steps, project.steps);

  const history = page.locator('.history-item').filter({ hasText: runId.slice(0, 8) });
  await history.waitFor();
  await history.click();
  const replayDownload = page.waitForEvent('download');
  await page.getByRole('button', { name: '导出重放配置', exact: true }).click();
  const replayPath = path.join(direct.directory, 'replay-project.json');
  await (await replayDownload).saveAs(replayPath);
  const replay = JSON.parse(await fs.readFile(replayPath, 'utf8'));
  assert.equal(replay.executionRequirements.viennapsVersion, '4.6.2');
  assert.equal(replay.executionRequirements.viennalsVersion, '5.8.5');
  assert.deepEqual(replay.executionRequirements.steps[0], { stepId: project.steps[0].id, engine: 'CPU_DISK', seed: project.solver.random.seed });

  const zipDownload = page.waitForEvent('download');
  await page.getByRole('button', { name: '下载运行结果 ZIP', exact: true }).click();
  const zipPath = path.join(direct.directory, 'run.zip');
  await (await zipDownload).saveAs(zipPath);
  const listing = await execFileAsync(python, ['-c', 'import sys,zipfile; print("\\n".join(zipfile.ZipFile(sys.argv[1]).namelist()))', zipPath]);
  const entries = listing.stdout.trim().split('\n');
  assert.ok(entries.includes('project.json'));
  assert.ok(entries.includes('replay-project.json'));
  assert.ok(entries.includes('state-initial.vpsd'));
  assert.ok(entries.includes(`state-${project.steps[0].id}.vpsd`));

  const replayRequest = page.waitForRequest(item => item.url().endsWith('/api/runs/replay') && item.method() === 'POST');
  const replayResponse = page.waitForResponse(item => item.url().endsWith('/api/runs/replay') && item.request().method() === 'POST');
  await page.locator('input[type="file"][accept*="application/json"]').setInputFiles(replayPath);
  const [sent, returned] = await Promise.all([replayRequest, replayResponse]);
  assert.equal(returned.status(), 202, returned.status() === 202 ? '' : await returned.text());
  assert.equal(sent.postDataJSON().normalizedProject.steps[0].nativeModel, 'sf6o2');
  const replayRunId = (await returned.json()).runId;
  const replayRun = await waitForRun(replayRunId);
  await compareRun(replayRunId, project, direct);
  await verifyExecution(replayRun, project);
  return { projectId: saved.value, replayRunId, zipEntries: entries };
}

async function verifySingleFlux(project, step, directory) {
  const directPath = path.join(directory, 'single-flux-direct.json');
  await execFileAsync(python, [path.join(root, 'scripts/bounded_child.py'),
    '--cwd', root, '--record', path.join(directory, 'single-flux.guard.json'),
    '--log-path', path.join(directory, 'single-flux.log'), '--timeout-seconds', '120',
    '--max-rss-mib', '1536', '--max-log-bytes', '300000', '--', python,
    path.join(root, 'tests/fixtures/stage8_teos_flux_probe.py'), path.join(directory, 'project.json'), step.id, directPath],
  { cwd: root, timeout: 150000, maxBuffer: 1024 * 1024 });
  const direct = JSON.parse(await fs.readFile(directPath, 'utf8'));
  await page.locator('.step-card').first().locator('.step-main').click();
  const fluxSubmit = await submit('单次计算所选粒子过程通量', '/api/runs/flux');
  assert.equal(fluxSubmit.input.stepId, step.id);
  assert.equal(fluxSubmit.input.project.steps[0].nativeModel, 'sf6o2');
  const fluxRun = await waitForRun(fluxSubmit.runId);
  const flux = await apiJson(`/api/runs/${fluxSubmit.runId}/flux-result`);
  assert.deepEqual(flux.points, direct.points);
  assert.deepEqual(flux.topology, direct.topology);
  assert.deepEqual(flux.pointData, direct.pointData);
  assert.deepEqual(flux.cellData, direct.cellData);
  assert.deepEqual(flux.fields, direct.fields);
  assert.equal(flux.meshIdentity, direct.meshIdentity);
  assert.deepEqual(flux.execution.rays.nativeRayTracingMetadata, direct.ray);
  assert.equal(fluxRun.actualExecution[0].actualEngine, 'CPU_DISK');
  assert.equal(fluxRun.actualExecution[0].actualSeed, project.solver.random.seed);
  return fluxSubmit.runId;
}

const acceptance = { status: 'running', service: baseUrl, requestedModels: selectedModels, requestedDimensions: selectedDimensions,
  versions: { viennaps: '4.6.2', viennals: '5.8.5' }, cases: [], pageErrors, consoleErrors };
try {
  await page.goto(`${baseUrl}/`);
  await page.getByText('本机服务已连接').waitFor();
  const catalog = await apiJson('/api/specialized-models');
  assert.equal(catalog.engineVersion, '4.6.2');
  assert.deepEqual(catalog.models.map(row => row.id), models);
  let savedDetails = null;
  let singleFluxRunId = null;
  for (const dimension of selectedDimensions) {
    for (const modelId of selectedModels) {
      const title = await prepareProject(dimension, modelId);
      const submitted = await submit('运行工艺', '/api/runs');
      const project = submitted.input.project;
      const step = project.steps[0];
      assert.equal(project.schemaVersion, 7);
      assert.equal(project.dimension, dimension);
      assert.equal(step.nativeModel, modelId);
      if (modelId === 'oxideRegrowth') assert.deepEqual(project.geometry.pattern, { kind: 'none' });
      assert.equal(step.nativeParameters[changedInputs[modelId][0]], changedInputs[modelId][1]);
      assert.equal(project.domain.xBoundary ?? project.domain.boundaryXY, 'reflective');
      await fs.mkdir(path.join(evidence, `${dimension}d-${modelId}`), { recursive: true });
      const run = await waitForRun(submitted.runId);
      await verifyExecution(run, project);
      const direct = await directReference(project, `${dimension}d-${modelId}`);
      assert.equal(direct.result.versions.viennaps, '4.6.2');
      assert.equal(direct.result.versions.viennals, '5.8.5');
      await compareRun(submitted.runId, project, direct);
      await showResultInViewer(submitted.runId, step);
      acceptance.cases.push({ model: modelId, dimension, projectName: title, runId: submitted.runId,
        directReference: path.relative(root, direct.outputPath), nativeEngine: run.actualExecution[0].actualEngine,
        exactInitialAndFinalMeshMatch: true, viewerDisplayedResultStep: true, changedInput: changedInputs[modelId] });
      if (dimension === 2 && modelId === 'sf6o2') {
        savedDetails = await saveReopenExportReplay(submitted.runId, project, direct);
        singleFluxRunId = await verifySingleFlux(project, step, direct.directory);
      }
    }
  }
  assert.deepEqual(pageErrors, []);
  acceptance.status = 'passed';
  acceptance.savedProject = savedDetails;
  acceptance.singleFluxRunId = singleFluxRunId;
  acceptance.pageErrors = pageErrors;
  acceptance.consoleErrors = consoleErrors;
  const reportPath = path.join(evidence, 'browser-live.json');
  await fs.writeFile(reportPath, `${JSON.stringify(acceptance, null, 2)}\n`);
  console.log(JSON.stringify({ status: acceptance.status, cases: acceptance.cases.length, savedProject: savedDetails,
    singleFluxRunId, pageErrors, consoleErrors, report: path.relative(root, reportPath) }, null, 2));
} catch (error) {
  acceptance.status = 'failed';
  acceptance.failure = { message: error.message, stack: error.stack };
  acceptance.pageErrors = pageErrors;
  acceptance.consoleErrors = consoleErrors;
  await fs.writeFile(path.join(evidence, 'browser-live.failure.json'), `${JSON.stringify(acceptance, null, 2)}\n`);
  throw error;
} finally {
  await browser.close();
}
