/**
 * Round-10 review regression: the Compile basis is the *compiled* authored
 * project, and it is taken from the server-normalized document.
 *
 * Two heuristics still leaked:
 *
 *  1. `applyTemplate`/`compileFlow` recorded the basis from the pre-Compile
 *     closure (`project`), then replaced the steps with `authoredSteps`. A
 *     5-step project that appended one 0.2 s template step therefore stored a
 *     5-step basis next to a 6-step Compile; deleting the appended step and
 *     saving compared 5 vs 5 and looked fresh while the old Compile ran 6.
 *  2. The fingerprint only padded solver defaults. Saving fills the rest
 *     (`maskMaterial`, Fin `taperAngle`/`maskHeight`, `stack.baseHeight`,
 *     `step.rateMode` …), so an imported project that omitted them looked
 *     edited right after its first unchanged save.
 *
 * Probes:
 *  1. 5 steps + template append(0.2) → Compile/Route → delete the appended
 *     step → save → reload: STALE (project 5 steps, old Compile 6).
 *  2. 5 steps + template append(0.2) → save unchanged → reload: NOT stale, and
 *     the basis equals the fingerprint of the normalized 6-step project.
 *  3. Imported trench without `maskMaterial`, fin without
 *     `taperAngle`/`maskHeight`, stack without `baseHeight`, step without
 *     `rateMode`: Compile → unchanged save → reload: NOT stale; the basis
 *     equals the normalized project. Editing duration/domain: STALE.
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
/** The server's canonical form: what a save stores, without storing it. */
const normalizedProject = async document => (await postJson('/api/v1/workflow/projects/normalize', document)).project;

