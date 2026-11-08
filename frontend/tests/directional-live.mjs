/** Real browser + candidate HTTP service acceptance; no API mocks. */
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '../..');
const evidence = path.join(root, 'docs/directional-evidence');
const baseUrl = (process.env.WORKBENCH_URL ?? 'http://127.0.0.1:8779').replace(/\/$/, '');
const projectPath = path.join(evidence, 'live-project-input.json');
const projectExportPath = path.join(evidence, 'live-project-export.json');
const replayPath = path.join(evidence, 'live-replay-export.json');

const projectInput = {
  schemaVersion: 3,
  name: `DirectionalProcess 真实浏览器验收 ${Date.now()}`,
  dimension: 2,
  units: { length: 'um', time: 's', rate: 'um/s', angle: 'deg' },
  domain: { gridDelta: 0.2, xExtent: 4, xBoundary: 'reflective' },
  geometry: { kind: 'plane', height: 0, substrateMaterial: 'Si' },
  steps: [],
  solver: { timeStepRatio: 0.25, raysPerPoint: 32, cpuThreads: 1,
    random: { mode: 'fixed', seed: 271828 }, enginePreference: 'cpu', timeoutSeconds: 90 },
  view: { panX: 0, panH: 0, zoom: 1, showGrid: true },
};

await fs.mkdir(evidence, { recursive: true });
await fs.writeFile(projectPath, `${JSON.stringify(projectInput, null, 2)}\n`);
const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const context = await browser.newContext({ acceptDownloads: true, viewport: { width: 1600, height: 1000 } });
const page = await context.newPage();
page.setDefaultTimeout(15000);
const pageErrors = [];
page.on('pageerror', error => pageErrors.push(error.message));

async function waitForRun(runId) {
  const terminal = new Set(['succeeded', 'failed', 'cancelled', 'timed_out', 'interrupted']);
  const deadline = Date.now() + 120000;
  let run;
  do {
    const response = await fetch(`${baseUrl}/api/runs/${runId}`, { cache: 'no-store' });
    assert.equal(response.status, 200, `GET run ${runId} returned HTTP ${response.status}`);
    run = await response.json();
    if (terminal.has(run.status)) break;
    await new Promise(resolve => setTimeout(resolve, 250));
  } while (Date.now() < deadline);
  assert.ok(terminal.has(run?.status), `run ${runId} did not finish within 120 seconds (status=${run?.status})`);
  assert.equal(run.status, 'succeeded', run.failureReason ?? `run ended as ${run.status}`);
  return run;
}

async function startFromUi() {
  const requestPromise = page.waitForRequest(request => request.url().endsWith('/api/runs') && request.method() === 'POST');
  const responsePromise = page.waitForResponse(response => response.url().endsWith('/api/runs') && response.request().method() === 'POST');
  await page.getByRole('button', { name: '运行工艺' }).click();
  const request = await requestPromise;
  const response = await responsePromise;
  assert.equal(response.status(), 202, await response.text());
  return { sent: request.postDataJSON().project, runId: (await response.json()).runId };
}

