/**
 * Round-11: global layout + 02 flow editor + 03 structure timeline.
 *
 *  A. The global right-hand Properties panel is gone (01–06).
 *  B. 02 is three columns: process presets | step graph | step parameters.
 *     The left column only lists category=process presets; materials, flows
 *     and experiments stay in the left "库" tree.
 *  C. A disabled last step is reported as skipped, a Compile that silently
 *     drops an enabled step blocks "创建 Route", and the 03 timeline always
 *     lists every Route step — pending Segments included.
 */
import assert from 'node:assert/strict';
import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';
import { missingCompiledSteps, missingRouteSteps } from '../src/workflow/compileCoverage.ts';

const here = path.dirname(fileURLToPath(import.meta.url));
const baseUrl = (process.env.WORKBENCH_URL ?? 'http://127.0.0.1:8991').replace(/\/$/, '');
const shotDir = process.env.WORKFLOW_SHOT_DIR ?? path.resolve(here, '../../var/review-shots');
mkdirSync(shotDir, { recursive: true });

// Pure coverage helpers: a missing enabled step is never silently skipped.
assert.deepEqual(
  missingCompiledSteps(
    [{ id: 'a', name: 'A', enabled: true }, { id: 'b', name: 'B', enabled: false }, { id: 'c', name: 'C', enabled: true }],
    ['a'],
  ).map(step => step.id),
  ['c'],
  'only enabled steps count as missing from the Compile',
);
assert.deepEqual(missingRouteSteps(['a', 'b', 'c'], ['a', 'b']), ['c'], 'route coverage reports the dropped step');

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

const baseProject = (name, steps) => ({
  schemaVersion: 7,
  name,
  dimension: 2,
  units: { length: 'um', time: 's', rate: 'um/s', angle: 'deg' },
  domain: { gridDelta: 0.2, xExtent: 4.0, xBoundary: 'reflective' },
  geometry: { kind: 'plane', height: 0.0, substrateMaterial: 'Si' },
  steps,
  solver: {
    timeStepRatio: 0.25, raysPerPoint: 16, cpuThreads: 1,
    random: { mode: 'fixed', seed: 7 }, enginePreference: 'cpu', timeoutSeconds: 60,
  },
  view: { panX: 0.0, panH: 0.0, zoom: 1.0, showGrid: true },
});
const etchStep = (index, name) => ({
  id: `r11_step_${String(index).padStart(2, '0')}`,
  name,
  enabled: true,
  operation: 'etch',
  model: 'isotropic',
  rateMagnitude: 0.0,
  duration: 0.01,
  rateMode: 'uniform',
});

const names = ['Round11 参数', 'Round11 跳过', 'Round11 五步'];
for (const row of (await getJson('/api/v1/workflow/projects')).projects ?? []) {
  if (names.includes(row.name)) {
    await fetch(`${baseUrl}/api/v1/workflow/projects/${row.projectId}`, { method: 'DELETE' });
  }
}
await postJson('/api/v1/workflow/projects', baseProject('Round11 参数', [etchStep(1, '参数步骤')]));
await postJson('/api/v1/workflow/projects', baseProject('Round11 跳过', [
  etchStep(1, '第一步'), etchStep(2, '第二步'), etchStep(3, '最后一步'),
]));
await postJson('/api/v1/workflow/projects', baseProject('Round11 五步', [
  etchStep(1, 'one'), etchStep(2, 'two'), etchStep(3, 'three'), etchStep(4, 'four'), etchStep(5, 'five'),
]));

const projectIdByName = async name => {
  const rows = (await getJson('/api/v1/workflow/projects')).projects ?? [];
  const row = rows.find(item => item.name === name);
  assert.ok(row, `project ${name} must exist`);
  return row.projectId;
};

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
const page = await context.newPage();
page.setDefaultTimeout(120000);
const errors = [];
page.on('pageerror', error => errors.push(error.message));

