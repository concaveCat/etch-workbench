/**
 * Round-8 review regression (stale false positives, compare picks, pending note).
 *
 *  A. Compile an unchanged project, save it without edits: the server-normalized
 *     document must still match its Compile, so 02 keeps "已编译"/"创建 Route"
 *     and 03 can run. (Solver defaults used to break the fingerprint.)
 *  B. A branch Route restored from its base Compile id must not be stale: the
 *     head attempt's derived Compile is what 03 runs, so "去 03" is offered and
 *     the 0.04 tail branch can execute. A real project edit still goes stale
 *     (covered by round 7).
 *  C. A module-definition Compile restores as fresh even though its expanded
 *     steps are not in project.steps; a Route can be created and executed.
 *  D/E. `comparePick` distinguishes "not chosen" (null → first four) from an
 *     explicit empty selection: unchecking all disables compare with a hint and
 *     sends no request, and the pending explanation appears even with ≤4
 *     candidates.
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
const authoredDuration = (document, authoredId) => {
  // POST /flows/... returns a CompileInfo ({compile: {project, sourceMap}});
  // GET /compiles/{id} returns the bare envelope ({project, sourceMap}).
  const compile = document.compile ?? document;
  const entry = compile.sourceMap.find(item => item.authoredStepId === authoredId);
  assert.ok(entry, `sourceMap must contain ${authoredId}`);
  const step = compile.project.steps.find(item => item.id === entry.stepId);
  return Number(step.duration);
};

const fixture = JSON.parse(readFileSync(projectPath, 'utf8'));
const managedNames = new Set(['Round8 未改动保存', 'Round8 分支恢复', 'Round8 定义编译', 'Round8 四候选']);
for (const row of (await getJson('/api/v1/workflow/projects')).projects ?? []) {
  if (managedNames.has(row.name)) {
    await fetch(`${baseUrl}/api/v1/workflow/projects/${row.projectId}`, { method: 'DELETE' });
  }
}

// --- A: unchanged project imported from the raw fixture, compiled, saved ----
const projectA = { ...fixture, name: 'Round8 未改动保存' };

// --- B: base Route + executed prefix + 0.04 tail branch ---------------------
const projectB = { ...fixture, name: 'Round8 分支恢复' };
const recordB = await postJson('/api/v1/workflow/projects', projectB);
const compiledBase = await postJson('/api/v1/workflow/flows/from-project', { project: projectB });
const stepsBase = compiledBase.compile.project.steps.filter(step => step.enabled);
const splitAfterStepId = stepsBase[stepsBase.length - 2].id;
const tailCompiledId = stepsBase[stepsBase.length - 1].id;
const tailAuthoredId = projectB.steps[projectB.steps.length - 1].id;
const routeB = await postJson('/api/v1/workflow/routes/from-compile', {
  compileId: compiledBase.compileId,
  name: 'Round8 branch route',
  splitAfterStepId,
});
const prefixSegment = routeB.plan.segments[0];
await postJson(`/api/v1/workflow/segments/${prefixSegment.segmentId}/execute`, {});
const prefixDone = await waitSegment(prefixSegment.segmentId);
assert.equal(prefixDone.status, 'succeeded', JSON.stringify(prefixDone));
const branchProject = {
  ...projectB,
  steps: projectB.steps.map(step => (step.id === tailAuthoredId ? { ...step, duration: 0.04 } : step)),
};
const derived = await postJson('/api/v1/workflow/flows/from-project', {
  project: branchProject, flowId: compiledBase.flowId,
});
assert.equal(authoredDuration(derived, tailAuthoredId), 0.04, 'the branch Compile must carry the 0.04 tail');
const branched = await postJson(`/api/v1/workflow/routes/${routeB.plan.route.routeId}/branches`, {
  compileId: derived.compileId, startAfterStepId: splitAfterStepId, changedStepIds: [tailCompiledId],
});
const branchAttempt = branched.plan.attempts[branched.plan.attempts.length - 1];
const branchSegment = branched.plan.segments.find(item => item.attemptId === branchAttempt.attemptId);
assert.ok(branchSegment, 'the branch must append a planned tail Segment');
assert.equal(branchSegment.sourceCompileId, derived.compileId);
// Reproduce the reported state: the pointer still names the base Compile.
await putJson(`/api/v1/workflow/projects/${recordB.projectId}/state`, {
  schemaVersion: 1,
  compileId: compiledBase.compileId,
  compileBasis: executionCompileBasis(projectB),
  routeId: routeB.plan.route.routeId,
  segmentRuns: [{ segmentId: prefixSegment.segmentId, runId: prefixDone.runId, measurementId: null }],
  measurementIds: [],
  experimentIds: [],
});

// --- C: module-definition Compile whose steps are not in project.steps ------
const suffix = Date.now().toString(36);
const moduleId = `mod_round8_${suffix}`;
await postJson('/api/v1/workflow/modules', {
  schemaVersion: 1,
  moduleId,
  name: 'round8 etch',
  description: '',
  parameters: [{ name: 'duration', default: 0.02, unit: 's' }],
  steps: [{
    id: 'etch_round8', name: 'etch', enabled: true, operation: 'etch', model: 'isotropic',
    rateMagnitude: 0.1, duration: '${duration}', rateMode: 'uniform',
  }],
});
const definitionId = `def_round8_${suffix}`;
const definitionFlowId = `flow_round8_${suffix}`;
await postJson('/api/v1/workflow/flows/definitions', {
  schemaVersion: 2,
  definitionId,
  flowId: definitionFlowId,
  revision: 1,
  name: 'round8 definition',
  description: '',
  variables: [],
  nodes: [{ kind: 'module-call', instanceId: 'call_one', moduleId, parameters: { duration: 0.02 } }],
  solver: fixture.solver,
});
const projectC = { ...fixture, name: 'Round8 定义编译' };
const recordC = await postJson('/api/v1/workflow/projects', projectC);
const compiledC = await postJson(`/api/v1/workflow/flows/definitions/${definitionId}/compile`, { project: projectC });
const localStepIds = new Set(projectC.steps.map(step => step.id));
assert.ok(compiledC.compile.sourceMap.some(entry => !localStepIds.has(entry.authoredStepId)),
  'the definition Compile must expand steps outside project.steps');
await putJson(`/api/v1/workflow/projects/${recordC.projectId}/state`, {
  schemaVersion: 1, compileId: compiledC.compileId,
  compileBasis: executionCompileBasis(projectC), routeId: null,
  segmentRuns: [], measurementIds: [], experimentIds: [],
});

// --- D/E: four-variant experiment for the compare checkboxes ---------------
const metricId = `metric_round8_${suffix}`;
await postJson('/api/v1/workflow/metrics', {
  schemaVersion: 1, metricId, name: 'round8 depth', unit: 'um',
});
const recipe = await postJson('/api/v1/workflow/metrology-recipes', {
  schemaVersion: 1, recipeId: `recipe_round8_${suffix}`, version: 1,
  metricId, name: 'round8 depth', dimension: 2,
  method: 'etch_depth',
  parameters: { kind: 'etch_depth', x: 1.0, referenceHeight: 0 },
  material: 'Si',
});
const projectD = { ...fixture, name: 'Round8 四候选' };
const compiledD = await postJson('/api/v1/workflow/flows/from-project', { project: projectD });
const stepsD = compiledD.compile.project.steps.filter(step => step.enabled);
const routeD = await postJson('/api/v1/workflow/routes/from-compile', {
  compileId: compiledD.compileId, name: 'Round8 D Route', splitAfterStepId: stepsD[0].id,
});
const experimentD = await postJson('/api/v1/workflow/experiments', {
  name: 'Round8 四候选实验',
  project: projectD,
  flowId: compiledD.flowId,
  compileId: compiledD.compileId,
  recipeId: recipe.recipeId,
  recipeVersion: recipe.version,
  variants: Array.from({ length: 4 }, (_, index) => ({
    parameters: { [stepsD[0].id]: { duration: 0.01 + index * 0.001 } },
    replicates: 1,
    seed: 800 + index,
  })),
  maxAttempts: 1,
  costPerRunUsd: 0,
});
await postJson(`/api/v1/workflow/experiments/${experimentD.experimentId}/run`, {});
const finishedD = await waitExperiment(experimentD.experimentId);
assert.equal(finishedD.status, 'completed', 'four variants must run');
const measurementD = finishedD.variants[0].replicates[0].measurementId;
assert.ok(measurementD, 'the experiment must produce a measurement');
const recordD = await postJson('/api/v1/workflow/projects', projectD);
await putJson(`/api/v1/workflow/projects/${recordD.projectId}/state`, {
  schemaVersion: 1, compileId: compiledD.compileId,
  compileBasis: executionCompileBasis(projectD), routeId: routeD.plan.route.routeId,
  segmentRuns: [], measurementIds: [measurementD], experimentIds: [experimentD.experimentId],
});

// --- Browser ----------------------------------------------------------------
const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const page = await browser.newPage({ viewport: { width: 1600, height: 1400 } });
page.setDefaultTimeout(120000);
const errors = [];
const compareRequests = [];
page.on('pageerror', error => errors.push(error.message));
page.on('request', request => {
  if (request.method() === 'POST' && request.url().includes('/api/v1/workflow/analysis/compare')) {
    compareRequests.push(JSON.parse(request.postData() ?? '{}'));
  }
});
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
  // A previous test run may have left a project id in localStorage; the mount
  // effect would auto-open it and can land in the middle of this test.
  await page.addInitScript(() => {
    try { window.localStorage.removeItem('viennaps.workflow.projectId'); } catch { /* ignore */ }
  });
  await page.goto(`${baseUrl}/`);

  // ---- A: an unchanged save keeps the Compile fresh ----------------------
  // Import the raw fixture (no solver defaults) so the save path is the one
  // that used to drop the server-normalized document.
  await page.locator('input[type=file][accept=".json"]').setInputFiles({
    name: 'round8-unchanged.json',
    mimeType: 'application/json',
    buffer: Buffer.from(JSON.stringify(projectA)),
  });
  await page.locator('.spx-tree-root').filter({ hasText: 'Round8 未改动保存' }).waitFor({ timeout: 30000 });
  await openZone('02 流程');
  const flowBar = page.locator('.spx-flow-bar');
  await flowBar.getByRole('button', { name: '编译流程', exact: true }).click();
  await page.locator('.spx-compile-badge').filter({ hasText: '已编译' }).waitFor({ timeout: 120000 });
  await page.waitForTimeout(500);
  const saveResponse = page.waitForResponse(response => {
    const method = response.request().method();
    return (method === 'PUT' || method === 'POST')
      && /^\/api\/v1\/workflow\/projects\/?[^/]*$/.test(new URL(response.url()).pathname);
  });
  await flowBar.getByRole('button', { name: '保存工程', exact: true }).click();
  assert.ok((await saveResponse).ok(), 'the project save must succeed');
  await page.waitForTimeout(1500);
  assert.match(await page.locator('.spx-compile-badge').innerText(), /已编译/,
    'saving an unchanged project must not invalidate its Compile');
  assert.equal(await page.locator('.spx-compile-badge').filter({ hasText: '需重新编译' }).count(), 0);
  assert.equal(await flowBar.getByRole('button', { name: '创建 Route', exact: true }).count(), 1,
    'a fresh Compile still offers 创建 Route after save');
  await page.screenshot({ path: path.join(shotDir, 'round8-02-save-keeps-compile.png'), fullPage: true });
  await flowBar.getByRole('button', { name: '创建 Route', exact: true }).click();
  await page.locator('.wf-summary').filter({ hasText: 'Route 已创建' }).waitFor({ timeout: 60000 });
  await openZone('03 运行');
  assert.equal(await page.locator('[aria-label="执行禁用原因"]').count(), 0);
  assert.equal(await page.getByRole('button', { name: '执行当前 Segment', exact: true }).isEnabled(), true,
    'the saved unchanged project must be runnable');

  // ---- B: a restored tail branch is not stale ----------------------------
  await openProject('Round8 分支恢复');
  await openZone('02 流程');
  await page.locator('.spx-compile-badge').filter({ hasText: '已编译' }).waitFor({ timeout: 60000 });
  assert.equal(await page.locator('.spx-compile-badge').filter({ hasText: '需重新编译' }).count(), 0,
    'a legitimate branch Compile must not be called stale');
  assert.equal(await flowBar.getByRole('button', { name: '去 03', exact: true }).count(), 1,
    'a branch Route must offer 去 03');
  assert.equal(await flowBar.locator('[aria-label="Route 需重建"]').count(), 0);
  await page.screenshot({ path: path.join(shotDir, 'round8-02-branch-restored.png'), fullPage: true });

  await openZone('03 运行');
  assert.equal(await page.locator('[aria-label="执行禁用原因"]').count(), 0,
    'a restored branch Route must not be blocked');
  assert.match(await page.locator('.wf-status-row').innerText(), /第 \d+ 段 · 待执行/,
    '03 must resume at the planned branch Segment');
  const runBranch = page.getByRole('button', { name: '执行当前 Segment', exact: true });
  assert.equal(await runBranch.isEnabled(), true, 'the branch tail must be executable');
  await runBranch.click();
  const finishedBranch = await waitSegment(branchSegment.segmentId);
  assert.equal(finishedBranch.status, 'succeeded', JSON.stringify(finishedBranch));
  await page.waitForTimeout(800);
  const branchPlan = await getJson(`/api/v1/workflow/routes/${routeB.plan.route.routeId}`);
  const executedBranch = branchPlan.plan.segments.find(item => item.segmentId === branchSegment.segmentId);
  assert.equal(executedBranch.status, 'succeeded', JSON.stringify(executedBranch));
  assert.equal(executedBranch.sourceCompileId, derived.compileId,
    'the branch must execute its derived Compile (0.04 tail)');
  await page.waitForTimeout(1200);
  let stateB = await getJson(`/api/v1/workflow/projects/${recordB.projectId}/state`);
  for (let attempt = 0; attempt < 40 && stateB.compileId !== derived.compileId; attempt += 1) {
    await page.waitForTimeout(250);
    stateB = await getJson(`/api/v1/workflow/projects/${recordB.projectId}/state`);
  }
  assert.equal(stateB.compileId, derived.compileId, 'the restored state must point at the branch Compile');
  assert.equal(authoredDuration(await getJson(`/api/v1/workflow/compiles/${stateB.compileId}`), tailAuthoredId), 0.04);
  await page.screenshot({ path: path.join(shotDir, 'round8-03-branch-executed.png'), fullPage: true });

  // ---- C: a definition Compile restores fresh and can route/run ----------
  await openProject('Round8 定义编译');
  await openZone('02 流程');
  await page.locator('.spx-compile-badge').filter({ hasText: '已编译' }).waitFor({ timeout: 60000 });
  assert.equal(await page.locator('.spx-compile-badge').filter({ hasText: '需重新编译' }).count(), 0,
    'a definition Compile must not be judged by project.steps');
  assert.equal(await flowBar.getByRole('button', { name: '创建 Route', exact: true }).count(), 1);
  await page.screenshot({ path: path.join(shotDir, 'round8-02-definition-restored.png'), fullPage: true });
  await flowBar.getByRole('button', { name: '创建 Route', exact: true }).click();
  await page.locator('.wf-summary').filter({ hasText: 'Route 已创建' }).waitFor({ timeout: 60000 });
  await openZone('03 运行');
  const runAll = page.getByRole('button', { name: '执行全部 Segment', exact: true });
  assert.equal(await runAll.isEnabled(), true, 'a definition Route must be executable');
  await runAll.click();
  await page.locator('.wf-status.succeeded').first().waitFor({ timeout: 180000 });

  // ---- D/E: compare picks and the pending explanation with 4 candidates --
  await openProject('Round8 四候选');
  await openZone('05 分析');
  await page.locator('[aria-label="候选表"]').waitFor({ timeout: 180000 });
  await page.waitForFunction(
    () => document.querySelectorAll('.wf-compare-grid figure canvas, .wf-compare-grid figure svg').length >= 4,
    { timeout: 180000 },
  );
  const boxes = page.locator('[aria-label="候选表"] tbody tr input[type=checkbox]');
  const checkedCount = () => page.locator('[aria-label="候选表"] tbody tr input[type=checkbox]:checked').count();
  assert.equal(await checkedCount(), 4, 'the first four candidates are checked by default');
  const requestsBefore = compareRequests.length;

  // E: unchecking one of four shows the pending explanation.
  await boxes.nth(3).uncheck();
  await page.locator('[aria-label="对比候选说明"]').waitFor({ timeout: 30000 });
  const pendingText = await page.locator('[aria-label="对比候选说明"]').innerText();
  assert.match(pendingText, /已修改对比选择/, pendingText);
  assert.match(pendingText, /比较并加载视图/);
  await page.screenshot({ path: path.join(shotDir, 'round8-05-compare-pending.png'), fullPage: true });
  await page.getByRole('button', { name: '比较并加载视图', exact: true }).click();
  await page.waitForFunction(
    () => !document.querySelector('[aria-label="对比候选说明"]'),
    { timeout: 60000 },
  );
  assert.equal(compareRequests.length, requestsBefore + 1);
  assert.equal(compareRequests[compareRequests.length - 1].candidates.length, 3,
    'the comparison must use the three checked candidates');

  // D: unchecking all four selects none, disables compare and sends nothing.
  const requestsAfterCompare = compareRequests.length;
  await boxes.nth(0).uncheck();
  await boxes.nth(1).uncheck();
  await boxes.nth(2).uncheck();
  assert.equal(await checkedCount(), 0, 'an explicit empty selection must stay empty');
  assert.equal(await page.getByRole('button', { name: '比较并加载视图', exact: true }).isDisabled(), true,
    'compare must be disabled with fewer than two candidates selected');
  await page.locator('[aria-label="比较禁用原因"]').waitFor({ timeout: 30000 });
  assert.match(await page.locator('[aria-label="比较禁用原因"]').innerText(), /至少选择 2 个候选/);
  assert.equal(compareRequests.length, requestsAfterCompare, 'an empty selection must not send a compare request');
  await page.screenshot({ path: path.join(shotDir, 'round8-05-compare-none.png'), fullPage: true });

  // Pick two again: compare becomes available and the table has one pair.
  await boxes.nth(0).check();
  await boxes.nth(1).check();
  assert.equal(await checkedCount(), 2);
  const compareButton = page.getByRole('button', { name: '比较并加载视图', exact: true });
  assert.equal(await compareButton.isEnabled(), true);
  await compareButton.click();
  await page.waitForFunction(
    () => document.querySelectorAll('[aria-label="候选对比表"] tbody tr').length === 1,
    { timeout: 120000 },
  );
  assert.equal(await page.locator('[aria-label="候选对比表"] tbody tr').count(), 1,
    'two candidates produce one pairwise row');
  assert.equal(compareRequests[compareRequests.length - 1].candidates.length, 2);
  await page.screenshot({ path: path.join(shotDir, 'round8-05-compare-two.png'), fullPage: true });

  assert.deepEqual(errors, [], `page errors: ${JSON.stringify(errors)}`);
  console.log(JSON.stringify({
    event: 'round8_pass',
    unchangedProject: projectA.name,
    branch: { routeId: routeB.plan.route.routeId, baseCompileId: compiledBase.compileId, branchCompileId: derived.compileId, tail: 0.04 },
    definition: { definitionId, compileId: compiledC.compileId, stepCount: compiledC.stepCount },
    compare: { total: 4, pendingShown: true, emptySelectionDisabled: true, requests: compareRequests.length },
    screenshots: shotDir,
  }));
} finally {
  await browser.close();
}
