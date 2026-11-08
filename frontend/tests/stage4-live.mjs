/** Real Chromium + localhost candidate acceptance for Stage 4 controls/results. */
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '../..');
const evidence = path.join(root, 'docs/stage4-evidence');
const baseUrl = (process.env.WORKBENCH_URL ?? 'http://127.0.0.1:8779').replace(/\/$/, '');
await fs.mkdir(evidence, { recursive: true });

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const context = await browser.newContext({ acceptDownloads: true, viewport: { width: 1600, height: 1000 } });
const page = await context.newPage();
page.setDefaultTimeout(20000);
const pageErrors = [];
const consoleErrors = [];
const failedRequests = [];
page.on('pageerror', error => pageErrors.push({ message: error.message, stack: error.stack }));
page.on('console', message => { if (message.type() === 'error') consoleErrors.push(message.text()); });
page.on('requestfailed', request => failedRequests.push({ url: request.url(), error: request.failure()?.errorText }));

async function fill(label, value) {
  const input = page.getByLabel(label);
  await input.fill(String(value));
  await input.press('Tab');
}

async function waitForRun(runId) {
  const terminal = new Set(['succeeded', 'failed', 'cancelled', 'timed_out', 'interrupted']);
  const deadline = Date.now() + 120000;
  let result;
  do {
    const response = await fetch(`${baseUrl}/api/runs/${runId}`, { cache: 'no-store' });
    assert.equal(response.status, 200);
    result = await response.json();
    if (terminal.has(result.status)) break;
    await new Promise(resolve => setTimeout(resolve, 200));
  } while (Date.now() < deadline);
  assert.ok(terminal.has(result?.status), `run ${runId} did not finish`);
  assert.equal(result.status, 'succeeded', result.failureReason ?? result.status);
  return result;
}

async function submit(buttonName, endpoint) {
  const request = page.waitForRequest(item => item.url().endsWith(endpoint) && item.method() === 'POST');
  const response = page.waitForResponse(item => item.url().endsWith(endpoint) && item.request().method() === 'POST');
  await page.getByRole('button', { name: buttonName }).click();
  const [sent, returned] = await Promise.all([request, response]);
  assert.equal(returned.status(), 202, await returned.text());
  return { input: sent.postDataJSON(), runId: (await returned.json()).runId };
}

async function openHistory(runId) {
  const row = page.locator('.history-item').filter({ hasText: runId.slice(0, 8) });
  await row.waitFor();
  await row.click();
}

