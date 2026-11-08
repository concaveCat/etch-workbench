/**
 * Round-7 review regression (stale Compile, recipe domain, compare picks, seed 0).
 *
 *  A. Restoring a saved Compile whose project was edited afterwards marks it
 *     stale: 02 hides "去 03", the primary action is "重新编译", the old Route
 *     must be rebuilt, and 03 refuses to run. Recompiling routes/executes the
 *     new duration.
 *  B. The recipe X domain is [-extent/2, +extent/2]: X=-1 measures, X=3 is
 *     rejected with the real range in 03 and 04.
 *  C. 05 candidate checkboxes follow one comparePick state: with 5 candidates,
 *     unchecking V1/V2 leaves V3/V4 and enables V5; checking V5 gives 3.
 *  D. A new project created with seed 0 saves and reopens with
 *     solver.random.seed === 0.
 */
import assert from 'node:assert/strict';
import { mkdirSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';
import { executionCompileBasis } from '../src/workflow/fingerprint.ts';

const here = path.dirname(fileURLToPath(import.meta.url));
const baseUrl = (process.env.WORKBENCH_URL ?? 'http://127.0.0.1:8991').replace(/\/$/, '');
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
    await new Promise(resolve => setTimeout(resolve, 500));
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
const authoredDuration = (compile, authoredId) => {
  const entry = compile.sourceMap.find(item => item.authoredStepId === authoredId);
  const step = compile.project.steps.find(item => item.id === entry.stepId);
  return Number(step.duration);
};

const fixture = JSON.parse(readFileSync(projectPath, 'utf8'));
const managedNames = new Set(['Round7 陈旧编译', 'Round7 五候选', 'Round7 seed零']);
for (const row of (await getJson('/api/v1/workflow/projects')).projects ?? []) {
  if (managedNames.has(row.name)) {
    await fetch(`${baseUrl}/api/v1/workflow/projects/${row.projectId}`, { method: 'DELETE' });
  }
}

// --- Project A: compile/route first, then save a different duration ---------
const staleProject = {
  ...fixture,
  name: 'Round7 陈旧编译',
  steps: fixture.steps.map((step, index) => (index === 0 ? { ...step, duration: 0.02 } : step)),
};
const recordA = await postJson('/api/v1/workflow/projects', staleProject);
const compiledA = await postJson('/api/v1/workflow/flows/from-project', { project: staleProject });
const stepsA = compiledA.compile.project.steps.filter(step => step.enabled);
const routeA = await postJson('/api/v1/workflow/routes/from-compile', {
  compileId: compiledA.compileId,
  name: 'Round7 stale Route',
  splitAfterStepId: stepsA[stepsA.length - 2].id,
});
await putJson(`/api/v1/workflow/projects/${recordA.projectId}/state`, {
  schemaVersion: 1,
  compileId: compiledA.compileId,
  compileBasis: executionCompileBasis(staleProject),
  routeId: routeA.plan.route.routeId,
  segmentRuns: [],
  measurementIds: [],
  experimentIds: [],
});
const editedProject = {
  ...staleProject,
  steps: staleProject.steps.map((step, index) => (index === 0 ? { ...step, duration: 0.2 } : step)),
};
await putJson(`/api/v1/workflow/projects/${recordA.projectId}`, editedProject);

// --- Shared recipe + project B: a completed five-variant experiment ---------
const suffix = Date.now().toString(36);
await postJson('/api/v1/workflow/metrics', {
  schemaVersion: 1, metricId: `metric_round7_${suffix}`, name: 'round7 depth', unit: 'um',
});
const recipe = await postJson('/api/v1/workflow/metrology-recipes', {
  schemaVersion: 1, recipeId: `recipe_round7_${suffix}`, version: 1,
  metricId: `metric_round7_${suffix}`, name: 'round7 depth', dimension: 2,
  method: 'etch_depth',
  parameters: { kind: 'etch_depth', x: 1.0, referenceHeight: 0 },
  material: 'Si',
});
const projectB = { ...fixture, name: 'Round7 五候选' };
const compiledB = await postJson('/api/v1/workflow/flows/from-project', { project: projectB });
const stepsB = compiledB.compile.project.steps.filter(step => step.enabled);
const routeB = await postJson('/api/v1/workflow/routes/from-compile', {
  compileId: compiledB.compileId, name: 'Round7 B Route', splitAfterStepId: stepsB[0].id,
});
const experimentB = await postJson('/api/v1/workflow/experiments', {
  name: 'Round7 五候选实验',
  project: projectB,
  flowId: compiledB.flowId,
  compileId: compiledB.compileId,
  recipeId: recipe.recipeId,
  recipeVersion: recipe.version,
  variants: Array.from({ length: 5 }, (_, index) => ({
    parameters: { [stepsB[0].id]: { duration: 0.01 + index * 0.001 } },
    replicates: 1,
    seed: 700 + index,
  })),
  maxAttempts: 1,
  costPerRunUsd: 0,
});
await postJson(`/api/v1/workflow/experiments/${experimentB.experimentId}/run`, {});
const finishedB = await waitExperiment(experimentB.experimentId);
assert.equal(finishedB.status, 'completed', 'five variants must run');
const measurementB = finishedB.variants[0].replicates[0].measurementId;
assert.ok(measurementB, 'the experiment must produce a measurement');
const recordB = await postJson('/api/v1/workflow/projects', projectB);
await putJson(`/api/v1/workflow/projects/${recordB.projectId}/state`, {
  schemaVersion: 1, compileId: compiledB.compileId,
  compileBasis: executionCompileBasis(projectB), routeId: routeB.plan.route.routeId,
  segmentRuns: [], measurementIds: [measurementB], experimentIds: [experimentB.experimentId],
});

// --- Browser ----------------------------------------------------------------
const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const page = await browser.newPage({ viewport: { width: 1600, height: 1400 } });
page.setDefaultTimeout(120000);
const errors = [];
page.on('pageerror', error => errors.push(error.message));
page.on('dialog', async dialog => { await dialog.accept(); });

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

try {
  await page.goto(`${baseUrl}/`);

  // ---- A: stale Compile after a saved duration edit -----------------------
  await openProject('Round7 陈旧编译');
  await openZone('02 流程');
  await page.waitForFunction(() => {
    const button = [...document.querySelectorAll('.spx-flow-bar button')]
      .find(item => item.textContent?.trim() === '重新编译');
    return !!button && !button.disabled;
  }, null, { timeout: 60000 });
  await page.locator('.spx-compile-badge').filter({ hasText: '需重新编译' }).waitFor({ timeout: 30000 });
  const flowBar = page.locator('.spx-flow-bar');
  assert.equal(await flowBar.getByRole('button', { name: '去 03', exact: true }).count(), 0,
    'a stale Compile must not offer 去 03 as the primary path');
  assert.equal(await flowBar.getByRole('button', { name: '创建 Route', exact: true }).count(), 0,
    'a stale Compile must not offer 创建 Route');
  assert.equal(await flowBar.getByRole('button', { name: '重新编译', exact: true }).count(), 1,
    '重新编译 must be the primary action');
  assert.equal(await flowBar.getByRole('button', { name: '重新编译', exact: true }).isEnabled(), true,
    '重新编译 must be usable immediately');
  assert.equal(await flowBar.getByRole('button', { name: '重建 Route…', exact: true }).isDisabled(), true,
    'the old Route cannot be rebuilt before recompiling');
  assert.match(await page.locator('[aria-label="Route 需重建"]').innerText(), /工程在编译后有修改，需重新编译；旧 Route 需重建/);
  await page.screenshot({ path: path.join(shotDir, 'round7-02-stale-compile.png'), fullPage: true });

  await openZone('03 运行');
  await page.locator('[aria-label="执行禁用原因"]').waitFor({ timeout: 30000 });
  assert.match(await page.locator('[aria-label="执行禁用原因"]').innerText(), /编译已过期，先重新编译并重建 Route/);
  assert.equal(await page.getByRole('button', { name: '执行全部 Segment', exact: true }).isDisabled(), true,
    'a stale Compile must not execute the old Route');

  await openZone('02 流程');
  await flowBar.getByRole('button', { name: '重新编译', exact: true }).click();
  await page.locator('.spx-compile-badge').filter({ hasText: '已编译' }).waitFor({ timeout: 120000 });
  await page.waitForTimeout(1200);
  const stateA = await getJson(`/api/v1/workflow/projects/${recordA.projectId}/state`);
  const recompiledA = await getJson(`/api/v1/workflow/compiles/${stateA.compileId}`);
  assert.equal(authoredDuration(recompiledA, 'p1_step_01'), 0.2,
    'recompiling must pick up the edited duration');

  await flowBar.getByRole('button', { name: '创建 Route', exact: true }).click();
  await page.locator('.wf-summary').filter({ hasText: 'Route 已创建' }).waitFor({ timeout: 60000 });
  await page.getByRole('button', { name: '执行全部 Segment', exact: true }).click();
  await page.waitForTimeout(1200);
  const stateAfterRoute = await getJson(`/api/v1/workflow/projects/${recordA.projectId}/state`);
  assert.ok(stateAfterRoute.routeId, 'the new Route must be persisted');
  const planA = await getJson(`/api/v1/workflow/routes/${stateAfterRoute.routeId}`);
  for (const segment of planA.plan.segments) {
    const done = await waitSegment(segment.segmentId);
    assert.equal(done.status, 'succeeded', JSON.stringify(done));
  }
  await page.locator('.wf-status.succeeded').first().waitFor({ timeout: 180000 });

  // ---- B: recipe X uses [-xExtent/2, +xExtent/2] --------------------------
  await openZone('03 运行');
  const xInput = page.getByLabel('深度 X');
  await xInput.fill('-1');
  assert.equal(await page.locator('[aria-label="配方 X 校验错误"]').count(), 0,
    'X=-1 must be inside the -2–2 µm domain');
  const measureButton = page.getByRole('button', { name: '测量', exact: true });
  assert.equal(await measureButton.isDisabled(), false, 'X=-1 must allow measuring');
  await measureButton.click();
  await page.getByText(/绑定 \(binding\)：已绑定/).first().waitFor({ timeout: 120000 });
  await page.screenshot({ path: path.join(shotDir, 'round7-03-x-negative.png'), fullPage: true });

  await xInput.fill('3');
  await page.locator('[aria-label="配方 X 校验错误"]').waitFor({ timeout: 30000 });
  assert.match(await page.locator('[aria-label="配方 X 校验错误"]').innerText(), /X 超出计算域（-2–2 µm）/);
  assert.equal(await measureButton.isDisabled(), true, 'X=3 must be rejected');
  await page.screenshot({ path: path.join(shotDir, 'round7-03-x-outside.png'), fullPage: true });

  await openZone('04 实验');
  await page.locator('[aria-label="实验空态"]').waitFor({ timeout: 30000 });
  assert.match(await page.locator('[aria-label="实验空态"]').innerText(), /配方 X 超出计算域（-2–2 µm）/,
    '04 must validate against the same centred domain as 03');

  // ---- C: 5 candidates, one comparePick truth -----------------------------
  await openProject('Round7 五候选');
  await openZone('05 分析');
  await page.locator('[aria-label="候选表"]').waitFor({ timeout: 180000 });
  await page.waitForFunction(() => document.querySelectorAll('.wf-compare-grid figure canvas, .wf-compare-grid figure svg').length >= 5,
    { timeout: 180000 });
  assert.equal(await page.locator('[aria-label="候选表"] tbody tr').count(), 5);
  const boxes = page.locator('[aria-label="候选表"] tbody tr input[type=checkbox]');
  const checkedCount = () => page.locator('[aria-label="候选表"] tbody tr input[type=checkbox]:checked').count();
  assert.equal(await checkedCount(), 4, 'the first four candidates are checked by default');

  await boxes.nth(0).uncheck();
  await boxes.nth(1).uncheck();
  assert.equal(await checkedCount(), 2, 'unchecking V1 and V2 must leave exactly V3/V4 checked');
  assert.equal(await boxes.nth(2).isChecked(), true);
  assert.equal(await boxes.nth(3).isChecked(), true);
  assert.equal(await boxes.nth(4).isDisabled(), false, 'V5 must become selectable after two removals');
  await boxes.nth(4).check();
  assert.equal(await checkedCount(), 3, 'V5 joins the compare table');
  assert.equal(await boxes.nth(4).isChecked(), true);
  await page.screenshot({ path: path.join(shotDir, 'round7-05-compare-picks.png'), fullPage: true });

  // ---- D: seed 0 survives create → save → reload -------------------------
  await closeToStart();
  await page.locator('.spx-command').first().click();
  if (await page.locator('.spx-rebuild').filter({ hasText: '未保存更改' }).count() > 0) {
    await page.getByRole('button', { name: '确认新建', exact: true }).click();
  }
  await page.getByLabel('新工程名称').fill('Round7 seed零');
  await page.getByLabel('新工程 seed').fill('0');
  await page.getByRole('button', { name: '创建工程草稿', exact: true }).click();
  await page.locator('.spx-save-button').click();
  await page.locator('.spx-save-button').filter({ hasText: '已保存' }).waitFor({ timeout: 60000 });
  const seedProjectId = await page.evaluate(() => window.localStorage.getItem('viennaps.workflow.projectId'));
  assert.ok(seedProjectId, 'the new project must be saved');
  const savedSeedProject = await getJson(`/api/v1/workflow/projects/${seedProjectId}`);
  assert.equal(savedSeedProject.solver.random.seed, 0, 'seed 0 must not be replaced by 7');

  await page.reload();
  await page.getByText('Etch Workbench').first().waitFor();
  await openZone('01 工程');
  await page.getByLabel('工程 seed').waitFor({ timeout: 30000 });
  assert.equal(await page.getByLabel('工程 seed').inputValue(), '0', 'the reopened project keeps seed 0');
  const reopenedSeedProject = await getJson(`/api/v1/workflow/projects/${seedProjectId}`);
  assert.equal(reopenedSeedProject.solver.random.seed, 0);
  await page.screenshot({ path: path.join(shotDir, 'round7-01-seed-zero.png'), fullPage: true });

  assert.deepEqual(errors, [], `page errors: ${JSON.stringify(errors)}`);
  console.log(JSON.stringify({
    event: 'round7_pass',
    staleProjectId: recordA.projectId,
    staleCompileId: compiledA.compileId,
    recompiledDuration: authoredDuration(recompiledA, 'p1_step_01'),
    recipeDomain: [-2, 2],
    candidates: { total: 5, afterUncheck: 2, afterPickV5: 3 },
    seedProjectId,
    seed: reopenedSeedProject.solver.random.seed,
    screenshots: shotDir,
  }));
} finally {
  await browser.close();
}
