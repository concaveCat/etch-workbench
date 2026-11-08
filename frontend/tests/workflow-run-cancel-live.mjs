/**
 * Run-control acceptance: a second Segment preempts the live one.
 *
 * The single supervisor slot used to answer "a workbench run is already
 * active" and the old Segment became impossible to find or stop. This test
 * builds a two-Segment Route (two attempts, both starting from the initial
 * state so neither needs the other's checkpoint), starts the first Segment,
 * switches to the second while the first is still running and executes it.
 *
 * Asserts:
 *  - the first Segment ends cancelled/interrupted, the second starts;
 *  - no "already active" error banner appears;
 *  - the 03 running banner names the live Segment and offers 停止运行;
 *  - the stop button drives the live Segment to a terminal state.
 */
import assert from 'node:assert/strict';
import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const here = path.dirname(fileURLToPath(import.meta.url));
const baseUrl = (process.env.WORKBENCH_URL ?? 'http://127.0.0.1:8820').replace(/\/$/, '');
const shotDir = process.env.WORKFLOW_SHOT_DIR ?? path.resolve(here, '../../var/review-shots');
const prefix = process.env.WORKFLOW_SCREENSHOT ?? path.join(shotDir, 'workflow-run-cancel');
mkdirSync(shotDir, { recursive: true });

const projectName = 'Run Cancel Live';
const stepIds = ['rc_step_01', 'rc_step_02'];
const stepNames = ['Run Cancel A', 'Run Cancel B'];

