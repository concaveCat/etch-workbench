/** Real browser acceptance of geometry recipe controls against ViennaPS. */
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '../..');
const evidence = path.join(root, 'docs/stage2-evidence');
const baseUrl = (process.env.WORKBENCH_URL ?? 'http://127.0.0.1:8779').replace(/\/$/, '');
const inputPath = path.join(evidence, 'browser-geometry-input.json');
const directPath = path.join(evidence, 'browser-geometry-direct-reference.json');
const projectExportPath = path.join(evidence, 'browser-geometry-project-export.json');
const replayExportPath = path.join(evidence, 'browser-geometry-replay-export.json');
const zipExportPath = path.join(evidence, 'browser-geometry-run.zip');

const projectInput = {
  schemaVersion: 3,
  name: `几何操作真实浏览器验收 ${Date.now()}`,
  dimension: 2,
  units: { length: 'um', time: 's', rate: 'um/s', angle: 'deg' },
  domain: { gridDelta: 0.2, xExtent: 4, xBoundary: 'reflective' },
  geometry: { kind: 'stack', baseHeight: 0, substrateMaterial: 'Si', layers: [
    { id: 'oxide-layer', name: '氧化层', material: 'SiO2', thickness: 0.6, role: 'film' },
    { id: 'nitride-mask', name: '氮化硅层', material: 'Si3N4', thickness: 0.6, role: 'mask' },
  ], pattern: { kind: 'none' } },
  steps: [],
  solver: { timeStepRatio: 0.25, raysPerPoint: 32, cpuThreads: 1,
    random: { mode: 'fixed', seed: 271828 }, enginePreference: 'cpu', timeoutSeconds: 90 },
  view: { panX: 0, panH: 0, zoom: 1, showGrid: true },
};

