/**
 * Round-9 review regression: staleness comes from an explicit compile basis.
 *
 * The state now stores `compileBasis` — the execution fingerprint of the
 * authored project at compile time — instead of guessing from the Compile's
 * expanded steps or from `compile.project`. These probes cover the seven
 * scenarios the heuristic got wrong:
 *
 *  1. A single-step project compiled at 0.01, then its step deleted and a new
 *     step (0.2) added and saved: stale, the old Compile cannot run.
 *  2. A definition Compile at xExtent=4, then the domain saved as 8: stale.
 *  3. A restored 0.04 branch, saved unchanged: NOT stale, the branch runs 0.04.
 *  4. A restored branch, its Route rebuilt from the derived Compile, reloaded:
 *     NOT stale and executable.
 *  5. An empty-steps project with a definition Compile and a Route: NOT stale.
 *  6. A saved duration edit (0.02 → 0.2): stale.
 *  7. A plain compile + unchanged save + reload: NOT stale, runnable.
 */
import assert from 'node:assert/strict';
import { mkdirSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

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

const waitSegment = async segmentId => {
  for (let index = 0; index < 600; index += 1) {
    const segment = await getJson(`/api/v1/workflow/segments/${segmentId}`);
    if (['succeeded', 'failed', 'cancelled', 'interrupted'].includes(segment.status)) return segment;
    await new Promise(resolve => setTimeout(resolve, 500));
  }
  throw new Error(`segment ${segmentId} did not finish`);
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
const managedNames = new Set([
  'Round9 单步替换', 'Round9 定义改域', 'Round9 分支保存', 'Round9 分支重建',
  'Round9 空步骤定义', 'Round9 时长修改', 'Round9 原样保存',
]);
for (const row of (await getJson('/api/v1/workflow/projects')).projects ?? []) {
  if (managedNames.has(row.name)) {
    await fetch(`${baseUrl}/api/v1/workflow/projects/${row.projectId}`, { method: 'DELETE' });
  }
}

// One authored step, compiled at 0.01; the UI will replace it with a new id.
const singleStepProject = {
  ...fixture,
  name: 'Round9 单步替换',
  steps: [{ ...fixture.steps[0], duration: 0.01 }],
};
const geometryProject = { ...fixture, name: 'Round9 定义改域' };
const branchSaveProject = { ...fixture, name: 'Round9 分支保存' };
const branchRebuildProject = { ...fixture, name: 'Round9 分支重建' };
const emptyStepsProject = { ...fixture, name: 'Round9 空步骤定义', steps: [] };
const durationProject = {
  ...fixture,
  name: 'Round9 时长修改',
  steps: fixture.steps.map((step, index) => (index === 0 ? { ...step, duration: 0.02 } : step)),
};
const unchangedProject = { ...fixture, name: 'Round9 原样保存' };

const projectIds = {};
for (const document of [
  singleStepProject, geometryProject, branchSaveProject, branchRebuildProject,
  emptyStepsProject, durationProject, unchangedProject,
]) {
  const record = await postJson('/api/v1/workflow/projects', document);
  projectIds[document.name] = record.projectId;
}

// A one-step module/definition shared by the definition-Compile probes.
const suffix = Date.now().toString(36);
const moduleId = `mod_round9_${suffix}`;
await postJson('/api/v1/workflow/modules', {
  schemaVersion: 1,
  moduleId,
  name: 'round9 etch',
  description: '',
  parameters: [{ name: 'duration', default: 0.02, unit: 's' }],
  steps: [{
    id: 'etch_round9', name: 'etch', enabled: true, operation: 'etch', model: 'isotropic',
    rateMagnitude: 0.1, duration: '${duration}', rateMode: 'uniform',
  }],
});
const definitionId = `def_round9_${suffix}`;
const definitionFlowId = `flow_round9_${suffix}`;
const definition = {
  schemaVersion: 2,
  definitionId,
  flowId: definitionFlowId,
  revision: 1,
  name: 'round9 definition',
  description: '',
  variables: [],
  nodes: [{ kind: 'module-call', instanceId: 'call_one', moduleId, parameters: { duration: 0.02 } }],
  solver: fixture.solver,
};
await postJson('/api/v1/workflow/flows/definitions', definition);

const waitState = async (projectId, predicate, label) => {
  let state = null;
  for (let attempt = 0; attempt < 120; attempt += 1) {
    state = await getJson(`/api/v1/workflow/projects/${projectId}/state`);
    if (predicate(state)) return state;
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  throw new Error(`state ${label} not reached: ${JSON.stringify(state)}`);
};

// --- Browser ----------------------------------------------------------------
const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const page = await browser.newPage({ viewport: { width: 1600, height: 1400 } });
page.setDefaultTimeout(120000);
const errors = [];
page.on('pageerror', error => errors.push(error.message));
page.on('dialog', async dialog => { await dialog.accept(); });

const flowBar = () => page.locator('.spx-flow-bar');
const compileBadge = () => page.locator('.spx-compile-badge');
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
const compileFlow = async () => {
  await flowBar().getByRole('button', { name: /编译流程|重新编译/ }).click();
  await compileBadge().filter({ hasText: '已编译' }).waitFor({ timeout: 120000 });
};
const saveFromFlowBar = async () => {
  const response = page.waitForResponse(item => {
    const method = item.request().method();
    return (method === 'PUT' || method === 'POST')
      && /^\/api\/v1\/workflow\/projects\/?[^/]*$/.test(new URL(item.url()).pathname);
  });
  await flowBar().getByRole('button', { name: '保存工程', exact: true }).click();
  assert.ok((await response).ok(), 'the project save must succeed');
  await page.waitForTimeout(900);
};
const expectStale = async label => {
  await compileBadge().filter({ hasText: '需重新编译' }).waitFor({ timeout: 30000 });
  assert.equal(await flowBar().getByRole('button', { name: '创建 Route', exact: true }).count(), 0,
    `${label}: a stale Compile must not offer 创建 Route`);
  assert.equal(await flowBar().getByRole('button', { name: '去 03', exact: true }).count(), 0,
    `${label}: a stale Compile must not offer 去 03`);
};
const expectFresh = async label => {
  await compileBadge().filter({ hasText: '已编译' }).waitFor({ timeout: 30000 });
  assert.equal(await compileBadge().filter({ hasText: '需重新编译' }).count(), 0, `${label}: must not be stale`);
};
// A <details> keeps its open state across project switches, so only toggle it
// when the panel content is not already visible.
const openAdvanced = async (summaryText, target) => {
  if (await target.isVisible()) return;
  await page.getByText(summaryText).click();
  await target.waitFor({ state: 'visible', timeout: 15000 });
};
const createRouteFromFlowBar = async () => {
  await flowBar().getByRole('button', { name: '创建 Route', exact: true }).click();
  await page.locator('.wf-summary').filter({ hasText: 'Route 已创建' }).waitFor({ timeout: 60000 });
};
const executeCurrentSegment = async (segmentId) => {
  const button = page.getByRole('button', { name: /执行当前 Segment|执行前序并运行/ });
  assert.equal(await button.isEnabled(), true, 'the current Segment must be executable');
  await button.click();
  const done = await waitSegment(segmentId);
  assert.equal(done.status, 'succeeded', JSON.stringify(done));
  await page.waitForTimeout(600);
  return done;
};

try {
  // A previous run may have left a project id in localStorage; the mount
  // effect would auto-open it and can land in the middle of this test.
  await page.addInitScript(() => {
    try { window.localStorage.removeItem('viennaps.workflow.projectId'); } catch { /* ignore */ }
  });
  await page.goto(`${baseUrl}/`);

  // ---- 1: replacing the compiled step is stale, never runs the old 0.01 ---
  await openProject('Round9 单步替换');
  await openZone('02 流程');
  await compileFlow();
  const stateSingle = await waitState(projectIds['Round9 单步替换'],
    state => !!state.compileBasis, 'compileBasis after compile');
  const oldSingleCompileId = stateSingle.compileId;
  assert.ok(stateSingle.compileBasis.length <= 128, 'the basis must fit the state field');
  assert.equal(authoredDuration(await getJson(`/api/v1/workflow/compiles/${oldSingleCompileId}`), 'p1_step_01'), 0.01);
  // Delete the compiled step and add a brand-new authored step at 0.2.
  await page.locator('[aria-label="流程步骤"] tbody tr').first().click();
  await page.getByRole('button', { name: '删除步骤', exact: true }).click();
  await page.getByRole('button', { name: /添加步骤/ }).click();
  await page.locator('input[aria-label="步骤时长"]').fill('0.2');
  await saveFromFlowBar();
  await expectStale('replaced step');
  const stateReplaced = await getJson(`/api/v1/workflow/projects/${projectIds['Round9 单步替换']}/state`);
  assert.equal(stateReplaced.compileId, oldSingleCompileId,
    'saving must not move the compile pointer; only a recompile does');
  await page.screenshot({ path: path.join(shotDir, 'round9-01-steps-stale.png'), fullPage: true });

  await openProject('Round9 单步替换');
  await openZone('02 流程');
  await expectStale('replaced step after reload');
  await openZone('03 运行');
  const staleRunReason = page.locator('[aria-label="执行禁用原因"]');
  await staleRunReason.waitFor({ timeout: 30000 });
  assert.match(await staleRunReason.innerText(), /编译已过期/);
  assert.equal(await page.getByRole('button', { name: /执行当前 Segment|执行前序并运行/ }).count(), 0,
    'the stale 0.01 Compile must not be executable');

  // ---- 2: a definition Compile followed by a domain edit is stale ---------
  await openProject('Round9 定义改域');
  await openZone('02 流程');
  await openAdvanced('高级：模块化流程定义（JSON）', page.getByLabel('流程定义 JSON'));
  await page.getByLabel('流程定义 JSON').fill(JSON.stringify(definition, null, 2));
  await page.getByRole('button', { name: '编译模块化流程', exact: true }).click();
  await expectFresh('definition compile');
  const stateGeometry = await waitState(projectIds['Round9 定义改域'],
    state => !!state.compileBasis, 'definition compileBasis');
  const definitionCompile = await getJson(`/api/v1/workflow/compiles/${stateGeometry.compileId}`);
  assert.equal(definitionCompile.project.domain.xExtent, 4,
    'the definition Compile must still carry xExtent=4 before the edit');
  await page.screenshot({ path: path.join(shotDir, 'round9-02-definition-compiled.png'), fullPage: true });

  await openZone('01 工程');
  await page.locator('input[aria-label="工程 xExtent"]').fill('8');
  const saveResponse = page.waitForResponse(item => {
    const method = item.request().method();
    return (method === 'PUT' || method === 'POST')
      && /^\/api\/v1\/workflow\/projects\/[^/]+$/.test(new URL(item.url()).pathname);
  });
  await page.getByRole('button', { name: '保存更改', exact: true }).click();
  assert.ok((await saveResponse).ok(), 'the domain edit must save');
  await page.waitForTimeout(900);
  await openZone('02 流程');
  await expectStale('definition domain edit');
  await page.screenshot({ path: path.join(shotDir, 'round9-02-definition-domain-stale.png'), fullPage: true });

  await openProject('Round9 定义改域');
  await openZone('02 流程');
  await expectStale('definition domain edit after reload');
  await openZone('03 运行');
  await page.locator('[aria-label="执行禁用原因"]').waitFor({ timeout: 30000 });
  assert.match(await page.locator('[aria-label="执行禁用原因"]').innerText(), /编译已过期/);

  // ---- 3: a restored 0.04 branch survives an unchanged save ---------------
  await openProject('Round9 分支保存');
  await openZone('02 流程');
  await compileFlow();
  const stateBranchSave = await waitState(projectIds['Round9 分支保存'],
    state => !!state.compileBasis, 'branch base compileBasis');
  const baseCompileId = stateBranchSave.compileId;
  const baseBasis = stateBranchSave.compileBasis;
  await createRouteFromFlowBar();
  const baseRouteState = await waitState(projectIds['Round9 分支保存'],
    state => !!state.routeId, 'base route pointer');
  const baseRouteId = baseRouteState.routeId;
  const basePlan = await getJson(`/api/v1/workflow/routes/${baseRouteId}`);
  await openZone('03 运行');
  await executeCurrentSegment(basePlan.plan.segments[0].segmentId);
  await openAdvanced('高级：分支与测量决策', page.getByLabel('尾步时长'));
  await page.getByLabel('尾步时长').fill('0.04');
  await page.getByRole('button', { name: '修改尾步并分支', exact: true }).click();
  // A branch keeps the Route id and appends an attempt; only the Compile moves.
  const branchState = await waitState(projectIds['Round9 分支保存'],
    state => !!state.routeId && state.compileId !== baseCompileId,
    'branch compile pointer');
  const branchCompileId = branchState.compileId;
  const tailAuthoredId = branchSaveProject.steps[branchSaveProject.steps.length - 1].id;
  assert.equal(authoredDuration(await getJson(`/api/v1/workflow/compiles/${branchCompileId}`), tailAuthoredId), 0.04);
  assert.equal(branchState.compileBasis, baseBasis,
    'a branch Compile keeps the authored project basis, not the derived tail');
  const branchPlan = await getJson(`/api/v1/workflow/routes/${branchState.routeId}`);
  // The base attempt keeps its own planned tail; the branch tail belongs to
  // the head attempt and is the Segment 03 resumes at.
  const branchSegment = branchPlan.plan.segments.find(item => item.status === 'planned'
    && item.attemptId === branchPlan.plan.route.headAttemptId);
  assert.ok(branchSegment, 'the branch must append a planned tail Segment');
  assert.equal(branchSegment.sourceCompileId, branchCompileId);

  await openZone('02 流程');
  await saveFromFlowBar();
  await expectFresh('branch unchanged save');
  await page.screenshot({ path: path.join(shotDir, 'round9-03-branch-saved.png'), fullPage: true });

  await openProject('Round9 分支保存');
  await openZone('02 流程');
  await expectFresh('branch unchanged save after reload');
  assert.equal(await flowBar().getByRole('button', { name: '去 03', exact: true }).count(), 1);
  await openZone('03 运行');
  assert.equal(await page.locator('[aria-label="执行禁用原因"]').count(), 0);
  await executeCurrentSegment(branchSegment.segmentId);
  const branchExecuted = await getJson(`/api/v1/workflow/routes/${branchState.routeId}`);
  const executedTail = branchExecuted.plan.segments.find(item => item.segmentId === branchSegment.segmentId);
  assert.equal(executedTail.status, 'succeeded', JSON.stringify(executedTail));
  assert.equal(executedTail.sourceCompileId, branchCompileId,
    'the restored branch must still execute its derived 0.04 Compile');
  await page.screenshot({ path: path.join(shotDir, 'round9-03-branch-restored.png'), fullPage: true });

  // ---- 4: a restored branch rebuilt into a new Route stays fresh ----------
  await openProject('Round9 分支重建');
  await openZone('02 流程');
  await compileFlow();
  const stateRebuild = await waitState(projectIds['Round9 分支重建'],
    state => !!state.compileBasis, 'rebuild base compileBasis');
  await createRouteFromFlowBar();
  const rebuildBaseRoute = await waitState(projectIds['Round9 分支重建'],
    state => !!state.routeId, 'rebuild base route');
  const rebuildPlan = await getJson(`/api/v1/workflow/routes/${rebuildBaseRoute.routeId}`);
  await openZone('03 运行');
  await executeCurrentSegment(rebuildPlan.plan.segments[0].segmentId);
  await openAdvanced('高级：分支与测量决策', page.getByLabel('尾步时长'));
  await page.getByLabel('尾步时长').fill('0.04');
  await page.getByRole('button', { name: '修改尾步并分支', exact: true }).click();
  const branched = await waitState(projectIds['Round9 分支重建'],
    state => !!state.routeId && state.compileId !== stateRebuild.compileId,
    'branch compile for rebuild');
  const derivedCompileId = branched.compileId;
  assert.equal(authoredDuration(await getJson(`/api/v1/workflow/compiles/${derivedCompileId}`), tailAuthoredId), 0.04);

  // Restore the branch, then rebuild the Route from its derived Compile.
  await openProject('Round9 分支重建');
  await openZone('02 流程');
  await expectFresh('branch restored before rebuild');
  await flowBar().getByRole('button', { name: '重建 Route…', exact: true }).click();
  await page.getByRole('button', { name: '确认重建', exact: true }).click();
  const rebuilt = await waitState(projectIds['Round9 分支重建'],
    state => !!state.routeId && state.routeId !== branched.routeId, 'rebuilt route pointer');
  const rebuiltPlan = await getJson(`/api/v1/workflow/routes/${rebuilt.routeId}`);
  assert.equal(rebuilt.compileId, derivedCompileId, 'the rebuilt Route keeps the derived Compile');
  await page.screenshot({ path: path.join(shotDir, 'round9-04-branch-rebuilt.png'), fullPage: true });

  await openProject('Round9 分支重建');
  await openZone('02 流程');
  await expectFresh('rebuilt Route after reload');
  await openZone('03 运行');
  assert.equal(await page.locator('[aria-label="执行禁用原因"]').count(), 0);
  await executeCurrentSegment(rebuiltPlan.plan.segments[0].segmentId);

  // ---- 5: an empty-steps project with a definition Compile is not stale ---
  await openProject('Round9 空步骤定义');
  await openZone('02 流程');
  await openAdvanced('高级：模块化流程定义（JSON）', page.getByLabel('流程定义 JSON'));
  await page.getByLabel('流程定义 JSON').fill(JSON.stringify(definition, null, 2));
  await page.getByRole('button', { name: '编译模块化流程', exact: true }).click();
  await expectFresh('empty-steps definition compile');
  const emptyState = await waitState(projectIds['Round9 空步骤定义'],
    state => !!state.compileBasis, 'empty-steps definition basis');
  assert.ok(emptyState.compileId, 'the empty-steps definition compile must be recorded');
  await createRouteFromFlowBar();
  const emptyRoute = await waitState(projectIds['Round9 空步骤定义'],
    state => !!state.routeId, 'empty-steps route');
  await page.screenshot({ path: path.join(shotDir, 'round9-05-empty-definition.png'), fullPage: true });

  await openProject('Round9 空步骤定义');
  await openZone('02 流程');
  await expectFresh('empty-steps definition after reload');
  assert.equal(await flowBar().getByRole('button', { name: '去 03', exact: true }).count(), 1,
    'the restored empty-steps definition Route must offer 去 03');
  const emptyPlan = await getJson(`/api/v1/workflow/routes/${emptyRoute.routeId}`);
  await openZone('03 运行');
  assert.equal(await page.locator('[aria-label="执行禁用原因"]').count(), 0);
  await executeCurrentSegment(emptyPlan.plan.segments[0].segmentId);

  // ---- 6: a saved duration edit is stale ---------------------------------
  await openProject('Round9 时长修改');
  await openZone('02 流程');
  await compileFlow();
  await waitState(projectIds['Round9 时长修改'], state => !!state.compileBasis, 'duration compileBasis');
  await page.locator('[aria-label="流程步骤"] tbody tr').first().click();
  await page.locator('input[aria-label="步骤时长"]').fill('0.2');
  await saveFromFlowBar();
  await expectStale('duration edit');
  await page.screenshot({ path: path.join(shotDir, 'round9-06-duration-stale.png'), fullPage: true });

  await openProject('Round9 时长修改');
  await openZone('02 流程');
  await expectStale('duration edit after reload');
  await openZone('03 运行');
  await page.locator('[aria-label="执行禁用原因"]').waitFor({ timeout: 30000 });

  // ---- 7: a plain compile + unchanged save stays fresh -------------------
  await openProject('Round9 原样保存');
  await openZone('02 流程');
  await compileFlow();
  await waitState(projectIds['Round9 原样保存'], state => !!state.compileBasis, 'unchanged compileBasis');
  await saveFromFlowBar();
  await expectFresh('unchanged save');
  await page.screenshot({ path: path.join(shotDir, 'round9-07-unchanged-save.png'), fullPage: true });

  await openProject('Round9 原样保存');
  await openZone('02 流程');
  await expectFresh('unchanged save after reload');
  await createRouteFromFlowBar();
  const unchangedRoute = await waitState(projectIds['Round9 原样保存'],
    state => !!state.routeId, 'unchanged route');
  const unchangedPlan = await getJson(`/api/v1/workflow/routes/${unchangedRoute.routeId}`);
  await openZone('03 运行');
  assert.equal(await page.locator('[aria-label="执行禁用原因"]').count(), 0);
  assert.equal(await page.getByRole('button', { name: /执行当前 Segment|执行前序并运行/ }).isEnabled(), true);
  await executeCurrentSegment(unchangedPlan.plan.segments[0].segmentId);

  assert.deepEqual(errors, [], `page errors: ${JSON.stringify(errors)}`);
  console.log(JSON.stringify({
    event: 'round9_pass',
    replacedStep: { project: singleStepProject.name, compileId: oldSingleCompileId },
    definitionDomain: { project: geometryProject.name, compileId: stateGeometry.compileId },
    branchSave: { project: branchSaveProject.name, branchCompileId, tail: 0.04 },
    branchRebuild: { project: branchRebuildProject.name, routeId: rebuilt.routeId },
    emptySteps: { project: emptyStepsProject.name, routeId: emptyRoute.routeId },
    durationEdit: { project: durationProject.name },
    unchanged: { project: unchangedProject.name, routeId: unchangedRoute.routeId },
    screenshots: shotDir,
  }));
} finally {
  await browser.close();
}
