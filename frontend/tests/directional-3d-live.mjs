/** Live Chromium + installed ViennaPS acceptance for a 3D DirectionalProcess. */
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '../..');
const evidence = path.join(root, 'docs/directional-evidence');
const baseUrl = (process.env.WORKBENCH_URL ?? 'http://127.0.0.1:8786').replace(/\/$/, '');
const projectInputPath = path.join(evidence, 'browser-3d-project-input.json');
const submittedPath = path.join(evidence, 'browser-3d-submitted-project.json');
const directPath = path.join(evidence, 'browser-3d-direct-reference.json');
const guardPath = path.join(evidence, 'browser-3d-direct-reference-guard.json');
const logPath = path.join(evidence, 'browser-3d-direct-reference.log');
const projectExportPath = path.join(evidence, 'browser-3d-project-export.json');
const replayExportPath = path.join(evidence, 'browser-3d-replay-export.json');

const projectInput = {
  schemaVersion: 5,
  name: `DirectionalProcess 3D 实时验收 ${Date.now()}`,
  dimension: 3,
  units: { length: 'um', time: 's', rate: 'um/s', angle: 'deg' },
  domain: { gridDelta: 0.2, xExtent: 4, yExtent: 4, boundaryXY: 'reflective' },
  geometry: { kind: 'plane', height: 0, substrateMaterial: 'Si' },
  steps: [],
  solver: { timeStepRatio: 0.25, raysPerPoint: 32, cpuThreads: 1,
    random: { mode: 'fixed', seed: 271828 }, enginePreference: 'cpu', timeoutSeconds: 90 },
  view3D: { projection: 'parallel', camera: null, displayMode: 'surface', visibleMaterialIds: [], opacityByMaterial: {},
    clip: { enabled: false, axis: 'x', position: 0, keep: 'positive' }, section: { enabled: false, axis: 'y', position: 0 } },
};
await fs.mkdir(evidence, { recursive: true });
await fs.writeFile(projectInputPath, `${JSON.stringify(projectInput, null, 2)}\n`);

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const context = await browser.newContext({ acceptDownloads: true, viewport: { width: 1600, height: 1000 } });
const page = await context.newPage();
page.setDefaultTimeout(20000);
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
  assert.ok(terminal.has(run?.status), `run ${runId} did not finish (status=${run?.status})`);
  assert.equal(run.status, 'succeeded', run.failureReason ?? `run ended as ${run.status}`);
  return run;
}