try {
  await page.goto(`${baseUrl}/`);
  await page.waitForTimeout(1000);
  if (pageErrors.length) throw new Error(`page errors during startup: ${JSON.stringify(pageErrors)}`);
  try { await page.getByText('本机服务已连接').waitFor(); }
  catch (error) {
    console.error(JSON.stringify({ url: page.url(), bodyText: await page.locator('body').innerText().catch(() => ''),
      pageErrors, consoleErrors, failedRequests }, null, 2));
    throw error;
  }
  const projectName = `Stage 4 Chromium ${Date.now()}`;
  await page.getByLabel('项目名称').fill(projectName);
  await page.getByRole('button', { name: '平面', exact: true }).click();
  await fill('网格间距', 0.2);
  await fill('x 横向总宽度', 4);
  await page.getByRole('button', { name: '＋ 添加各向同性步骤' }).click();
  await fill('速率大小', 0.2);
  await fill('持续时间', 0.1);
  await fill('时间步比例', 0.3);
  await page.getByText('ViennaLS 离散与自适应参数', { exact: true }).click();
  await page.getByLabel('空间离散').selectOption('WENO_3RD_ORDER');
  await page.getByLabel('时间离散').selectOption('RUNGE_KUTTA_2ND_ORDER');
  await page.getByLabel('自适应时间步').check();
  await fill('自适应细分次数', 8);
  await fill('耗散系数 α', 0.7);
  await page.getByLabel('计算中间速度').check();
  await page.getByLabel('启用速度输出').check();

  const process = await submit('运行工艺', '/api/runs');
  assert.equal(process.input.project.schemaVersion, 7);
  assert.equal(process.input.project.solver.advection.spatialScheme, 'WENO_3RD_ORDER');
  assert.equal(process.input.project.solver.advection.temporalScheme, 'RUNGE_KUTTA_2ND_ORDER');
  assert.equal(process.input.project.solver.advection.adaptiveTimeStepping, true);
  assert.equal(process.input.project.solver.advection.adaptiveTimeStepSubdivisions, 8);
  assert.equal(process.input.project.solver.advection.velocityOutput, true);
  const processResult = await waitForRun(process.runId);
  const output = processResult.intermediateOutputs.find(item => item.format === 'VTP');
  assert.ok(output, 'ViennaLS VTP field output was not stored in the run');
  assert.deepEqual(new Set(output.fields.map(field => field.name)), new Set(['LSValues', 'SegmentID', 'AdvectionVelocities', 'Dissipation']));
  const nativePanel = page.getByRole('region', { name: 'ViennaLS 原生中间场结果' });
  await nativePanel.waitFor();
  await page.getByLabel('原生中间场字段').selectOption('cellData:AdvectionVelocities');
  try {
    await nativePanel.locator('.flux-table thead th').nth(1).waitFor();
    assert.match(await nativePanel.locator('.flux-table thead th').nth(1).innerText(), /AdvectionVelocities/);
  }
  catch (error) {
    console.error(JSON.stringify({ nativePanel: await nativePanel.innerText(), pageErrors, consoleErrors, failedRequests }, null, 2));
    throw error;
  }
  const tableRows = await nativePanel.locator('tbody tr').count();
  assert.ok(tableRows >= 1);
  const vtpDownload = page.waitForEvent('download');
  await page.getByRole('link', { name: '下载原生 VTP' }).click();
  const vtpPath = path.join(evidence, 'browser-advection.vtp');
  await (await vtpDownload).saveAs(vtpPath);
  const vtpBytes = await fs.readFile(vtpPath);
  assert.equal(createHash('sha256').update(vtpBytes).digest('hex'), output.sha256);
  const csvDownload = page.waitForEvent('download');
  await page.getByRole('button', { name: '导出所选字段 CSV' }).last().click();
  const csvPath = path.join(evidence, 'browser-advection.csv');
  await (await csvDownload).saveAs(csvPath);
  assert.match(await fs.readFile(csvPath, 'utf8'), /AdvectionVelocities/);

  const saveResponse = page.waitForResponse(response => response.url().endsWith('/api/projects') && response.request().method() === 'POST');
  await page.getByRole('button', { name: '保存项目' }).click();
  assert.equal((await saveResponse).status(), 201);
  await page.waitForFunction(name => Array.from(document.querySelectorAll('select[aria-label="已保存项目"] option'))
    .some(option => option.textContent?.includes(name)), projectName);
  const options = await page.getByLabel('已保存项目').locator('option').evaluateAll(rows => rows.map(row => ({ value: row.value, text: row.textContent ?? '' })));
  const saved = options.find(row => row.value && row.text.includes(projectName));
  assert.ok(saved);
  await page.getByLabel('已保存项目').selectOption(saved.value);
  await page.getByRole('button', { name: '打开', exact: true }).click();
  assert.equal(await page.getByLabel('空间离散').inputValue(), 'WENO_3RD_ORDER');
  await openHistory(process.runId);
  await page.getByRole('region', { name: 'ViennaLS 原生中间场结果' }).waitFor();

  await page.getByRole('button', { name: '＋ 添加单粒子步骤' }).click();
  await page.getByText('单粒子射线跟踪参数', { exact: true }).click();
  await fill('Disk 半径', 0.2);
  await fill('最大边界命中次数', 240);
  await fill('最大反射次数', 6);
  await fill('最小节点距离比例', 0.12);
  await fill('平滑邻居数', 2);
  await page.getByLabel('通量归一化').selectOption('MAX');
  await page.getByLabel('忽略通量边界').check();
  await fill('随机种子', 20261026);
  const flux2d = await submit('单次计算所选步骤通量', '/api/runs/flux');
  assert.equal(flux2d.input.project.dimension, 2);
  assert.equal(flux2d.input.project.solver.rayTracing.normalizationType, 'MAX');
  assert.equal(flux2d.input.project.solver.rayTracing.maxBoundaryHits, 240);
  assert.equal(flux2d.input.project.solver.rayTracing.maxReflections, 6);
  assert.equal(flux2d.input.project.solver.random.seed, 20261026);
  const flux2dResult = await waitForRun(flux2d.runId);
  await page.getByRole('region', { name: 'ViennaPS 通量场结果' }).waitFor();
  await page.getByLabel('通量字段').selectOption('cellData:particleFlux');
  const fluxPanel = page.getByRole('region', { name: 'ViennaPS 通量场结果' });
  await fluxPanel.locator('.flux-table thead th').nth(1).waitFor();
  assert.match(await fluxPanel.locator('.flux-table thead th').nth(1).innerText(), /particleFlux/);
  const apiFlux2d = await fetch(`${baseUrl}/api/runs/${flux2d.runId}/flux-result`).then(response => response.json());
  assert.equal(apiFlux2d.dimension, 2);
  assert.equal(apiFlux2d.meshIdentity, flux2dResult.fluxResult.meshIdentity);
  assert.equal(apiFlux2d.execution.actualSeed, 20261026);
  assert.ok(apiFlux2d.fields.some(field => field.name === 'particleFlux' && field.association === 'cellData'));

  const replayDownload = page.waitForEvent('download');
  await page.getByRole('button', { name: '导出重放配置' }).click();
  const replayPath = path.join(evidence, 'browser-flux-replay.json');
  await (await replayDownload).saveAs(replayPath);
  const replay = JSON.parse(await fs.readFile(replayPath, 'utf8'));
  assert.equal(replay.schemaVersion, 7);
  assert.equal(replay.analysisMode, 'flux');
  assert.equal(replay.analysisStepId, flux2d.input.stepId);
  const replay2d = await submitReplay(replayPath);
  const replay2dResult = await waitForRun(replay2d.runId);
  const replayedFlux = await fetch(`${baseUrl}/api/runs/${replay2d.runId}/flux-result`).then(response => response.json());
  assert.equal(replayedFlux.meshIdentity, apiFlux2d.meshIdentity);
  assert.deepEqual(replayedFlux.cellData, apiFlux2d.cellData);

  const zipDownload = page.waitForEvent('download');
  await page.getByRole('button', { name: '下载运行结果 ZIP' }).click();
  const zipPath = path.join(evidence, 'browser-flux-run.zip');
  await (await zipDownload).saveAs(zipPath);
  assert.ok((await fs.stat(zipPath)).size > 0);
  await openHistory(flux2d.runId);
  await page.getByRole('region', { name: 'ViennaPS 通量场结果' }).waitFor();

  await page.getByRole('button', { name: '切换到 3D' }).click();
  await page.waitForFunction(() => document.querySelector('button[aria-label="切换到 3D"]')?.getAttribute('aria-pressed') === 'true');
  await page.locator('.step-card .step-main').last().click();
  const flux3d = await submit('单次计算所选步骤通量', '/api/runs/flux');
  assert.equal(flux3d.input.project.dimension, 3);
  const flux3dResult = await waitForRun(flux3d.runId);
  const apiFlux3d = await fetch(`${baseUrl}/api/runs/${flux3d.runId}/flux-result`).then(response => response.json());
  assert.equal(apiFlux3d.dimension, 3);
  assert.equal(apiFlux3d.meshIdentity, flux3dResult.fluxResult.meshIdentity);
  await page.getByRole('region', { name: 'ViennaPS 通量场结果' }).waitFor();
  await assert.deepEqual(pageErrors, []);

  const result = { status: 'passed', browser: 'Chromium', baseUrl, processRunId: process.runId,
    flux2dRunId: flux2d.runId, flux2dReplayRunId: replay2d.runId, flux3dRunId: flux3d.runId,
    processInput: process.input, flux2dInput: flux2d.input, flux3dInput: flux3d.input,
    outputVtp: output, flux2dMeshIdentity: apiFlux2d.meshIdentity, flux3dMeshIdentity: apiFlux3d.meshIdentity,
    browserParsedIntermediateFields: true, pageErrors };
  await fs.writeFile(path.join(evidence, 'browser-live.json'), `${JSON.stringify(result, null, 2)}\n`);
  console.log(JSON.stringify({ status: result.status, processRunId: result.processRunId,
    flux2dRunId: result.flux2dRunId, flux3dRunId: result.flux3dRunId, pageErrors }, null, 2));
} finally {
  await context.close();
  await browser.close();
}

async function submitReplay(replayPath) {
  const response = page.waitForResponse(item => item.url().endsWith('/api/runs/replay') && item.request().method() === 'POST');
  await page.locator('input[type=file]').setInputFiles(replayPath);
  const result = await response;
  assert.equal(result.status(), 202, await result.text());
  return { runId: (await result.json()).runId };
}