try {
  await page.goto(`${baseUrl}/`);
  await page.getByText('本机服务已连接').waitFor();
  await page.locator('input[type=file]').setInputFiles(projectPath);
  await page.waitForFunction(name => document.querySelector('input[aria-label="项目名称"]')?.value === name,
    projectInput.name, { timeout: 15000 });

  await page.getByRole('button', { name: '＋ 添加定向步骤' }).click();
  await page.getByLabel('方向 x 分量').fill('0.6');
  await page.getByLabel('方向 x 分量').press('Tab');
  await page.getByLabel('方向 h 分量').fill('-0.8');
  await page.getByLabel('方向 h 分量').press('Tab');
  await page.getByLabel('定向速率大小').fill('0.3');
  await page.getByLabel('定向速率大小').press('Tab');
  await page.getByLabel('各向同性速率分量').fill('0.05');
  await page.getByLabel('各向同性速率分量').press('Tab');

  const first = await startFromUi();
  assert.equal(first.sent.schemaVersion, 5);
  assert.deepEqual(first.sent.steps[0].direction, [0.6, -0.8, 0]);
  assert.equal(first.sent.steps[0].model, 'directional');
  assert.equal(first.sent.steps[0].operation, 'etch');
  assert.equal(first.sent.steps[0].directionalRateMagnitude, 0.3);
  assert.equal(first.sent.steps[0].isotropicRateMagnitude, 0.05);

  const firstRun = await waitForRun(first.runId);
  const actual = firstRun.actualExecution.find(item => item.stepId === first.sent.steps[0].id);
  assert.equal(actual.model, 'directional');
  assert.equal(actual.actualEngine, 'not-applicable');
  assert.equal(actual.actualSeed, null);
  assert.equal(actual.actualParameters.nativeDirectionalVelocity, -0.3);
  assert.equal(actual.actualParameters.nativeIsotropicVelocity, -0.05);
  assert.deepEqual(actual.actualParameters.nativeUnitDirection, [0.6, -0.8, 0]);
  await page.getByTitle('刷新记录').click();
  const historyRow = page.locator('.history-item').filter({ hasText: first.runId.slice(0, 8) });
  await historyRow.waitFor();
  await historyRow.click();
  const historyPanel = page.locator('.directional-history');
  await page.waitForTimeout(1200);
  if (await historyPanel.count() === 0) {
    const diagnostic = await page.evaluate(async id => {
      const run = await fetch(`/api/runs/${id}?cursor=0`, { cache: 'no-store' }).then(response => response.json());
      return { runStatus: run.status, actualExecution: run.actualExecution,
        selectedResultStep: document.querySelector('select[aria-label="当前结果步骤"]')?.value ?? null,
        snapshotChip: document.querySelector('.snapshot-chip')?.innerText ?? null,
        pageText: document.body.innerText.slice(-1800) };
    }, first.runId);
    console.log(`HISTORY_PANEL_DIAGNOSTIC ${JSON.stringify(diagnostic)}`);
  }
  await historyPanel.waitFor();
  const historyText = await historyPanel.innerText();
  assert.match(historyText, /原生定向速度：-0\.3 µm\/s/);
  assert.match(historyText, /原生各向同性速度：-0\.05 µm\/s/);

  const saveResponse = page.waitForResponse(response => response.url().endsWith('/api/projects') && response.request().method() === 'POST');
  await page.getByRole('button', { name: '保存项目' }).click();
  assert.equal((await saveResponse).status(), 201);
  const savedProjectOption = page.getByLabel('已保存项目').locator('option').filter({ hasText: projectInput.name });
  await savedProjectOption.first().waitFor({ state: 'attached' });
  const savedOptions = await page.getByLabel('已保存项目').locator('option').evaluateAll(options => options.map(option => ({ value: option.value, text: option.textContent ?? '' })));
  const saved = savedOptions.find(option => option.value && option.text.includes(projectInput.name));
  assert.ok(saved, 'saved project was not listed after the save response');
  await page.getByLabel('已保存项目').selectOption(saved.value);
  await page.getByRole('button', { name: '打开', exact: true }).click();
  assert.equal(await page.getByLabel('定向速率大小').inputValue(), '0.3');

  const projectDownload = page.waitForEvent('download');
  await page.getByRole('button', { name: '导出项目' }).click();
  await (await projectDownload).saveAs(projectExportPath);
  const exportedProject = JSON.parse(await fs.readFile(projectExportPath, 'utf8'));
  assert.equal(exportedProject.schemaVersion, 5);
  assert.deepEqual(exportedProject.steps[0].direction, [0.6, -0.8, 0]);

  const replayDownload = page.waitForEvent('download');
  await page.getByRole('button', { name: '导出重放配置' }).click();
  await (await replayDownload).saveAs(replayPath);
  const replayProject = JSON.parse(await fs.readFile(replayPath, 'utf8'));
  assert.equal(replayProject.schemaVersion, 5);
  assert.deepEqual(replayProject.executionRequirements.steps, []);

  const replayResponsePromise = page.waitForResponse(response => response.url().endsWith('/api/runs/replay') && response.request().method() === 'POST');
  await page.locator('input[type=file]').setInputFiles(replayPath);
  const replayResponse = await replayResponsePromise;
  assert.equal(replayResponse.status(), 202, await replayResponse.text());
  const replayRunId = (await replayResponse.json()).runId;
  const replayRun = await waitForRun(replayRunId);
  assert.equal(replayRun.actualExecution[0].actualParameters.nativeDirectionalVelocity, -0.3);

  const originalSnapshot = await page.evaluate(async ({ runId, stepId }) =>
    (await fetch(`/api/runs/${runId}/snapshots/${stepId}`)).json(), { runId: first.runId, stepId: first.sent.steps[0].id });
  const replaySnapshot = await page.evaluate(async ({ runId, stepId }) =>
    (await fetch(`/api/runs/${runId}/snapshots/${stepId}`)).json(), { runId: replayRunId, stepId: first.sent.steps[0].id });
  assert.deepEqual(replaySnapshot.points, originalSnapshot.points);
  assert.deepEqual(replaySnapshot.lines, originalSnapshot.lines);
  assert.deepEqual(replaySnapshot.cellData.materialIds, originalSnapshot.cellData.materialIds);
  assert.deepEqual(pageErrors, []);

  const result = {
    status: 'passed', browser: 'Chromium', baseUrl, schemaVersion: 5,
    submittedProject: first.sent, runId: first.runId, replayRunId,
    actualExecution: actual, replayActualExecution: replayRun.actualExecution,
    replayMeshMatches: true, pageErrors,
  };
  await fs.writeFile(path.join(evidence, 'browser-live.json'), `${JSON.stringify(result, null, 2)}\n`);
  console.log(JSON.stringify({ status: result.status, runId: result.runId, replayRunId, replayMeshMatches: true }, null, 2));
} finally {
  await context.close();
  await browser.close();
  await fs.unlink(projectPath).catch(() => {});
}