async function submitFromUi() {
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
  await page.getByRole('navigation', { name: '项目操作' }).locator('input[type=file]').setInputFiles(projectInputPath);
  await page.waitForFunction(name => document.querySelector('input[aria-label="项目名称"]')?.value === name, projectInput.name);
  await page.getByRole('button', { name: '＋ 添加定向步骤' }).click();
  await page.getByLabel('工艺操作').selectOption('deposit');
  await page.getByLabel('沉积材料').selectOption('W');
  await page.getByLabel('方向 x 分量').fill('0.6');
  await page.getByLabel('方向 x 分量').press('Tab');
  await page.getByLabel('方向 y 分量').fill('0');
  await page.getByLabel('方向 y 分量').press('Tab');
  await page.getByLabel('方向 z 分量').fill('-0.8');
  await page.getByLabel('方向 z 分量').press('Tab');
  await page.getByLabel('定向速率大小').fill('0.25');
  await page.getByLabel('定向速率大小').press('Tab');
  await page.getByLabel('各向同性速率分量').fill('0.05');
  await page.getByLabel('各向同性速率分量').press('Tab');
  const submitted = await submitFromUi();
  assert.equal(submitted.sent.schemaVersion, 5);
  assert.equal(submitted.sent.dimension, 3);
  assert.equal(submitted.sent.steps[0].model, 'directional');
  assert.equal(submitted.sent.steps[0].operation, 'deposit');
  assert.deepEqual(submitted.sent.steps[0].direction, [0.6, 0, -0.8]);
  assert.equal(submitted.sent.steps[0].directionalRateMagnitude, 0.25);
  assert.equal(submitted.sent.steps[0].isotropicRateMagnitude, 0.05);
  assert.equal(submitted.sent.steps[0].depositMaterial, 'W');
  await fs.writeFile(submittedPath, `${JSON.stringify(submitted.sent, null, 2)}\n`);

  const child = execFileSync(path.join(root, '.venv/bin/python'), [path.join(root, 'scripts/bounded_child.py'),
    '--cwd', root, '--record', guardPath, '--log-path', logPath, '--timeout-seconds', '90',
    '--max-rss-mib', '2048', '--max-log-bytes', '2097152', '--', path.join(root, '.venv/bin/python'),
    path.join(root, 'tests/fixtures/directional_direct_reference.py'), submittedPath, directPath],
  { encoding: 'utf8', timeout: 105000 });
  const guard = JSON.parse(await fs.readFile(guardPath, 'utf8'));
  assert.equal(guard.terminationReason, 'exited', child);
  assert.deepEqual(guard.remainingOwnedGroupMembers, []);
  const direct = JSON.parse(await fs.readFile(directPath, 'utf8'));

  const run = await waitForRun(submitted.runId);
  const step = submitted.sent.steps[0];
  const actual = run.actualExecution.find(item => item.stepId === step.id);
  assert.equal(actual.model, 'directional');
  assert.equal(actual.actualEngine, 'not-applicable');
  assert.equal(actual.actualSeed, null);
  assert.deepEqual(actual.actualParameters.nativeUnitDirection, [0.6, 0, -0.8]);
  assert.equal(actual.actualParameters.nativeDirectionalVelocity, 0.25);
  assert.equal(actual.actualParameters.nativeIsotropicVelocity, 0.05);
  assert.equal(actual.actualParameters.calculateVisibility, true);

  const snapshot = await page.evaluate(async ({ runId, stepId }) =>
    fetch(`/api/runs/${runId}/snapshots/${stepId}`).then(response => response.json()), { runId: submitted.runId, stepId: step.id });
  assert.deepEqual(snapshot.triangles, direct[step.id].cells);
  assert.deepEqual(snapshot.points, direct[step.id].points);
  assert.deepEqual(snapshot.cellData.materialIds, direct[step.id].materialIds);
  assert.deepEqual(snapshot.materialIdsByLevelSetIndex, direct[step.id].materialMap);
  assert.ok(snapshot.materialIdsByLevelSetIndex.includes(70), 'W deposition material is missing from the 3D output');

  await page.getByTitle('刷新记录').click();
  const historyRow = page.locator('.history-item').filter({ hasText: submitted.runId.slice(0, 8) });
  await historyRow.waitFor();
  await historyRow.click();
  const historyPanel = page.locator('.directional-history');
  await historyPanel.waitFor();
  const historyText = await historyPanel.innerText();
  assert.match(historyText, /原生定向速度：0\.25 µm\/s/);
  assert.match(historyText, /原生各向同性速度：0\.05 µm\/s/);

  const savePromise = page.waitForResponse(response => response.url().endsWith('/api/projects') && response.request().method() === 'POST');
  await page.getByRole('button', { name: '保存项目' }).click();
  assert.equal((await savePromise).status(), 201);
  await page.getByLabel('已保存项目').locator('option').filter({ hasText: projectInput.name }).first().waitFor({ state: 'attached' });
  const savedOptions = await page.getByLabel('已保存项目').locator('option').evaluateAll(options =>
    options.map(option => ({ value: option.value, text: option.textContent ?? '' })));
  const saved = savedOptions.find(option => option.value && option.text.includes(projectInput.name));
  assert.ok(saved, 'saved project was not listed');
  await page.getByLabel('已保存项目').selectOption(saved.value);
  await page.getByRole('button', { name: '打开', exact: true }).click();
  assert.equal(await page.getByLabel('方向 z 分量').inputValue(), '-0.8');
  assert.equal(await page.getByLabel('沉积材料').inputValue(), 'W');

  const projectDownload = page.waitForEvent('download');
  await page.getByRole('button', { name: '导出项目' }).click();
  await (await projectDownload).saveAs(projectExportPath);
  const exported = JSON.parse(await fs.readFile(projectExportPath, 'utf8'));
  assert.equal(exported.schemaVersion, 5);
  assert.deepEqual(exported.steps[0].direction, [0.6, 0, -0.8]);
  assert.equal(exported.steps[0].depositMaterial, 'W');

  const replayDownload = page.waitForEvent('download');
  await page.getByRole('button', { name: '导出重放配置' }).click();
  await (await replayDownload).saveAs(replayExportPath);
  const replayProject = JSON.parse(await fs.readFile(replayExportPath, 'utf8'));
  assert.equal(replayProject.schemaVersion, 5);
  assert.deepEqual(replayProject.executionRequirements.steps, []);
  const replayResponsePromise = page.waitForResponse(response => response.url().endsWith('/api/runs/replay') && response.request().method() === 'POST');
  await page.getByRole('navigation', { name: '项目操作' }).locator('input[type=file]').setInputFiles(replayExportPath);
  const replayResponse = await replayResponsePromise;
  assert.equal(replayResponse.status(), 202, await replayResponse.text());
  const replayRunId = (await replayResponse.json()).runId;
  const replayRun = await waitForRun(replayRunId);
  const replaySnapshot = await page.evaluate(async ({ runId, stepId }) =>
    fetch(`/api/runs/${runId}/snapshots/${stepId}`).then(response => response.json()), { runId: replayRunId, stepId: step.id });
  assert.deepEqual(replaySnapshot.points, snapshot.points);
  assert.deepEqual(replaySnapshot.triangles, snapshot.triangles);
  assert.deepEqual(replaySnapshot.cellData.materialIds, snapshot.cellData.materialIds);
  assert.deepEqual(replaySnapshot.materialIdsByLevelSetIndex, snapshot.materialIdsByLevelSetIndex);
  assert.equal(replayRun.actualExecution[0].actualParameters.nativeDirectionalVelocity, 0.25);
  assert.deepEqual(pageErrors, []);

  const result = { status: 'passed', browser: 'Chromium', baseUrl, dimension: 3, schemaVersion: 5,
    submittedProject: submitted.sent, runId: submitted.runId, directNativeReferenceMatches: true,
    directReferenceGuard: guard, comparedSnapshot: { points: snapshot.points.length, triangles: snapshot.triangles.length,
      materialIds: snapshot.cellData.materialIds, materialMap: snapshot.materialIdsByLevelSetIndex },
    actualExecution: actual, replayRunId, replayMeshMatches: true,
    projectSaveOpenExportPassed: true, historyPassed: true, pageErrors };
  const evidencePath = path.join(evidence, 'browser-3d-live.json');
  await fs.writeFile(evidencePath, `${JSON.stringify(result, null, 2)}\n`);
  console.log(JSON.stringify({ status: result.status, runId: result.runId, replayRunId,
    directNativeReferenceMatches: true, replayMeshMatches: true, evidencePath }, null, 2));
} finally {
  await context.close();
  await browser.close();
  await fs.unlink(projectInputPath).catch(() => {});
}