const waitState = async (projectId, predicate, label) => {
  let state = null;
  for (let attempt = 0; attempt < 120; attempt += 1) {
    state = await getJson(`/api/v1/workflow/projects/${projectId}/state`);
    if (predicate(state)) return state;
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  throw new Error(`state ${label} not reached: ${JSON.stringify(state)}`);
};
const projectIdByName = async name => {
  const rows = (await getJson('/api/v1/workflow/projects')).projects ?? [];
  const row = rows.find(item => item.name === name);
  assert.ok(row, `project ${name} must exist`);
  return row.projectId;
};
const compileSteps = async compileId => (await getJson(`/api/v1/workflow/compiles/${compileId}`)).project.steps;

const fixture = JSON.parse(readFileSync(projectPath, 'utf8'));
const suffix = Date.now().toString(36);
const templateId = `tpl_round10_${suffix}`;
const managedNames = [
  'Round10 模板追加',
  'Round10 模板原样',
  'Round10 缺省 trench',
  'Round10 缺省 fin',
  'Round10 缺省 stack',
  'Round10 缺省 step',
];
for (const row of (await getJson('/api/v1/workflow/projects')).projects ?? []) {
  if (managedNames.includes(row.name)) {
    await fetch(`${baseUrl}/api/v1/workflow/projects/${row.projectId}`, { method: 'DELETE' });
  }
}

// One template step at ${duration}; the UI override fills 0.2.
await postJson('/api/v1/workflow/templates', {
  schemaVersion: 1,
  templateId,
  name: `round10 template ${suffix}`,
  description: '',
  steps: [{
    id: 'tpl_round10_etch',
    name: 'template etch',
    enabled: true,
    operation: 'etch',
    model: 'isotropic',
    rateMagnitude: 0.0,
    duration: '${duration}',
  }],
  variables: [{ name: 'duration', value: 0.2, unit: 's' }],
});

// The template scenarios start from a persisted 5-step project.
for (const name of ['Round10 模板追加', 'Round10 模板原样']) {
  await postJson('/api/v1/workflow/projects', { ...fixture, name });
}

// Imported projects deliberately omit defaults the server fills on save.
const omittedTrench = {
  ...fixture,
  name: 'Round10 缺省 trench',
  geometry: {
    kind: 'trench', width: 1.0, depth: 2.0,
    taperAngle: 0.0, maskHeight: 0.0, maskTaperAngle: 0.0,
    substrateMaterial: 'Si',
    // maskMaterial omitted.
  },
  steps: [{ ...fixture.steps[0], rateMode: undefined }],
};
const omittedFin = {
  ...fixture,
  name: 'Round10 缺省 fin',
  geometry: {
    kind: 'fin', width: 1.0, height: 1.2, substrateMaterial: 'Si',
    // taperAngle / maskHeight / maskTaperAngle / maskMaterial / construction omitted.
  },
  steps: [{ ...fixture.steps[0], rateMode: undefined }],
};
const omittedStack = {
  ...fixture,
  name: 'Round10 缺省 stack',
  dimension: 3,
  domain: { gridDelta: 0.2, xExtent: 6.0, yExtent: 4.0, boundaryXY: 'reflective' },
  view: undefined,
  view3D: {},
  geometry: {
    kind: 'stack', substrateMaterial: 'Si',
    // baseHeight omitted.
    layers: [{ id: 'layer_mask', name: 'mask', material: 'Si3N4', thickness: 0.4, role: 'mask' }],
    pattern: { kind: 'trench', width: 1.0, taperAngle: 0.0 },
  },
  steps: [{ ...fixture.steps[0], rateMode: undefined }],
};
const omittedStep = {
  ...fixture,
  name: 'Round10 缺省 step',
  steps: [{ ...fixture.steps[0], rateMode: undefined }],
};

// --- Browser ----------------------------------------------------------------
const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
// A fresh context starts with empty localStorage, so a previous run's project
// id cannot auto-open in the middle of the test — while the later real reload
// still finds the id this run saved.
const context = await browser.newContext({ viewport: { width: 1600, height: 1400 } });
const page = await context.newPage();
page.setDefaultTimeout(120000);
const errors = [];
page.on('pageerror', error => errors.push(error.message));
page.on('dialog', async dialog => { await dialog.accept(); });

const flowBar = () => page.locator('.spx-flow-bar');
const compileBadge = () => page.locator('.spx-compile-badge');
const stepRows = () => page.locator('[aria-label="流程步骤"] tbody tr');
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
const importProject = async document => {
  await closeToStart();
  await page.locator('input[accept=".json"]').setInputFiles({
    name: `${document.name}.json`,
    mimeType: 'application/json',
    buffer: Buffer.from(JSON.stringify(document)),
  });
  await page.locator('.spx-project-status').filter({ hasText: '导入的工程尚未写入服务端' })
    .waitFor({ timeout: 30000 });
};
const compileFlow = async () => {
  await flowBar().getByRole('button', { name: /编译流程|重新编译/ }).click();
  await compileBadge().filter({ hasText: '已编译' }).waitFor({ timeout: 120000 });
};
const saveFromFlowBar = async () => {
  const response = page.waitForResponse(item => {
    const method = item.request().method();
    return (method === 'PUT' || method === 'POST')
      && /^\/api\/v1\/workflow\/projects(\/proj_[a-f0-9]+)?$/.test(new URL(item.url()).pathname);
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
const openAdvanced = async (summaryText, target) => {
  if (await target.isVisible()) return;
  await page.getByText(summaryText).click();
  await target.waitFor({ state: 'visible', timeout: 15000 });
};
const appendTemplate = async () => {
  await openAdvanced('模板（可选）', page.getByLabel('模板选择'));
  await page.getByLabel('模板选择').selectOption(templateId);
  await page.getByLabel('模板变量值').fill('0.2');
  await page.getByRole('button', { name: '插入模板', exact: true }).click();
  await page.waitForFunction(
    count => document.querySelectorAll('[aria-label="流程步骤"] tbody tr').length === count,
    6,
    { timeout: 120000 },
  );
};
const createRouteFromFlowBar = async () => {
  await flowBar().getByRole('button', { name: '创建 Route', exact: true }).click();
  await page.locator('.wf-summary').filter({ hasText: 'Route 已创建' }).waitFor({ timeout: 60000 });
};
const assertBasisIsNormalizedProject = async (projectId, label) => {
  const saved = await getJson(`/api/v1/workflow/projects/${projectId}`);
  const state = await getJson(`/api/v1/workflow/projects/${projectId}/state`);
  assert.equal(state.compileBasis, executionCompileBasis(await normalizedProject(saved)),
    `${label}: the stored basis must be the fingerprint of the normalized project`);
};

try {
  await page.goto(`${baseUrl}/`);

  // ---- 1: template append, then deleting the appended step is stale -------
  await openProject('Round10 模板追加');
  await openZone('02 流程');
  await compileFlow();
  const beforeAppend = await waitState(await projectIdByName('Round10 模板追加'),
    state => !!state.compileBasis, 'five-step basis');
  assert.equal((await compileSteps(beforeAppend.compileId)).length, 5);

  await appendTemplate();
  const afterAppend = await waitState(await projectIdByName('Round10 模板追加'),
    state => !!state.compileBasis && state.compileId !== beforeAppend.compileId,
    'template compile pointer');
  assert.notEqual(afterAppend.compileBasis, beforeAppend.compileBasis,
    'appending a template step must move the basis to the 6-step project');
  const appendedSteps = await compileSteps(afterAppend.compileId);
  assert.equal(appendedSteps.length, 6);
  assert.deepEqual(appendedSteps.map(step => Number(step.duration)).filter(value => value === 0.2), [0.2],
    'the template override must compile one 0.2 s step');

  await createRouteFromFlowBar();
  const routedState = await waitState(await projectIdByName('Round10 模板追加'),
    state => !!state.routeId, 'template route pointer');

  // Delete the appended (last) authored step and save: 5 steps vs a 6-step Compile.
  await openZone('02 流程');
  assert.equal(await stepRows().count(), 6);
  await stepRows().last().click();
  await page.getByRole('button', { name: '删除步骤', exact: true }).click();
  await page.waitForFunction(
    () => document.querySelectorAll('[aria-label="流程步骤"] tbody tr').length === 5,
    null,
    { timeout: 30000 },
  );
  await saveFromFlowBar();
  await expectStale('deleted template step');
  const savedAfterDelete = await getJson(`/api/v1/workflow/projects/${await projectIdByName('Round10 模板追加')}`);
  assert.equal(savedAfterDelete.steps.length, 5, 'the saved project is back to 5 steps');
  assert.equal((await compileSteps(routedState.compileId)).length, 6, 'the old Compile still expands to 6 steps');
  const afterDelete = await getJson(`/api/v1/workflow/projects/${await projectIdByName('Round10 模板追加')}/state`);
  assert.equal(afterDelete.compileBasis, routedState.compileBasis,
    'saving must not move the basis; only a recompile does');
  await page.screenshot({ path: path.join(shotDir, 'round10-01-template-deleted-stale.png'), fullPage: true });

  await openProject('Round10 模板追加');
  await openZone('02 流程');
  await expectStale('deleted template step after reload');
  await openZone('03 运行');
  const staleReason = page.locator('[aria-label="执行禁用原因"]');
  await staleReason.waitFor({ timeout: 30000 });
  assert.match(await staleReason.innerText(), /编译已过期/);
  // The Route was restored, so 03 renders its execute actions — disabled.
  const executeButton = page.getByRole('button', { name: /执行当前 Segment|执行前序并运行/ });
  assert.equal(await executeButton.isDisabled(), true, 'the stale Compile must not be executable');
  assert.equal(await page.getByRole('button', { name: '执行全部 Segment', exact: true }).isDisabled(), true);

  // ---- 2: an appended template saved unchanged stays fresh ----------------
  await openProject('Round10 模板原样');
  await openZone('02 流程');
  await compileFlow();
  const templateFreshId = await projectIdByName('Round10 模板原样');
  const beforeTemplate = await waitState(templateFreshId, state => !!state.compileBasis, 'template base basis');
  await appendTemplate();
  const appendedState = await waitState(templateFreshId,
    state => !!state.compileBasis && state.compileId !== beforeTemplate.compileId,
    'template fresh compile pointer');
  await saveFromFlowBar();
  await expectFresh('template unchanged save');
  assert.equal((await getJson(`/api/v1/workflow/projects/${templateFreshId}`)).steps.length, 6);
  await assertBasisIsNormalizedProject(templateFreshId, 'template unchanged save');
  assert.equal(await flowBar().getByRole('button', { name: '创建 Route', exact: true }).count(), 1,
    'a fresh template Compile must offer 创建 Route');
  await page.screenshot({ path: path.join(shotDir, 'round10-02-template-saved-fresh.png'), fullPage: true });

  // A real page reload exercises the mount restore path.
  await page.reload();
  await page.locator('.spx-tree-root').filter({ hasText: 'Round10 模板原样' }).waitFor({ timeout: 60000 });
  await openZone('02 流程');
  await expectFresh('template unchanged save after reload');
  assert.equal((await getJson(`/api/v1/workflow/projects/${templateFreshId}/state`)).compileBasis,
    appendedState.compileBasis);

  // ---- 3: omitted defaults survive an unchanged save ----------------------
  const omittedCases = [
    { document: omittedTrench, edit: 'duration' },
    { document: omittedFin, edit: 'domain' },
    { document: omittedStack, edit: 'duration' },
    { document: omittedStep, edit: 'domain' },
  ];
  for (const { document, edit } of omittedCases) {
    await importProject(document);
    await openZone('02 流程');
    await compileFlow();
    await saveFromFlowBar();
    const projectId = await projectIdByName(document.name);
    const compiled = await waitState(projectId, state => !!state.compileBasis, `${document.name} basis`);
    await openProject(document.name);
    await openZone('02 流程');
    await expectFresh(`${document.name} unchanged save after reload`);
    await assertBasisIsNormalizedProject(projectId, document.name);
    const saved = await getJson(`/api/v1/workflow/projects/${projectId}`);
    assert.equal(saved.steps[0].rateMode, 'uniform', `${document.name}: the server fills step.rateMode`);
    if (document.geometry.kind === 'trench') assert.equal(saved.geometry.maskMaterial, 'Mask');
    if (document.geometry.kind === 'fin') {
      assert.equal(saved.geometry.taperAngle, 0);
      assert.equal(saved.geometry.maskHeight, 0);
    }
    if (document.geometry.kind === 'stack') assert.equal(saved.geometry.baseHeight, 0);
    await page.screenshot({
      path: path.join(shotDir, `round10-03-${document.geometry.kind}-fresh.png`), fullPage: true,
    });

    if (edit === 'duration') {
      await openZone('02 流程');
      await stepRows().first().click();
      await page.locator('input[aria-label="步骤时长"]').fill('0.05');
      await saveFromFlowBar();
    } else {
      await openZone('01 工程');
      await page.locator('input[aria-label="工程 xExtent"]').fill('5');
      const saveResponse = page.waitForResponse(item => {
        const method = item.request().method();
        return (method === 'PUT' || method === 'POST')
          && /^\/api\/v1\/workflow\/projects\/proj_[a-f0-9]+$/.test(new URL(item.url()).pathname);
      });
      await page.getByRole('button', { name: '保存更改', exact: true }).click();
      assert.ok((await saveResponse).ok(), `${document.name}: the domain edit must save`);
      await page.waitForTimeout(900);
      await openZone('02 流程');
    }
    await expectStale(`${document.name} ${edit} edit`);
    const afterEdit = await getJson(`/api/v1/workflow/projects/${projectId}/state`);
    assert.equal(afterEdit.compileId, compiled.compileId, `${document.name}: saving must not move the compile pointer`);
    await page.screenshot({
      path: path.join(shotDir, `round10-04-${document.geometry.kind}-${edit}-stale.png`), fullPage: true,
    });
  }

  assert.deepEqual(errors, [], `page errors: ${JSON.stringify(errors)}`);
  console.log(JSON.stringify({
    event: 'round10_pass',
    templateDelete: {
      project: 'Round10 模板追加',
      fiveStepBasis: beforeAppend.compileBasis,
      sixStepBasis: afterAppend.compileBasis,
    },
    templateFresh: { project: 'Round10 模板原样', compileId: appendedState.compileId },
    omittedDefaults: omittedCases.map(item => item.document.name),
    screenshots: shotDir,
  }));
} finally {
  await browser.close();
}
