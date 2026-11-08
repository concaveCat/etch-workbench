/**
 * Round-5 review acceptance (H1–H6).
 *
 * H1 03 restores the saved recipe X; an out-of-domain X blocks measure/experiment.
 * H2 rebuilding a Route clears the old measurement; 05 no longer mixes old runs.
 * H3 06 reports persist per project and survive a reload.
 * H4 the GDS form lists registered Layouts immediately and stages its errors.
 * H5 experiment names are unique by default and a planned experiment can be deleted.
 * H6 a 9-variant experiment shows 9 chips, 9 candidates and 9 candidate rows.
 */
import assert from 'node:assert/strict';
import { mkdirSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';
import { executionCompileBasis } from '../src/workflow/fingerprint.ts';

const here = path.dirname(fileURLToPath(import.meta.url));
const baseUrl = (process.env.WORKBENCH_URL ?? 'http://127.0.0.1:8912').replace(/\/$/, '');
const layoutPath = process.env.WORKFLOW_LAYOUT
  ?? path.resolve(here, '../../docs/stage5-evidence/two-structure-two-layer.gds');
const secondLayoutPath = path.resolve(here, '../../docs/stage5-evidence/selected.gds');
const projectPath = process.env.WORKFLOW_PROJECT ?? path.join(here, 'fixtures', 'workflow-2d-five.json');
const shotDir = process.env.WORKFLOW_SHOT_DIR ?? path.resolve(here, '../../var/review-shots');
mkdirSync(shotDir, { recursive: true });

const getJson = async endpoint => {
  const response = await fetch(`${baseUrl}${endpoint}`);
  assert.ok(response.ok, `${endpoint} -> ${response.status}`);
  return response.json();
};
const sendJson = async (endpoint, method, body) => {
  const response = await fetch(`${baseUrl}${endpoint}`, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  assert.ok(response.ok, `${method} ${endpoint} -> ${response.status} ${text}`);
  return text ? JSON.parse(text) : null;
};
const postJson = (endpoint, body) => sendJson(endpoint, 'POST', body);
const putJson = (endpoint, body) => sendJson(endpoint, 'PUT', body);
const waitSegment = async segmentId => {
  for (let index = 0; index < 600; index += 1) {
    const segment = await getJson(`/api/v1/workflow/segments/${segmentId}`);
    if (['succeeded', 'failed', 'cancelled', 'interrupted'].includes(segment.status)) return segment;
    await new Promise(resolve => setTimeout(resolve, 400));
  }
  throw new Error(`segment ${segmentId} did not finish`);
};
const waitExperiment = async experimentId => {
  for (let index = 0; index < 1200; index += 1) {
    const document = await getJson(`/api/v1/workflow/experiments/${experimentId}`);
    if (['completed', 'failed'].includes(document.status)) return document;
    await new Promise(resolve => setTimeout(resolve, 500));
  }
  throw new Error(`experiment ${experimentId} did not finish`);
};

const fixture = JSON.parse(readFileSync(projectPath, 'utf8'));
const managedNames = new Set([
  'Round5 工程 A', 'Round5 九变体', 'Round5 空白工程', 'Round5 GDS 草稿',
]);
for (const row of (await getJson('/api/v1/workflow/projects')).projects ?? []) {
  if (managedNames.has(row.name)) {
    await fetch(`${baseUrl}/api/v1/workflow/projects/${row.projectId}`, { method: 'DELETE' });
  }
}

// --- Shared recipe/compile ---------------------------------------------------
const suffix = Date.now().toString(36);
await postJson('/api/v1/workflow/metrics', {
  schemaVersion: 1, metricId: `metric_round5_${suffix}`, name: 'round5 depth', unit: 'um',
});
const recipe = await postJson('/api/v1/workflow/metrology-recipes', {
  schemaVersion: 1, recipeId: `recipe_round5_${suffix}`, version: 1,
  metricId: `metric_round5_${suffix}`, name: 'round5 depth', dimension: 2,
  method: 'etch_depth',
  parameters: { kind: 'etch_depth', x: 1.0, referenceHeight: 0 },
  material: 'Si',
});

const projectA = { ...fixture, name: 'Round5 工程 A' };
const compiledA = await postJson('/api/v1/workflow/flows/from-project', { project: projectA });
const stepsA = compiledA.compile.project.steps.filter(step => step.enabled);
const routeA = await postJson('/api/v1/workflow/routes/from-compile', {
  compileId: compiledA.compileId,
  name: 'Round5 A Route',
  splitAfterStepId: stepsA[stepsA.length - 2].id,
});
const firstSegment = routeA.plan.segments[0];
const firstRun = await postJson(`/api/v1/workflow/segments/${firstSegment.segmentId}/execute`, {});
const firstDone = await waitSegment(firstRun.segment.segmentId);
assert.equal(firstDone.status, 'succeeded');
const measured = await postJson('/api/v1/workflow/measurements/from-run', {
  recipeId: recipe.recipeId, recipeVersion: recipe.version,
  runId: firstDone.runId, stepId: firstDone.throughStepId,
});
const bound = await postJson(`/api/v1/workflow/measurements/${measured.measurementId}/bind`, {
  segmentId: firstSegment.segmentId, runId: firstDone.runId, stepId: firstDone.throughStepId,
});
const recordA = await postJson('/api/v1/workflow/projects', projectA);
const recordADoc = await getJson(`/api/v1/workflow/projects/${recordA.projectId}`);
await putJson(`/api/v1/workflow/projects/${recordA.projectId}/state`, {
  schemaVersion: 1, compileId: compiledA.compileId,
  compileBasis: executionCompileBasis(recordADoc), routeId: routeA.plan.route.routeId,
  segmentRuns: [{ segmentId: firstSegment.segmentId, runId: firstDone.runId, measurementId: bound.measurementId }],
  measurementIds: [bound.measurementId], experimentIds: [],
});

// --- Project B: a completed nine-variant experiment --------------------------
const projectB = { ...fixture, name: 'Round5 九变体' };
const compiledB = await postJson('/api/v1/workflow/flows/from-project', { project: projectB });
const stepsB = compiledB.compile.project.steps.filter(step => step.enabled);
const experimentB = await postJson('/api/v1/workflow/experiments', {
  name: 'Round5 九变体实验',
  project: projectB,
  flowId: compiledB.flowId,
  compileId: compiledB.compileId,
  recipeId: recipe.recipeId,
  recipeVersion: recipe.version,
  variants: Array.from({ length: 9 }, (_, index) => ({
    parameters: { [stepsB[0].id]: { duration: 0.01 + index * 0.001 } },
    replicates: 1,
    seed: 900 + index,
  })),
  maxAttempts: 1,
  costPerRunUsd: 0,
});
await postJson(`/api/v1/workflow/experiments/${experimentB.experimentId}/run`, {});
const finishedB = await waitExperiment(experimentB.experimentId);
assert.equal(finishedB.status, 'completed', 'nine variants must run');
assert.equal(finishedB.variants.length, 9);
const experimentMeasurement = finishedB.variants[0].replicates[0].measurementId;
assert.ok(experimentMeasurement, 'the experiment must produce a measurement');
const recordB = await postJson('/api/v1/workflow/projects', projectB);
const recordBDoc = await getJson(`/api/v1/workflow/projects/${recordB.projectId}`);
await putJson(`/api/v1/workflow/projects/${recordB.projectId}/state`, {
  schemaVersion: 1, compileId: compiledB.compileId,
  compileBasis: executionCompileBasis(recordBDoc), routeId: null,
  segmentRuns: [], measurementIds: [experimentMeasurement],
  experimentIds: [experimentB.experimentId],
});

const recordC = await postJson('/api/v1/workflow/projects', { ...fixture, name: 'Round5 空白工程' });

// --- GDS layouts: one with a clip, one without -------------------------------
const uploadGds = async filePath => {
  const bytes = readFileSync(filePath);
  const response = await fetch(`${baseUrl}/api/gds-assets?file_name=${encodeURIComponent(path.basename(filePath))}`, {
    method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: bytes,
  });
  assert.ok(response.ok, `gds upload -> ${response.status}`);
  return response.json();
};
const asset = await uploadGds(layoutPath);
const layout = await postJson('/api/v1/workflow/layouts', { assetId: asset.assetId, fileName: asset.fileName });
const structure = layout.structures[0];
const clip = await postJson('/api/v1/workflow/clips', {
  layoutId: layout.layoutId, name: 'round5 clip', structure: structure.name,
  layers: structure.layerDatatypes.map(([layer, datatype]) => ({ layer, datatype, material: 'Si' })),
  bbox: structure.bounds, annotations: [],
});
const asset2 = await uploadGds(secondLayoutPath);
const layout2 = await postJson('/api/v1/workflow/layouts', { assetId: asset2.assetId, fileName: asset2.fileName });
assert.ok(layout2.structures.length > 0);

// --- Browser -----------------------------------------------------------------
const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const page = await browser.newPage({ viewport: { width: 1600, height: 1500 } });
page.setDefaultTimeout(60000);
const errors = [];
const dialogs = [];
page.on('pageerror', error => errors.push(error.message));
page.on('dialog', async dialog => {
  dialogs.push(dialog.type());
  await dialog.accept();
});

const openZone = async name => {
  await page.getByRole('button', { name, exact: true }).click();
};
const closeToStart = async () => {
  if (await page.locator('.spx-save-button').count() > 0) {
    await page.getByRole('button', { name: '文件', exact: true }).click();
    await page.getByRole('menuitem', { name: '关闭工程 / 返回开始页' }).click();
  }
  await page.getByRole('button', { name: '01 工程', exact: true }).click();
  await page.locator('.spx-command').first().waitFor({ state: 'visible', timeout: 30000 });
};
const openProject = async name => {
  await closeToStart();
  const open = page.getByRole('button', { name: `打开工程 ${name}`, exact: true });
  await open.waitFor({ timeout: 30000 });
  await open.click();
  await page.locator('.spx-tree-root').filter({ hasText: name }).waitFor({ timeout: 30000 });
};

try {
  await page.goto(`${baseUrl}/`);

  // ---- H1: recipe X restored, out-of-domain blocked ----------------------
  await openProject('Round5 工程 A');
  await openZone('03 运行');
  await page.locator('[aria-label="当前配方"]').waitFor({ timeout: 30000 });
  let recipeText = await page.locator('[aria-label="当前配方"]').innerText();
  assert.match(recipeText, /X = 1 µm/, recipeText);
  assert.match(recipeText, /刻蚀深度/, recipeText);
  assert.equal(await page.getByLabel('深度 X').inputValue(), '1', 'the X input must restore the saved recipe');
  await page.screenshot({ path: path.join(shotDir, 'round5-03-recipe-restored.png'), fullPage: true });

  // Reload: the recipe comes back from the saved measurement, not a default 0.
  await page.reload();
  await page.locator('.spx-tree-root').filter({ hasText: 'Round5 工程 A' }).waitFor({ timeout: 30000 });
  await openZone('03 运行');
  await page.locator('[aria-label="当前配方"]').waitFor({ timeout: 30000 });
  recipeText = await page.locator('[aria-label="当前配方"]').innerText();
  assert.match(recipeText, /X = 1 µm/, `recipe X must survive a reload: ${recipeText}`);
  assert.equal(await page.getByLabel('深度 X').inputValue(), '1');

  // L1: the marker belongs to its run/step and hides on 初始结构.
  await page.locator('.wf-step-chip').first().waitFor();
  const chipCount = await page.locator('.wf-step-chip').count();
  assert.ok(chipCount >= 2, 'the timeline needs a step chip');
  await page.locator('.wf-step-chip').first().click();
  await page.waitForTimeout(300);
  assert.equal(await page.locator('.wf-marker').count(), 0, 'the marker must hide on another step');
  await page.locator('.wf-step-chip[data-executed="true"]').last().click();
  await page.waitForTimeout(300);
  assert.ok(await page.locator('.wf-marker').count() >= 1, 'the marker returns on its own step');

  await page.getByLabel('深度 X').fill('99');
  await page.locator('[aria-label="配方 X 校验错误"]').waitFor();
  assert.match(await page.locator('[aria-label="配方 X 校验错误"]').innerText(), /X 超出计算域/);
  assert.equal(await page.locator('.wf-run .spx-run-actions').getByRole('button', { name: '测量', exact: true }).isDisabled(), true);
  await page.screenshot({ path: path.join(shotDir, 'round5-03-recipe-out-of-domain.png'), fullPage: true });
  await openZone('04 实验');
  await page.locator('[aria-label="实验空态"]').waitFor({ timeout: 30000 });
  assert.match(await page.locator('[aria-label="实验空态"]').innerText(), /配方 X 超出计算域/);

  // ---- H2: rebuilding the Route clears the old measurement ---------------
  await openZone('03 运行');
  await page.getByLabel('深度 X').fill('1');
  await openZone('02 流程');
  await page.getByRole('button', { name: /重建 Route/ }).click();
  await page.getByRole('button', { name: '确认重建', exact: true }).click();
  await page.locator('.spx-log').filter({ hasText: '已创建 Route' }).waitFor({ timeout: 60000 });
  await openZone('03 运行');
  const runText = await page.locator('.wf-run').innerText();
  assert.match(runText, /测量已失效，需重新测量/, runText);
  assert.equal(await page.locator('.wf-summary[aria-label="当前测量"]').count(), 0, 'the old measurement summary must be gone');
  const treeText = await page.locator('.spx-tree').first().innerText();
  assert.match(treeText, /测量[\s\S]*?需重新测量/, treeText);
  await page.screenshot({ path: path.join(shotDir, 'round5-03-route-rebuilt-measurement-cleared.png'), fullPage: true });
  await openZone('05 分析');
  assert.equal(await page.locator('[aria-label="测量指标表"]').count(), 0, '05 must not keep the old run measurement');

  // ---- H6: nine variants → nine chips / candidates / rows ----------------
  await openProject('Round5 九变体');
  await openZone('05 分析');
  await page.locator('[aria-label="候选表"]').waitFor({ timeout: 120000 });
  assert.equal(await page.locator('.wf-condition-chip').count(), 9, 'nine condition chips');
  assert.equal(await page.locator('[aria-label="候选表"] tbody tr').count(), 9, 'nine candidate rows');
  assert.match(await page.locator('.spx-tree').first().innerText(), /分析[\s\S]*?9 候选/);
  await page.screenshot({ path: path.join(shotDir, 'round5-05-nine-candidates.png'), fullPage: true });

  // ---- H3: report persists per project and survives a reload -------------
  await openZone('06 报告');
  await page.getByRole('button', { name: '生成报告' }).click();
  await page.locator('.wf-report-preview').waitFor({ timeout: 120000 });
  assert.match(await page.locator('.spx-tree').first().innerText(), /报告[\s\S]*?已生成/);
  await page.screenshot({ path: path.join(shotDir, 'round5-06-report-generated.png'), fullPage: true });
  await page.reload();
  await page.locator('.spx-tree-root').filter({ hasText: 'Round5 九变体' }).waitFor({ timeout: 30000 });
  await openZone('06 报告');
  await page.locator('.wf-report-preview').waitFor({ timeout: 30000 });
  assert.match(await page.locator('.spx-tree').first().innerText(), /报告[\s\S]*?已生成 ·/);
  assert.equal(await page.getByRole('button', { name: '下载 HTML' }).count(), 1, 'the download button returns with the report');
  await page.screenshot({ path: path.join(shotDir, 'round5-06-report-restored-after-reload.png'), fullPage: true });

  // Another project must not inherit project B's report.
  await openProject('Round5 空白工程');
  await openZone('06 报告');
  assert.equal(await page.locator('.wf-report-preview').count(), 0, 'project C has no report');
  assert.match(await page.locator('.spx-tree').first().innerText(), /报告[\s\S]*?未生成/);

  // ---- H5: unique default names + delete a planned experiment ------------
  await openProject('Round5 九变体');
  await openZone('04 实验');
  const inputs = page.locator('details.wf-experiment-inputs');
  await inputs.waitFor({ timeout: 30000 });
  if (!await inputs.evaluate(element => element.open)) await inputs.locator('summary').first().click();
  assert.equal(await page.getByLabel('实验名称').inputValue(), '');
  const placeholder = await page.getByLabel('实验名称').getAttribute('placeholder');
  assert.match(placeholder ?? '', /Round5 九变体 · 实验 · \d{2}:\d{2}/, placeholder ?? '');
  await page.getByRole('button', { name: '创建实验' }).click();
  await page.locator('.spx-log').filter({ hasText: '已创建实验' }).waitFor({ timeout: 60000 });
  if (!await inputs.evaluate(element => element.open)) await inputs.locator('summary').first().click();
  await page.getByRole('button', { name: '创建实验' }).click();
  await page.locator('.spx-log').filter({ hasText: '已创建实验' }).waitFor({ timeout: 60000 });
  await page.waitForTimeout(500);
  const experimentListText = await page.locator('[aria-label="实验列表"]').innerText();
  assert.match(experimentListText, /Round5 九变体 · 实验 · \d{2}:\d{2}/, experimentListText);
  assert.match(experimentListText, /Round5 九变体 · 实验 · \d{2}:\d{2} · 2/, `the second default name must be unique: ${experimentListText}`);

  const targetRow = page.locator('.wf-experiment-row').filter({ hasText: '· 2' }).first();
  const targetName = (await targetRow.locator('strong').innerText()).trim();
  const rowsBeforeDelete = await page.locator('.wf-experiment-row').count();
  await targetRow.getByRole('button', { name: '删除', exact: true }).click();
  assert.match(await targetRow.innerText(), /确认删除/);
  await page.screenshot({ path: path.join(shotDir, 'round5-04-experiment-delete-confirm.png'), fullPage: true });
  await targetRow.getByRole('button', { name: '确认删除', exact: true }).click();
  await page.locator('.spx-log').filter({ hasText: '已删除实验' }).waitFor({ timeout: 30000 });
  assert.equal(await page.getByText(targetName, { exact: true }).count(), 0, 'the deleted experiment leaves the list');
  assert.equal(await page.locator('.wf-experiment-row').count(), rowsBeforeDelete - 1, 'exactly one row is removed');

  // The API refuses to delete a running experiment (the UI disables the button).
  const running = await postJson('/api/v1/workflow/experiments', {
    name: 'Round5 运行中删除校验', project: projectB, flowId: compiledB.flowId,
    compileId: compiledB.compileId, recipeId: recipe.recipeId, recipeVersion: recipe.version,
    variants: Array.from({ length: 12 }, (_, index) => ({
      parameters: { [stepsB[0].id]: { duration: 0.01 + index * 0.001 } },
      replicates: 2,
      seed: 2000 + index,
    })),
    maxAttempts: 1,
  });
  await postJson(`/api/v1/workflow/experiments/${running.experimentId}/run`, {});
  let runningStatus = '';
  for (let index = 0; index < 20; index += 1) {
    const document = await getJson(`/api/v1/workflow/experiments/${running.experimentId}`);
    runningStatus = document.status;
    if (['running', 'queued'].includes(runningStatus)) break;
    if (['completed', 'failed'].includes(runningStatus)) break;
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  assert.ok(['running', 'queued'].includes(runningStatus), `the guard experiment must still be running: ${runningStatus}`);
  const refused = await fetch(`${baseUrl}/api/v1/workflow/experiments/${running.experimentId}`, { method: 'DELETE' });
  assert.equal(refused.status, 409, `running delete -> ${refused.status}`);
  assert.match(await refused.text(), /实验运行中，先取消/);
  await waitExperiment(running.experimentId);

  // ---- H4: GDS form lists Layouts immediately and stages its errors ------
  await closeToStart();
  await page.locator('.spx-command').filter({ hasText: '新建工程' }).click();
  const layoutRoute = /\/api\/v1\/workflow\/layouts$/;
  await page.route(layoutRoute, route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ layouts: [] }),
  }));
  await page.getByRole('button', { name: 'GDS / Layout' }).click();
  await page.getByRole('button', { name: '创建工程草稿' }).click();
  await page.locator('.wf-error').waitFor();
  assert.match(await page.locator('.wf-error').innerText(), /还没有 Layout：先上传 GDS 并注册为 Layout/);
  await page.unroute(layoutRoute);

  await page.reload();
  await page.locator('.spx-command').filter({ hasText: '新建工程' }).click();
  await page.getByRole('button', { name: 'GDS / Layout' }).click();
  const layoutSelect = page.getByLabel('版图选择');
  await layoutSelect.waitFor();
  await page.waitForFunction(() => {
    const select = document.querySelector('select[aria-label="版图选择"]');
    return select && select.options.length >= 3;
  });
  assert.ok((await layoutSelect.locator('option').count()) >= 3, 'registered Layouts list without opening 版图/Clip');
  await page.screenshot({ path: path.join(shotDir, 'round5-01-gds-layouts-listed.png'), fullPage: true });

  // Layout with a Clip, nothing selected → choose an existing Clip.
  await layoutSelect.selectOption(layout.layoutId);
  await page.locator('.wf-clip-editor .spx-clip-summary-list').waitFor({ timeout: 30000 });
  await page.getByRole('button', { name: '创建工程草稿' }).click();
  await page.locator('.wf-error').waitFor();
  assert.match(await page.locator('.wf-error').innerText(), /选择已有 Clip 或保存新 Clip/);

  // Layout without a Clip → save one first.
  await layoutSelect.selectOption(layout2.layoutId);
  await page.waitForFunction(() => document.querySelectorAll('.wf-clip-editor .spx-clip-summary-list').length === 0);
  await page.getByRole('button', { name: '创建工程草稿' }).click();
  await page.locator('.wf-error').waitFor();
  assert.match(await page.locator('.wf-error').innerText(), /已有 Layout，选择结构并保存 Clip/);

  // Uploading a GDS auto-registers it and selects its first structure.
  await page.locator('input[type="file"][accept=".gds,.gdsii"]').setInputFiles(layoutPath);
  await page.waitForFunction(() => {
    const select = document.querySelector('select[aria-label="版图选择"]');
    return select && select.value !== '';
  }, { timeout: 30000 });
  assert.ok(await page.getByLabel('版图结构').count() === 1, 'the uploaded Layout exposes its structures');
  await page.screenshot({ path: path.join(shotDir, 'round5-01-gds-upload-autoselect.png'), fullPage: true });

  assert.deepEqual(errors, []);
  console.log(JSON.stringify({
    status: 'passed', baseUrl, shotDir,
    projectA: recordA.projectId, projectB: recordB.projectId, projectC: recordC.projectId,
    experimentId: experimentB.experimentId, measurementId: bound.measurementId,
    clipId: clip.clipId,
  }));
} finally {
  await browser.close();
}
