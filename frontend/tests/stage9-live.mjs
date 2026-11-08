/** Real Chromium Stage 9 GPU, replay, history reload and service lifecycle acceptance. */
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { promisify } from 'node:util';
import { execFile } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const execFileAsync = promisify(execFile);
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const evidence = path.join(root, 'docs/stage9-evidence/browser');
const baseUrl = (process.env.WORKBENCH_URL ?? 'http://127.0.0.1:8784').replace(/\/$/, '');
const dataDir = path.join(root, 'var/stage9-browser-data');
const python = path.join(root, '.venv/bin/python');
const serviceEnv = { ...process.env, WORKBENCH_PORT: '8784', VIENNAPS_WORKBENCH_DATA_DIR: dataDir };
await fs.mkdir(evidence, { recursive: true });

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const context = await browser.newContext({ acceptDownloads: true, viewport: { width: 1680, height: 1200 } });
const page = await context.newPage();
page.setDefaultTimeout(30000);
const pageErrors = [];
const consoleErrors = [];
page.on('pageerror', error => pageErrors.push({ message: error.message, stack: error.stack }));
page.on('console', message => { if (message.type() === 'error') consoleErrors.push(message.text()); });

async function lifecycle(action) {
  const result = await execFileAsync(path.join(root, `scripts/${action}.sh`), [], { cwd: root, env: serviceEnv, timeout: 30000 });
  return result.stdout.trim();
}

async function apiJson(route) {
  const response = await fetch(`${baseUrl}${route}`, { cache: 'no-store' });
  if (!response.ok) assert.fail(`${route}: ${response.status} ${await response.text()}`);
  return response.json();
}

async function waitForRun(runId) {
  const end = Date.now() + 120000;
  let result;
  do {
    result = await apiJson(`/api/runs/${runId}`);
    if (['succeeded', 'failed', 'cancelled', 'timed_out', 'interrupted'].includes(result.status)) break;
    await new Promise(resolve => setTimeout(resolve, 200));
  } while (Date.now() < end);
  assert.equal(result?.status, 'succeeded', result?.failureReason ?? `run ${runId} timed out`);
  return result;
}

async function directReference(project, directory, label) {
  const input = path.join(directory, `${label}.project.json`);
  const output = path.join(directory, `${label}.direct.json`);
  await fs.writeFile(input, `${JSON.stringify(project, null, 2)}\n`);
  await execFileAsync(python, [path.join(root, 'scripts/bounded_child.py'), '--cwd', root,
    '--record', path.join(directory, `${label}.direct.guard.json`), '--log-path', path.join(directory, `${label}.direct.log`),
    '--timeout-seconds', '120', '--max-rss-mib', '1536', '--max-log-bytes', '300000', '--', python,
    path.join(root, 'tests/fixtures/stage8_direct_reference.py'), input, output,
    path.join(directory, `${label}.direct-scratch`), 'GPU_DISK'],
  { cwd: root, timeout: 150000, maxBuffer: 1024 * 1024 });
  return JSON.parse(await fs.readFile(output, 'utf8'));
}

function productSignature(snapshot) {
  return { points: snapshot.points, cells: snapshot.lines, materialIds: snapshot.cellData.materialIds,
    materialIdsByLevelSetIndex: snapshot.materialIdsByLevelSetIndex };
}

function directSignature(snapshot) {
  return { points: snapshot.points, cells: snapshot.cells, materialIds: snapshot.materialIds,
    materialIdsByLevelSetIndex: snapshot.materialIdsByLevelSetIndex };
}