await fs.mkdir(evidence, { recursive: true });
await fs.writeFile(inputPath, `${JSON.stringify(projectInput, null, 2)}\n`);
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
  await page.locator('input[type=file]').setInputFiles(inputPath);
  await page.waitForFunction(name => document.querySelector('input[aria-label="项目名称"]')?.value === name,
    projectInput.name, { timeout: 15000 });

  await page.getByRole('button', { name: '＋ 移除材料' }).click();
  await page.getByLabel('要移除的材料').selectOption('Si3N4');
  await page.getByRole('button', { name: '＋ 添加各向同性步骤' }).click();
  await page.getByLabel('速率大小').fill('0.2');
  await page.getByLabel('速率大小').press('Tab');
  await page.getByLabel('持续时间').fill('0.2');
  await page.getByLabel('持续时间').press('Tab');
  await page.getByRole('button', { name: '＋ 平坦化' }).click();
  await page.getByLabel('平坦化截断高度').fill('0.5');
  await page.getByLabel('平坦化截断高度').press('Tab');
  await page.getByRole('button', { name: '＋ 布尔几何' }).click();
  await page.getByLabel('布尔操作', { exact: true }).selectOption('intersection');
  await page.getByLabel('盒体 h 起始位置').fill('0.1');
  await page.getByLabel('盒体 h 起始位置').press('Tab');

  // Exercise copying, ordering and disabling without adding an extra operation to the actual flow.
  const originalPlanarize = page.locator('.step-card').filter({ hasText: '平坦化' }).filter({ hasNotText: '副本' }).first();
  await originalPlanarize.locator('.step-main').click();
  await page.locator('.settings-panel').getByRole('button', { name: '复制', exact: true }).click();
  const duplicate = page.locator('.step-card').filter({ hasText: '平坦化 副本' });
  await duplicate.locator('.step-main').click();
  await page.getByRole('button', { name: '↑ 上移' }).click();
  await duplicate.locator('input[type=checkbox]').uncheck();

  const first = await startFromUi();
  assert.equal(first.sent.schemaVersion, 5);
  assert.deepEqual(first.sent.steps.map(step => step.model), ['removeMaterial', 'isotropic', 'planarize', 'planarize', 'boolean']);
  assert.deepEqual(first.sent.steps.map(step => step.enabled), [true, true, false, true, true]);
  assert.equal(first.sent.steps[0].material, 'Si3N4');
  assert.equal(first.sent.steps[1].operation, 'etch');
  assert.equal(first.sent.steps[1].rateMagnitude, 0.2);
  assert.equal(first.sent.steps[1].duration, 0.2);
  assert.equal(first.sent.steps[2].cutoffHeight, 0.5);
  assert.equal(first.sent.steps[3].cutoffHeight, 0.5);
  assert.equal(first.sent.steps[4].booleanOperation, 'intersection');
  assert.deepEqual(first.sent.steps[4].position, [0, 0.1, 0]);
  assert.equal(first.sent.steps[4].applyToAll, true);

  const directSource = path.join(evidence, 'browser-geometry-submitted-project.json');
  const guardPath = path.join(evidence, 'browser-geometry-reference-guard.json');
  const logPath = path.join(evidence, 'browser-geometry-reference.log');
  await fs.writeFile(directSource, `${JSON.stringify(first.sent, null, 2)}\n`);
  const child = spawnSync(path.join(root, '.venv/bin/python'), [path.join(root, 'scripts/bounded_child.py'), '--cwd', root,
    '--record', guardPath, '--log-path', logPath, '--timeout-seconds', '90', '--max-rss-mib', '2048', '--max-log-bytes', '2097152', '--',
    path.join(root, '.venv/bin/python'), path.join(root, 'tests/fixtures/geometry_operations_direct_reference.py'), directSource, directPath],
  { encoding: 'utf8', timeout: 100000 });
  assert.equal(child.status, 0, `${child.stderr}\n${await fs.readFile(logPath, 'utf8').catch(() => '')}`);
  const guard = JSON.parse(await fs.readFile(guardPath, 'utf8'));
  assert.equal(guard.terminationReason, 'exited');
  assert.deepEqual(guard.remainingOwnedGroupMembers, []);
  const direct = JSON.parse(await fs.readFile(directPath, 'utf8'));

  const firstRun = await waitForRun(first.runId);
  assert.deepEqual(firstRun.snapshots.map(row => row.stepId), ['initial', ...first.sent.steps.filter(step => step.enabled).map(step => step.id)]);
  const compared = [];
  for (const row of firstRun.snapshots) {
    const response = await fetch(`${baseUrl}/api/runs/${first.runId}/snapshots/${row.stepId}`);
    assert.equal(response.status, 200);
    const snapshot = await response.json();
    assert.deepEqual(snapshot.lines, direct[row.stepId].cells, row.stepId);
    assert.deepEqual(snapshot.cellData.materialIds, direct[row.stepId].materialIds, row.stepId);
    assert.deepEqual(snapshot.materialIdsByLevelSetIndex, direct[row.stepId].materialMap, row.stepId);
    assert.deepEqual(snapshot.points, direct[row.stepId].points, row.stepId);
    compared.push({ stepId: row.stepId, points: snapshot.points.length, lines: snapshot.lines.length, materialIds: snapshot.cellData.materialIds });
  }
  const actualGeometry = firstRun.actualExecution.filter(item => ['removeMaterial', 'planarize', 'boolean'].includes(item.model));
  assert.equal(actualGeometry.length, 3);
  assert.equal(actualGeometry.find(item => item.stepId === first.sent.steps[0].id).actualParameters.material, 'Si3N4');
  const booleanActual = actualGeometry.find(item => item.model === 'boolean');
  assert.equal(booleanActual.actualParameters.nativeBooleanEnum, 'INTERSECT');
  assert.equal(booleanActual.actualParameters.booleanOperation, 'intersection');
  for (const record of actualGeometry) {
    assert.equal(record.actualEngine, 'not-applicable');
    assert.equal(record.noOp, false);
    assert.equal(Object.hasOwn(record, 'duration'), false);
    assert.equal(Object.hasOwn(record, 'actualRate'), false);
  }

  await page.getByTitle('刷新记录').click();
  const historyRow = page.locator('.history-item').filter({ hasText: first.runId.slice(0, 8) });
  await historyRow.waitFor();
  await historyRow.click();
  const historyPanel = page.locator('.directional-history');
  await historyPanel.waitFor();
  assert.match(await historyPanel.innerText(), /nativeBooleanEnum/);
  assert.match(await historyPanel.innerText(), /INTERSECT/);

  const saveResponse = page.waitForResponse(response => response.url().endsWith('/api/projects') && response.request().method() === 'POST');
  await page.getByRole('button', { name: '保存项目' }).click();
  assert.equal((await saveResponse).status(), 201);
  const savedOption = page.getByLabel('已保存项目').locator('option').filter({ hasText: projectInput.name });
  await savedOption.first().waitFor({ state: 'attached' });
  const savedOptions = await page.getByLabel('已保存项目').locator('option').evaluateAll(options => options.map(option => ({ value: option.value, text: option.textContent ?? '' })));
  const saved = savedOptions.find(option => option.value && option.text.includes(projectInput.name));
  assert.ok(saved);
  await page.getByLabel('已保存项目').selectOption(saved.value);
  await page.getByRole('button', { name: '打开', exact: true }).click();
  await page.locator('.step-card').first().locator('.step-main').click();
  await page.getByLabel('要移除的材料').waitFor();
  assert.equal(await page.getByLabel('要移除的材料').inputValue(), 'Si3N4');

  const projectDownload = page.waitForEvent('download');
  await page.getByRole('button', { name: '导出项目' }).click();
  await (await projectDownload).saveAs(projectExportPath);
  const exportedProject = JSON.parse(await fs.readFile(projectExportPath, 'utf8'));
  assert.equal(exportedProject.schemaVersion, 5);
  assert.deepEqual(exportedProject.steps.map(step => step.enabled), [true, true, false, true, true]);
  const importValidation = page.waitForResponse(response => response.url().endsWith('/api/projects/validate') && response.request().method() === 'POST');
  await page.locator('input[type=file]').setInputFiles(projectExportPath);
  const importResponse = await importValidation;
  assert.equal(importResponse.status(), 200, await importResponse.text());
  const imported = await importResponse.json();
  assert.equal(imported.normalizedProject.schemaVersion, 5);
  assert.deepEqual(imported.normalizedProject.steps.map(step => step.enabled), [true, true, false, true, true]);

  const replayDownload = page.waitForEvent('download');
  await page.getByRole('button', { name: '导出重放配置' }).click();
  await (await replayDownload).saveAs(replayExportPath);
  const replayProject = JSON.parse(await fs.readFile(replayExportPath, 'utf8'));
  assert.equal(replayProject.schemaVersion, 5);
  assert.deepEqual(replayProject.executionRequirements.steps, []);

  const zipDownload = page.waitForEvent('download');
  await page.getByRole('button', { name: '下载运行结果 ZIP' }).click();
  await (await zipDownload).saveAs(zipExportPath);
  const zipSignature = await fs.readFile(zipExportPath);
  assert.equal(zipSignature.subarray(0, 2).toString('ascii'), 'PK');

  const replayResponsePromise = page.waitForResponse(response => response.url().endsWith('/api/runs/replay') && response.request().method() === 'POST');
  await page.locator('input[type=file]').setInputFiles(replayExportPath);
  const replayResponse = await replayResponsePromise;
  assert.equal(replayResponse.status(), 202, await replayResponse.text());
  const replayRunId = (await replayResponse.json()).runId;
  const replayRun = await waitForRun(replayRunId);
  for (const row of firstRun.snapshots) {
    const [left, right] = await Promise.all([
      fetch(`${baseUrl}/api/runs/${first.runId}/snapshots/${row.stepId}`).then(response => response.json()),
      fetch(`${baseUrl}/api/runs/${replayRunId}/snapshots/${row.stepId}`).then(response => response.json()),
    ]);
    assert.deepEqual(right.points, left.points, row.stepId);
    assert.deepEqual(right.lines, left.lines, row.stepId);
    assert.deepEqual(right.cellData.materialIds, left.cellData.materialIds, row.stepId);
    assert.deepEqual(right.materialIdsByLevelSetIndex, left.materialIdsByLevelSetIndex, row.stepId);
  }
  await page.getByRole('button', { name: '切换到 3D' }).waitFor();
  await page.waitForFunction(() => {
    const button = [...document.querySelectorAll('button')].find(item => item.getAttribute('aria-label') === '切换到 3D');
    return !!button && !button.disabled;
  });
  await page.getByRole('button', { name: '切换到 3D' }).click();
  await page.waitForFunction(() => document.querySelector('button[aria-label="切换到 3D"]')?.getAttribute('aria-pressed') === 'true');
  await page.locator('.step-card').filter({ hasText: '布尔几何' }).locator('.step-main').click();
  await page.getByLabel('布尔操作应用到所有材料界面').uncheck();
  const second = await startFromUi();
  assert.equal(second.sent.schemaVersion, 5);
  assert.equal(second.sent.dimension, 3);
  assert.deepEqual(second.sent.steps.map(step => step.model), ['removeMaterial', 'isotropic', 'planarize', 'planarize', 'boolean']);
  assert.deepEqual(second.sent.steps[4].position, [0, 0, 0.1]);
  assert.equal(second.sent.steps[4].depth, second.sent.steps[4].width);
  assert.equal(second.sent.steps[4].applyToAll, false);
  const direct3DPath = path.join(evidence, 'browser-geometry-3d-direct-reference.json');
  const source3DPath = path.join(evidence, 'browser-geometry-3d-submitted-project.json');
  const guard3DPath = path.join(evidence, 'browser-geometry-3d-reference-guard.json');
  const log3DPath = path.join(evidence, 'browser-geometry-3d-reference.log');
  await fs.writeFile(source3DPath, `${JSON.stringify(second.sent, null, 2)}\n`);
  const child3D = spawnSync(path.join(root, '.venv/bin/python'), [path.join(root, 'scripts/bounded_child.py'), '--cwd', root,
    '--record', guard3DPath, '--log-path', log3DPath, '--timeout-seconds', '90', '--max-rss-mib', '2048', '--max-log-bytes', '2097152', '--',
    path.join(root, '.venv/bin/python'), path.join(root, 'tests/fixtures/geometry_operations_direct_reference.py'), source3DPath, direct3DPath],
  { encoding: 'utf8', timeout: 100000 });
  assert.equal(child3D.status, 0, `${child3D.stderr}\n${await fs.readFile(log3DPath, 'utf8').catch(() => '')}`);
  const guard3D = JSON.parse(await fs.readFile(guard3DPath, 'utf8'));
  assert.equal(guard3D.terminationReason, 'exited');
  assert.deepEqual(guard3D.remainingOwnedGroupMembers, []);
  const direct3D = JSON.parse(await fs.readFile(direct3DPath, 'utf8'));
  const secondRun = await waitForRun(second.runId);
  const compared3D = [];
  for (const row of secondRun.snapshots) {
    const response = await fetch(`${baseUrl}/api/runs/${second.runId}/snapshots/${row.stepId}`);
    assert.equal(response.status, 200);
    const snapshot = await response.json();
    assert.deepEqual(snapshot.triangles, direct3D[row.stepId].cells, row.stepId);
    assert.deepEqual(snapshot.cellData.materialIds, direct3D[row.stepId].materialIds, row.stepId);
    assert.deepEqual(snapshot.materialIdsByLevelSetIndex, direct3D[row.stepId].materialMap, row.stepId);
    assert.deepEqual(snapshot.points, direct3D[row.stepId].points, row.stepId);
    compared3D.push({ stepId: row.stepId, points: snapshot.points.length, triangles: snapshot.triangles.length, materialIds: snapshot.cellData.materialIds });
  }
  assert.deepEqual(pageErrors, []);
  const result = { status: 'passed', browser: 'Chromium', baseUrl, schemaVersion: 5, submittedProject: first.sent,
    runId: first.runId, replayRunId, directNativeReferenceMatches: true, compared, actualExecution: firstRun.actualExecution,
    replayMatches: true, projectSaveOpenExportPassed: true, projectImportPassed: true, runZipExportPassed: true,
    submittedProject3D: second.sent, run3DId: second.runId, direct3DNativeReferenceMatches: true, compared3D,
    actualExecution3D: secondRun.actualExecution, pageErrors };
  await fs.writeFile(path.join(evidence, 'browser-geometry-live.json'), `${JSON.stringify(result, null, 2)}\n`);
  console.log(JSON.stringify({ status: result.status, runId: first.runId, replayRunId, directNativeReferenceMatches: true, pageErrors }, null, 2));
} finally {
  await context.close();
  await browser.close();
  await fs.unlink(inputPath).catch(() => {});
}