const openZone = async name => { await page.getByRole('button', { name, exact: true }).click(); };
const closeToStart = async () => {
  if (await page.locator('.spx-save-button').count() > 0) {
    await page.getByRole('button', { name: '文件', exact: true }).click();
    await page.getByRole('menuitem', { name: '关闭工程 / 返回开始页' }).click();
    await page.waitForTimeout(400);
    const confirmClose = page.getByRole('button', { name: '确认关闭', exact: true });
    if (await confirmClose.count() > 0) await confirmClose.click();
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
const flowBar = () => page.locator('.spx-flow-bar');
const stepRows = () => page.locator('[aria-label="流程步骤"] tbody tr');
const compileFlow = async () => {
  await flowBar().getByRole('button', { name: /编译流程|重新编译/ }).click();
  await page.locator('.spx-compile-badge').filter({ hasText: '已编译' }).waitFor({ timeout: 120000 });
};
const createRoute = async () => {
  await flowBar().getByRole('button', { name: '创建 Route', exact: true }).click();
  await page.locator('.wf-run .wf-summary').filter({ hasText: 'Route 已创建' }).waitFor({ timeout: 120000 });
};
const saveFromFlowBar = async () => {
  const response = page.waitForResponse(item => {
    const method = item.request().method();
    return (method === 'PUT' || method === 'POST')
      && /^\/api\/v1\/workflow\/projects(\/proj_[a-f0-9]+)?$/.test(new URL(item.url()).pathname);
  });
  await flowBar().getByRole('button', { name: '保存工程', exact: true }).click();
  assert.ok((await response).ok(), 'the project save must succeed');
  await page.waitForTimeout(700);
};
const timelineSteps = () => page.locator('.wf-structure-timeline [data-kind="step"]');

try {
  await page.goto(`${baseUrl}/`);
  await page.locator('.spx-command').first().waitFor({ timeout: 60000 });

  // ---- A. no global right-hand Properties column anywhere -----------------
  await openProject('Round11 参数');
  assert.equal(await page.locator('.spx-right').count(), 0, '01 must not render the right Properties panel');
  await openZone('02 流程');
  assert.equal(await page.locator('.spx-right').count(), 0, '02 must not render the right Properties panel');
  await openZone('03 运行');
  assert.equal(await page.locator('.spx-right').count(), 0, '03 must not render the right Properties panel');

  // ---- B. 02 three columns, process-only insert source --------------------
  await openZone('02 流程');
  await page.locator('.spx-flow-editor').waitFor();
  const layout = await page.evaluate(() => {
    const editor = document.querySelector('.spx-flow-editor');
    const body = document.querySelector('.spx-doc-body');
    const list = document.querySelector('.spx-flow-step-scroll');
    return {
      columns: getComputedStyle(editor).gridTemplateColumns.split(' ').filter(Boolean).length,
      bodyOverflow: body.scrollWidth - body.clientWidth,
      listOverflowY: list ? getComputedStyle(list).overflowY : '',
    };
  });
  assert.equal(layout.columns, 3, '02 must render three columns');
  assert.ok(layout.bodyOverflow <= 1, `02 must not scroll horizontally (${layout.bodyOverflow}px)`);
  assert.equal(layout.listOverflowY, 'auto', 'the middle step list scrolls on its own');

  const source = page.locator('.spx-library-browser');
  assert.match(await source.innerText(), /工艺预设/);
  assert.equal(await page.locator('.spx-library-tabs').count(), 0, '02 must not render material/process/flow/experiment tabs');
  const processCount = ((await getJson('/api/v1/workflow/library/items?category=process')).items ?? []).length;
  assert.equal(await source.locator('.spx-library-item').count(), processCount,
    'the insert source lists exactly the process presets');
  assert.equal(await source.locator('.spx-library-item-main span').filter({ hasText: /µm\/s/ }).count(), processCount,
    'every insert-source row is a process preset with rate/duration');
  assert.equal(await source.getByRole('button', { name: '材料', exact: true }).count(), 0);
  assert.equal(await source.getByRole('button', { name: '流程', exact: true }).count(), 0);
  assert.equal(await source.getByRole('button', { name: '实验', exact: true }).count(), 0);

  // ---- B. vertical parameter rows and model/operation gating --------------
  await stepRows().first().click();
  const inspector = page.locator('.spx-step-inspector');
  await inspector.waitFor();
  const fieldGeometry = await inspector.evaluate(el => {
    const rows = [...el.querySelectorAll('.spx-step-form [data-field]')];
    const boxes = rows.map(row => ({ field: row.dataset.field, x: row.getBoundingClientRect().x, y: row.getBoundingClientRect().y }));
    return { boxes, columns: getComputedStyle(el.querySelector('.spx-step-form')).gridTemplateColumns.split(' ').filter(Boolean).length };
  });
  assert.equal(fieldGeometry.columns, 1, 'step parameters are one per row');
  assert.ok(fieldGeometry.boxes.every(box => Math.abs(box.x - fieldGeometry.boxes[0].x) < 1),
    'every parameter row starts at the same x');
  assert.deepEqual(
    fieldGeometry.boxes.map(box => box.field).slice(0, 2),
    ['name', 'enabled'],
    'name and enabled come first',
  );

  // isotropic + etch: no deposit material, no particle fields, rateMode uniform/material.
  assert.equal(await inspector.locator('[data-field="depositMaterial"]').count(), 0);
  assert.equal(await inspector.locator('[data-field="stickingProbability"]').count(), 0);
  assert.equal(await inspector.locator('[data-field="sourceExponent"]').count(), 0);
  assert.equal(await page.getByLabel('步骤速率模式').isDisabled(), false);
  assert.deepEqual(await page.getByLabel('步骤速率模式').locator('option').evaluateAll(nodes => nodes.map(node => node.value)),
    ['uniform', 'material']);

  // isotropic + deposit: deposit material appears, particle fields stay hidden,
  // rateMode is forced to uniform.
  await page.getByLabel('步骤操作').selectOption('deposit');
  assert.equal(await inspector.locator('[data-field="depositMaterial"]').count(), 1);
  assert.equal(await inspector.locator('[data-field="stickingProbability"]').count(), 0);
  assert.equal(await page.getByLabel('步骤速率模式').isDisabled(), true);
  assert.deepEqual(await page.getByLabel('步骤速率模式').locator('option').evaluateAll(nodes => nodes.map(node => node.value)),
    ['uniform']);
  await page.getByLabel('步骤沉积材料').locator('option[value="SiO2"]').waitFor({ state: 'attached' });
  const depositOptions = await page.getByLabel('步骤沉积材料').locator('option').evaluateAll(nodes => nodes.map(node => node.value));
  for (const material of ['SiO2', 'SiGe', 'PolySi']) {
    assert.ok(depositOptions.includes(material), `deposit materials include ${material}: ${depositOptions.join(',')}`);
  }
  for (const material of ['Mask', 'PMMA']) {
    assert.ok(!depositOptions.includes(material), `deposit materials exclude ${material}: ${depositOptions.join(',')}`);
  }
  assert.equal(await page.getByLabel('步骤沉积材料').inputValue(), 'SiO2',
    'switching to deposit defaults to a depositable material, never Mask');

  // singleParticle: particle fields appear; deposit material still follows operation.
  await page.getByLabel('步骤模型').selectOption('singleParticle');
  assert.equal(await inspector.locator('[data-field="stickingProbability"]').count(), 1);
  assert.equal(await inspector.locator('[data-field="sourceExponent"]').count(), 1);
  assert.equal(await inspector.locator('[data-field="depositMaterial"]').count(), 1);

  // back to etch: deposit material disappears, particle fields stay.
  await page.getByLabel('步骤操作').selectOption('etch');
  assert.equal(await inspector.locator('[data-field="depositMaterial"]').count(), 0);
  assert.equal(await inspector.locator('[data-field="stickingProbability"]').count(), 1);
  await page.screenshot({ path: path.join(shotDir, 'round11-param-gating.png'), fullPage: true });

  // isotropic clears the particle fields (and keeps only uniform for deposit).
  await page.getByLabel('步骤模型').selectOption('isotropic');
  assert.equal(await inspector.locator('[data-field="stickingProbability"]').count(), 0);
  assert.equal(await inspector.locator('[data-field="sourceExponent"]').count(), 0);
  await page.getByLabel('步骤操作').selectOption('deposit');
  await page.getByLabel('步骤沉积材料').selectOption({ index: 1 });
  await page.screenshot({ path: path.join(shotDir, 'round11-02-three-columns.png'), fullPage: true });
  await saveFromFlowBar();

  const paramProjectId = await projectIdByName('Round11 参数');
  const saved = await getJson(`/api/v1/workflow/projects/${paramProjectId}`);
  const savedStep = saved.steps[0];
  assert.equal(savedStep.operation, 'deposit');
  assert.equal(savedStep.model, 'isotropic');
  assert.equal(savedStep.depositMaterial, 'SiO2', 'deposit keeps the selected depositable material');
  assert.equal(savedStep.stickingProbability, undefined, 'isotropic must not carry stickingProbability');
  assert.equal(savedStep.sourceExponent, undefined, 'isotropic must not carry sourceExponent');
  assert.equal(savedStep.rateMode, 'uniform', 'deposit rateMode is always uniform');

  // ---- C. disabled last step: explicit skip + 2-step Route ----------------
  await openProject('Round11 跳过');
  await openZone('02 流程');
  await stepRows().last().click();
  await page.getByLabel('步骤启用').uncheck();
  await compileFlow();
  const skipNote = page.locator('.wf-flow [aria-label="已跳过步骤"]');
  await skipNote.waitFor();
  assert.match(await skipNote.innerText(), /已跳过 1 个未启用步骤：最后一步/);
  await page.screenshot({ path: path.join(shotDir, 'round11-last-step-skipped.png'), fullPage: true });
  await createRoute();
  const routeSummary = page.locator('.wf-run .wf-summary').first();
  assert.match(await routeSummary.innerText(), /2 步/);
  assert.equal(await timelineSteps().count(), 2, 'the timeline has exactly the two enabled steps');
  assert.equal(await page.locator('.wf-structure-timeline [data-kind="initial"]').count(), 1);

  // Re-enable the last step: Compile + Route now cover three steps.
  await openZone('02 流程');
  await stepRows().last().click();
  await page.getByLabel('步骤启用').check();
  await compileFlow();
  assert.equal(await page.locator('.wf-flow [aria-label="已跳过步骤"]').count(), 0, 're-enabled step is no longer skipped');
  await createRoute();
  assert.match(await page.locator('.wf-run .wf-summary').first().innerText(), /3 步/);
  assert.equal(await timelineSteps().count(), 3, 'the timeline covers all three enabled steps');
  assert.equal(await timelineSteps().filter({ hasText: '待执行' }).count(), 3, 'unexecuted steps stay grey, not hidden');

  // ---- C. 5-step split Route: timeline shows initial + all 5 steps --------
  await openProject('Round11 五步');
  await openZone('02 流程');
  await compileFlow();
  await createRoute();
  assert.match(await page.locator('.wf-run .wf-summary').first().innerText(), /5 步/);
  assert.equal(await page.locator('.wf-structure-timeline [data-kind="initial"]').count(), 1);
  assert.equal(await timelineSteps().count(), 5, 'a split Route still lists its last step before it runs');
  const pendingTexts = await timelineSteps().allInnerTexts();
  for (const stepName of ['one', 'two', 'three', 'four', 'five']) {
    assert.ok(pendingTexts.some(text => text.includes(stepName)),
      `pending chip keeps the step name ${stepName}: ${JSON.stringify(pendingTexts)}`);
  }
  assert.equal(pendingTexts.filter(text => /待执行/.test(text)).length, 5, 'every pending chip has a 待执行 badge');
  assert.ok(!pendingTexts.some(text => /^\d+\s*\n?\s*待执行/.test(text)),
    'a chip must not replace the step name with its sequence number');
  const lastChip = timelineSteps().last();
  assert.equal(await lastChip.getAttribute('data-executed'), 'false');
  assert.match(await lastChip.innerText(), /待执行/);
  assert.match(await lastChip.innerText(), /five/);
  await page.screenshot({ path: path.join(shotDir, 'round11-timeline-pending.png'), fullPage: true });

  // A pending chip must not fake a snapshot.
  await lastChip.click();
  await page.locator('.wf-structure-empty').filter({ hasText: '该步骤尚未执行' }).waitFor();
  assert.equal(await page.locator('.wf-structure-canvas').count(), 0, 'pending steps have no structure canvas');

  // Execute both Segments: the last step becomes previewable.
  await page.getByRole('button', { name: '执行全部 Segment', exact: true }).click();
  await page.waitForFunction(
    () => document.querySelectorAll('.wf-structure-timeline [data-kind="step"][data-executed="true"]').length === 5,
    null,
    { timeout: 240000 },
  );
  assert.equal(await timelineSteps().count(), 5);
  assert.equal(await timelineSteps().filter({ hasText: '待执行' }).count(), 0);
  const executedTexts = await timelineSteps().allInnerTexts();
  for (const stepName of ['one', 'two', 'three', 'four', 'five']) {
    assert.ok(executedTexts.some(text => text.includes(stepName) && /成功/.test(text)),
      `executed chip keeps the step name ${stepName} and a 成功 badge: ${JSON.stringify(executedTexts)}`);
  }
  await lastChip.click();
  await page.locator('.wf-structure-canvas').waitFor({ timeout: 60000 });
  assert.match(await page.locator('.wf-structure-head .wf-structure-step').innerText(), /five/);
  // The 2D structure labels are screen-sized: no 30–40px world-unit text.
  const round11Text = await page.locator('.wf-structure-canvas').first().evaluate(svg => {
    const heights = Array.from(svg.querySelectorAll('.wf-tick-label, .wf-axis-title--x, .wf-scale-label'))
      .map(node => node.getBoundingClientRect().height).filter(height => height > 0);
    return { maxHeight: heights.length ? Math.max(...heights) : 0, count: heights.length };
  });
  assert.ok(round11Text.count >= 4 && round11Text.maxHeight > 0 && round11Text.maxHeight <= 14,
    `03 structure labels must stay <= 14px on screen, got ${JSON.stringify(round11Text)}`);
  await page.screenshot({ path: path.join(shotDir, 'round11-timeline-executed.png'), fullPage: true });

  // ---- C fallback: a Compile that drops an enabled step blocks Route ------
  let intercepted = false;
  await openZone('02 流程');
  await page.route('**/api/v1/workflow/flows/from-project', async route => {
    if (intercepted) { await route.continue(); return; }
    intercepted = true;
    const response = await route.fetch();
    const body = await response.json();
    body.compile.sourceMap = (body.compile.sourceMap ?? []).slice(0, -1);
    await route.fulfill({ response, json: body });
  });
  await flowBar().getByRole('button', { name: /重新编译/ }).click();
  const gapAlert = page.locator('.wf-flow [aria-label="编译缺少步骤"]');
  await gapAlert.waitFor({ timeout: 120000 });
  assert.match(await gapAlert.innerText(), /编译结果缺少步骤 five，已阻止创建 Route/);
  assert.match(await page.locator('.wf-error[role="alert"]').innerText(), /编译结果缺少步骤 five/);
  const routeButton = flowBar().getByRole('button', { name: '创建 Route', exact: true });
  assert.equal(await routeButton.isDisabled(), true, '创建 Route is disabled while a step is missing');
  await page.screenshot({ path: path.join(shotDir, 'round11-compile-missing-step.png'), fullPage: true });
  await page.unroute('**/api/v1/workflow/flows/from-project');

  assert.deepEqual(errors, []);
  console.log(JSON.stringify({
    status: 'passed',
    baseUrl,
    timeline: { fiveStepPending: 5, fiveStepExecuted: 5, skippedThreeStepRoute: 2 },
    screenshots: [
      'round11-02-three-columns.png',
      'round11-param-gating.png',
      'round11-last-step-skipped.png',
      'round11-timeline-pending.png',
      'round11-timeline-executed.png',
      'round11-compile-missing-step.png',
    ],
  }));
} finally {
  await browser.close();
}