let report = { status: 'running', service: baseUrl, versions: { viennaps: '4.6.2', viennals: '5.8.5' }, pageErrors, consoleErrors };
try {
  await page.goto(`${baseUrl}/`);
  await page.getByText('本机服务已连接').waitFor();
  await page.getByRole('button', { name: '新建工程', exact: true }).click();
  await page.getByLabel('项目名称').fill(`Stage 9 GPU browser ${Date.now()}`);
  await page.getByLabel('网格间距', { exact: true }).fill('0.2');
  await page.getByLabel('x 横向总宽度', { exact: true }).fill('4');
  await page.getByLabel('选择 ViennaPS 专用模型').selectOption('sf6o2');
  await page.getByRole('button', { name: '＋ 添加专用模型步骤', exact: true }).click();
  await page.locator('.step-card').first().locator('.step-main').click();
  await page.getByLabel('ViennaPS 4.6.2 专用模型').selectOption('sf6o2');
  await page.getByLabel('持续时间', { exact: true }).fill('0.02');
  await page.getByLabel('每点射线数', { exact: true }).fill('32');
  await page.getByLabel('随机种子', { exact: true }).fill('271828');
  const gpuOption = page.getByLabel('引擎偏好', { exact: true }).locator('option[value="gpu"]');
  await gpuOption.waitFor({ state: 'attached' });
  await page.getByLabel('基底材料', { exact: true }).selectOption('SiO2');
  const changedMaterial = await page.getByLabel('基底材料', { exact: true }).inputValue();
  const gpuDisabledForChangedMaterial = await gpuOption.evaluate(option => option.disabled);
  const gateTextAfterMaterialChange = await page.locator('.capability-note').textContent();
  assert.equal(gpuDisabledForChangedMaterial, true, `GPU option closes when measured material roles change: ${JSON.stringify({ changedMaterial, gateTextAfterMaterialChange })}`);
  await page.getByLabel('基底材料', { exact: true }).selectOption('Si');
  assert.equal(await gpuOption.evaluate(option => option.disabled), false, 'Stage 9 direct-reference GPU combination is enabled in the UI');
  await page.getByLabel('引擎偏好', { exact: true }).selectOption('gpu');

  const requestPromise = page.waitForRequest(item => item.url().endsWith('/api/runs') && item.method() === 'POST');
  const responsePromise = page.waitForResponse(item => item.url().endsWith('/api/runs') && item.request().method() === 'POST');
  await page.getByRole('button', { name: '运行工艺', exact: true }).click();
  const [request, response] = await Promise.all([requestPromise, responsePromise]);
  assert.equal(response.status(), 202, response.status() === 202 ? '' : await response.text());
  const project = request.postDataJSON().project;
  assert.equal(project.schemaVersion, 7);
  assert.equal(project.solver.enginePreference, 'gpu');
  assert.equal(project.steps[0].nativeModel, 'sf6o2');
  const runId = (await response.json()).runId;
  const run = await waitForRun(runId);
  const execution = run.actualExecution.find(row => row.stepId === project.steps[0].id);
  assert.equal(execution.actualEngine, 'GPU_DISK');
  assert.equal(execution.requestedEnginePreference, 'gpu');
  assert.equal(execution.actualParameters.nativeModel.nativeProcessEngine, 'GPU_DISK');

  const direct = await directReference(project, evidence, `sf6o2-${runId}`);
  const initial = await apiJson(`/api/runs/${runId}/snapshots/initial`);
  const final = await apiJson(`/api/runs/${runId}/snapshots/${encodeURIComponent(project.steps[0].id)}`);
  assert.deepEqual(productSignature(initial), directSignature(direct.initial));
  assert.deepEqual(productSignature(final), directSignature(direct.final));

  const projectSaved = page.waitForResponse(item => item.url().endsWith('/api/projects') && item.request().method() === 'POST');
  await page.getByRole('button', { name: '保存项目', exact: true }).click();
  assert.equal((await projectSaved).status(), 201);
  const historyItem = page.locator('.history-item').filter({ hasText: runId.slice(0, 8) });
  await historyItem.waitFor();
  await historyItem.click();
  const replayDownload = page.waitForEvent('download');
  await page.getByRole('button', { name: '导出重放配置', exact: true }).click();
  const replayPath = path.join(evidence, `sf6o2-${runId}.replay.json`);
  await (await replayDownload).saveAs(replayPath);
  const replay = JSON.parse(await fs.readFile(replayPath, 'utf8'));
  assert.deepEqual(replay.executionRequirements.steps[0], { stepId: project.steps[0].id, engine: 'GPU_DISK', seed: 271828 });
  const replayInput = page.waitForRequest(item => item.url().endsWith('/api/runs/replay') && item.method() === 'POST');
  const replayResponse = page.waitForResponse(item => item.url().endsWith('/api/runs/replay') && item.request().method() === 'POST');
  await page.locator('input[type="file"][accept*="application/json"]').setInputFiles(replayPath);
  const [replayRequest, replayReturned] = await Promise.all([replayInput, replayResponse]);
  assert.equal(replayReturned.status(), 202, replayReturned.status() === 202 ? '' : await replayReturned.text());
  assert.equal(replayRequest.postDataJSON().normalizedProject.solver.enginePreference, 'gpu');
  const replayRunId = (await replayReturned.json()).runId;
  const replayRun = await waitForRun(replayRunId);
  assert.equal(replayRun.actualExecution[0].actualEngine, 'GPU_DISK');
  const replayFinal = await apiJson(`/api/runs/${replayRunId}/snapshots/${encodeURIComponent(project.steps[0].id)}`);
  assert.deepEqual(productSignature(replayFinal), directSignature(direct.final));

  const lifecycleResults = [];
  lifecycleResults.push(await lifecycleAction('stop'));
  lifecycleResults.push(await lifecycleAction('start'));
  lifecycleResults.push(await lifecycleAction('status'));
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.getByText('本机服务已连接').waitFor();
  const persisted = page.locator('.history-item').filter({ hasText: runId.slice(0, 8) });
  await persisted.waitFor();
  await persisted.click();
  const selector = page.getByLabel('当前结果步骤');
  await selector.waitFor();
  await selector.selectOption(project.steps[0].id);
  await page.waitForFunction(name => document.querySelector('.snapshot-chip span')?.textContent === name, project.steps[0].name);
  assert.equal(await selector.inputValue(), project.steps[0].id);
  assert.deepEqual(pageErrors, []);
  assert.deepEqual(consoleErrors, []);

  report = { ...report, status: 'passed', runId, replayRunId, actualEngine: execution.actualEngine,
    exactDirectReferenceMatch: true, historyReloadedAfterServiceRestart: true, lifecycle: lifecycleResults,
    pageErrors, consoleErrors };
  const output = path.join(evidence, 'browser-gpu-live.json');
  await fs.writeFile(output, `${JSON.stringify(report, null, 2)}\n`);
  console.log(JSON.stringify({ status: report.status, runId, replayRunId, report: path.relative(root, output) }, null, 2));
} catch (error) {
  report = { ...report, status: 'failed', failure: { message: error.message, stack: error.stack }, pageErrors, consoleErrors };
  await fs.writeFile(path.join(evidence, 'browser-gpu-live.failure.json'), `${JSON.stringify(report, null, 2)}\n`);
  throw error;
} finally {
  try { await lifecycle('stop'); } catch {}
  await browser.close();
}

async function lifecycleAction(action) { return lifecycle(action); }