const getJson = async endpoint => {
  const response = await fetch(`${baseUrl}${endpoint}`);
  const text = await response.text();
  assert.ok(response.ok, `${endpoint} -> ${response.status} ${text}`);
  return JSON.parse(text);
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
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

// A persistent preview data dir must not hit the same-name 409 guard.
for (const row of (await getJson('/api/v1/workflow/projects')).projects ?? []) {
  if (row.name === projectName) {
    await fetch(`${baseUrl}/api/v1/workflow/projects/${row.projectId}`, { method: 'DELETE' });
  }
}

// Two long CPU-only isotropic etch steps: the first run stays live long enough
// to be preempted and the second stays live long enough to be stopped.
const project = {
  schemaVersion: 7,
  name: projectName,
  dimension: 2,
  units: { length: 'um', time: 's', rate: 'um/s', angle: 'deg' },
  domain: { gridDelta: 0.02, xExtent: 4.0, xBoundary: 'reflective' },
  geometry: { kind: 'plane', height: 0.0, substrateMaterial: 'Si' },
  steps: stepIds.map((id, index) => ({
    id,
    name: stepNames[index],
    enabled: true,
    operation: 'etch',
    // CPU particle tracing is slow in wall-clock time but logs only one line
    // per time step, so the live window is long without hitting the log cap.
    model: 'singleParticle',
    rateMagnitude: 0.01,
    duration: 10,
    stickingProbability: 0.1,
    sourceExponent: 1.0,
    rateMode: 'uniform',
  })),
  solver: {
    timeStepRatio: 0.25, raysPerPoint: 4096, cpuThreads: 1,
    random: { mode: 'fixed', seed: 7 }, enginePreference: 'cpu', timeoutSeconds: 120,
  },
  view: { panX: 0, panH: 0, zoom: 1, showGrid: true },
};

const created = await postJson('/api/v1/workflow/projects', project);
const projectId = created.projectId;
assert.ok(projectId, 'project must be persisted');

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const page = await browser.newPage({ viewport: { width: 1600, height: 1500 } });
page.setDefaultTimeout(60000);
const errors = [];
page.on('pageerror', error => errors.push(error.message));

const waitForText = async (locator, pattern, label, timeout = 60000) => {
  const deadline = Date.now() + timeout;
  let text = '';
  while (Date.now() < deadline) {
    try {
      text = await locator.innerText();
    } catch {
      text = '';
    }
    if (pattern.test(text)) return text;
    await sleep(250);
  }
  throw new Error(`${label} not reached, last text: ${text}`);
};

try {
  await page.addInitScript(id => {
    window.localStorage.setItem('viennaps.workflow.projectId', id);
  }, projectId);
  await page.goto(`${baseUrl}/`);
  await page.getByText('Etch Workbench').first().waitFor();

  // Compile through the UI so the saved state also carries the compile basis;
  // only the Route itself is seeded through the API below.
  await page.getByRole('button', { name: '02 流程' }).click();
  await page.getByRole('button', { name: '编译流程' }).click();
  await page.getByText(/编译成功/).waitFor();

  let state = null;
  const stateDeadline = Date.now() + 20000;
  while (Date.now() < stateDeadline) {
    state = await getJson(`/api/v1/workflow/projects/${projectId}/state`);
    if (state.compileId && state.compileBasis) break;
    await sleep(250);
  }
  assert.ok(state?.compileId && state?.compileBasis, `compile state must persist: ${JSON.stringify(state)}`);
  const compiled = await getJson(`/api/v1/workflow/compiles/${state.compileId}`);
  // The Compile assigns its own stable step ids; names are the authored ones.
  const compiledSteps = compiled.project.steps.map(step => step.id);
  assert.equal(compiledSteps.length, 2, 'compile must keep both steps');
  assert.deepEqual(compiled.project.steps.map(step => step.name), stepNames, 'compile must keep the step names');

  // Two attempts, one Segment each, both from the initial state. A split Route
  // would chain them through a checkpoint, which cannot exist while A runs.
  const suffix = Date.now().toString(36);
  const routeId = `route_rc_${suffix}`;
  const attempts = compiledSteps.map((stepId, index) => ({
    attemptId: `attempt_rc_${suffix}_${index}`,
    routeId,
    compileId: state.compileId,
    submissionKey: `submit_rc_${suffix}_${index}`,
    startAfterStepId: null,
    throughStepId: stepId,
    seedPolicy: 'shared-run-seed',
    runSeed: compiled.project.solver.random.seed,
    flowDigest: compiled.compileRecord.fullProjectHash,
    status: 'planned',
  }));
  const segments = compiledSteps.map((stepId, index) => ({
    segmentId: `segment_rc_${suffix}_${index}`,
    attemptId: attempts[index].attemptId,
    sourceCompileId: state.compileId,
    segmentProjectHash: compiled.compileRecord.fullProjectHash,
    startAfterStepId: null,
    throughStepId: stepId,
    startCheckpointRef: null,
    endCheckpointRef: null,
    runId: `pending_rc_${suffix}_${index}`,
    provenanceInputHash: '0'.repeat(64),
    flowDigest: compiled.compileRecord.fullProjectHash,
    executionHash: '0'.repeat(64),
    status: 'planned',
  }));
  await postJson('/api/v1/workflow/routes', {
    route: {
      routeId, flowId: compiled.compileRecord.flowId,
      name: 'Run Cancel Live Route', headAttemptId: attempts[0].attemptId,
    },
    stepOrder: compiledSteps,
    attempts,
    segments,
    reuse: [],
  });
  await sendJson(`/api/v1/workflow/projects/${projectId}/state`, 'PUT', {
    ...state,
    routeId,
    segmentRuns: [],
    measurementIds: [],
    experimentIds: [],
  });

  await page.reload();
  await page.getByText('Etch Workbench').first().waitFor();
  await page.getByRole('button', { name: '03 运行' }).click();
  const rows = page.locator('.wf-segment-row');
  await rows.first().waitFor({ timeout: 30000 });
  assert.equal(await rows.count(), 2, 'Route must show exactly two Segments');
  await page.locator('.wf-summary').filter({ hasText: 'Route 已创建' }).waitFor();

  // --- 1. Start Segment A and see it in the live banner -------------------
  await rows.nth(0).click();
  await page.getByRole('button', { name: '执行当前 Segment' }).click();
  const banner = page.locator('[aria-label="正在运行"]');
  await banner.waitFor({ timeout: 30000 });
  const bannerText = await banner.innerText();
  assert.match(bannerText, /正在运行：/, `banner must name the live step: ${bannerText}`);
  assert.match(bannerText, /Run Cancel A/, `banner must name step A: ${bannerText}`);
  const stopButton = banner.getByRole('button', { name: '停止运行' });
  await stopButton.waitFor();
  assert.ok(await stopButton.isEnabled(), 'the stop button must be clickable while a run is live');
  await page.screenshot({ path: `${prefix}-running.png`, fullPage: false });

  // --- 2. Switching to B and executing preempts A ------------------------
  await rows.nth(1).click();
  assert.equal(await rows.nth(1).getAttribute('aria-current'), 'true', 'the second Segment must be selectable');
  await page.getByText('将自动停止上一个运行').waitFor();
  await page.getByRole('button', { name: '执行当前 Segment' }).click();
  await waitForText(rows.nth(0), /已取消|已中断/, 'the preempted Segment must become terminal');
  await waitForText(banner, /Run Cancel B/, 'the banner must switch to the new Segment');
  const errorBanner = page.locator('.spx-error');
  if (await errorBanner.count()) {
    throw new Error(`preemption must not surface an error: ${await errorBanner.innerText()}`);
  }
  await page.screenshot({ path: `${prefix}-switched.png`, fullPage: false });

  // --- 3. The stop button terminates the live run ------------------------
  await banner.getByRole('button', { name: '停止运行' }).click();
  const notice = page.locator('[aria-label="运行提示"]');
  await notice.waitFor({ timeout: 60000 });
  assert.match(await notice.innerText(), /已停止/, 'the UI must confirm the stop');
  await waitForText(rows.nth(1), /已取消|已中断/, 'the stopped Segment must become terminal');
  assert.equal(await page.locator('[aria-label="正在运行"]').count(), 0, 'the banner must hide after the stop');
  await page.screenshot({ path: `${prefix}-stopped.png`, fullPage: false });

  assert.deepEqual(errors, [], `page errors: ${errors.join('; ')}`);
  console.log(`workflow-run-cancel-live OK · screenshots ${prefix}-{running,switched,stopped}.png`);
} finally {
  await browser.close();
  await fetch(`${baseUrl}/api/v1/workflow/projects/${projectId}`, { method: 'DELETE' }).catch(() => undefined);
}
