/** Real Chromium acceptance for the global settings dialog and the storage-budget gate.
 *
 * Covers GET/PUT /api/settings, 文件 → 设置…, persistence across a reload, and the
 * storage preflight rejection on POST /api/runs. The original maxDataGiB is always
 * restored, including when an assertion fails, and the process exits non-zero on
 * any failed assertion.
 */
import assert from 'node:assert/strict';
import { chromium } from 'playwright';

const baseUrl = (process.env.WORKBENCH_URL ?? 'http://127.0.0.1:8872').replace(/\/$/, '');
const LIMIT_KEYS = ['maxDataGiB', 'artifactLimitMiB', 'rssLimitMiB', 'maxLogMiB', 'minFreeGiB'];
const STORAGE_KEYS = [
  'dataDirectory', 'usedBytes', 'freeBytes', 'totalBytes',
  'runs', 'projects', 'domainStates', 'gdsAssets', 'csvAssets',
];
const SOURCES = ['default', 'env', 'settings'];

/** Minimal valid ProjectV7: 2D plane, 4 µm wide, one isotropic etch step, seed 7, timeout 10 s. */
const PROJECT = {
  schemaVersion: 7,
  name: 'settings storage gate',
  dimension: 2,
  units: { length: 'um', time: 's', rate: 'um/s', angle: 'deg' },
  domain: { gridDelta: 0.2, xExtent: 4.0, xBoundary: 'reflective' },
  geometry: { kind: 'plane', height: 0.0, substrateMaterial: 'Si' },
  steps: [
    {
      id: 'settings_step_01', name: 'etch', enabled: true, operation: 'etch',
      model: 'isotropic', rateMagnitude: 0.02, duration: 0.02, rateMode: 'uniform',
    },
  ],
  solver: {
    timeStepRatio: 0.25, raysPerPoint: 16, cpuThreads: 1,
    random: { mode: 'fixed', seed: 7 }, enginePreference: 'cpu', timeoutSeconds: 10,
  },
  view: { panX: 0.0, panH: 0.0, zoom: 1.0, showGrid: true },
};

async function readJson(response) {
  const text = await response.text();
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

async function call(path, { method = 'GET', body } = {}) {
  const response = await fetch(`${baseUrl}${path}`, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  return { status: response.status, payload: await readJson(response) };
}

const getSettings = () => call('/api/settings');
const putSettings = body => call('/api/settings', { method: 'PUT', body });

async function listRunIds() {
  const { status, payload } = await call('/api/runs');
  assert.equal(status, 200, `GET /api/runs returned ${status}: ${JSON.stringify(payload).slice(0, 400)}`);
  return (payload.runs ?? []).map(run => run.runId);
}

function assertSettingsPayload(payload, label) {
  assert.ok(payload && typeof payload === 'object' && !Array.isArray(payload), `${label}: body must be a JSON object`);
  assert.ok(payload.limits && typeof payload.limits === 'object', `${label}: limits key is missing`);
  for (const key of LIMIT_KEYS) {
    const limit = payload.limits[key];
    assert.ok(limit && typeof limit === 'object', `${label}: limits.${key} is missing`);
    assert.ok(Number.isInteger(limit.value), `${label}: limits.${key}.value must be an integer`);
    assert.ok(Number.isInteger(limit.default), `${label}: limits.${key}.default must be an integer`);
    assert.ok(Number.isInteger(limit.minimum), `${label}: limits.${key}.minimum must be an integer`);
    assert.ok(limit.maximum === null || Number.isInteger(limit.maximum),
      `${label}: limits.${key}.maximum must be an integer or null`);
    assert.ok(SOURCES.includes(limit.source), `${label}: limits.${key}.source is ${limit.source}`);
    assert.equal(typeof limit.envVariable, 'string', `${label}: limits.${key}.envVariable must be a string`);
  }
  assert.ok(payload.storage && typeof payload.storage === 'object', `${label}: storage key is missing`);
  for (const key of STORAGE_KEYS) {
    assert.ok(key in payload.storage, `${label}: storage.${key} is missing`);
  }
  assert.equal(typeof payload.storage.dataDirectory, 'string', `${label}: storage.dataDirectory must be a string`);
  for (const key of STORAGE_KEYS.slice(1)) {
    assert.ok(Number.isFinite(payload.storage[key]), `${label}: storage.${key} must be a number`);
  }
  assert.ok(payload.defaults && typeof payload.defaults === 'object', `${label}: defaults key is missing`);
  assert.ok(Number.isInteger(payload.defaults.solverTimeoutSeconds)
    && payload.defaults.solverTimeoutSeconds >= 5
    && payload.defaults.solverTimeoutSeconds <= 86400,
    `${label}: defaults.solverTimeoutSeconds must be an integer in 5..86400`);
  assert.ok(payload.versions && typeof payload.versions === 'object', `${label}: versions key is missing`);
  for (const key of ['viennaps', 'viennals']) {
    assert.ok(key in payload.versions, `${label}: versions.${key} is missing`);
  }
}

/** Pick a different value that satisfies minimum <= value <= env cap. */
function nextMaxDataGiB(limit) {
  let next = limit.value + 1;
  if (limit.maximum !== null && next > limit.maximum) next = limit.value - 1;
  assert.notEqual(next, limit.value, `maxDataGiB is pinned at ${limit.value} with no room to change it`);
  assert.ok(next >= limit.minimum,
    `no valid different maxDataGiB: current ${limit.value}, minimum ${limit.minimum}, maximum ${limit.maximum}`);
  return next;
}

async function openSettings(page) {
  await page.getByRole('button', { name: '文件', exact: true }).click();
  await page.getByRole('menuitem', { name: '设置…', exact: true }).click();
  const dialog = page.locator('[role="dialog"][aria-label="设置"]');
  await dialog.waitFor();
  await dialog.locator('[aria-label="设置 maxDataGiB"]').waitFor();
  assert.equal(await page.locator('[role="dialog"][aria-label="设置"]').count(), 1,
    'exactly one 设置 dialog must be open');
  assert.equal(await dialog.locator('input[aria-label^="设置 "]').count(), LIMIT_KEYS.length + 1,
    `the 设置 dialog must expose ${LIMIT_KEYS.length} limit inputs and the solver timeout default`);
  return dialog;
}

async function restoreOriginal(limit, baseline) {
  const body = {
    limits: { maxDataGiB: limit.source === 'settings' ? limit.value : null },
    defaults: { solverTimeoutSeconds: baseline.defaults.solverTimeoutSeconds },
  };
  const response = await putSettings(body);
  assert.equal(response.status, 200,
    `restore PUT returned ${response.status}: ${JSON.stringify(response.payload).slice(0, 400)}`);
  const current = await getSettings();
  assert.equal(current.status, 200, `restore GET returned ${current.status}`);
  assertSettingsPayload(current.payload, 'restore GET');
  assert.equal(current.payload.limits.maxDataGiB.value, limit.value,
    `restored maxDataGiB must be ${limit.value}, got ${current.payload.limits.maxDataGiB.value}`);
  assert.equal(current.payload.defaults.solverTimeoutSeconds, baseline.defaults.solverTimeoutSeconds,
    `restoring maxDataGiB must restore solverTimeoutSeconds ${baseline.defaults.solverTimeoutSeconds}`);
  for (const key of LIMIT_KEYS.filter(item => item !== 'maxDataGiB')) {
    assert.equal(current.payload.limits[key].value, baseline.limits[key].value,
      `restoring maxDataGiB must not change ${key}`);
  }
  return current.payload.limits.maxDataGiB.value;
}

function rejectionDetail(payload) {
  if (typeof payload === 'string') return payload.slice(0, 500);
  const detail = payload?.detail;
  if (typeof detail === 'string') return detail.slice(0, 500);
  if (Array.isArray(detail)) {
    return detail.map(item => item?.message ?? JSON.stringify(item)).join('; ').slice(0, 500);
  }
  if (detail && typeof detail === 'object') return String(detail.message ?? JSON.stringify(detail)).slice(0, 500);
  return JSON.stringify(payload).slice(0, 500);
}

const MENTIONS_DATA_BUDGET = /(storage|data|存储|数据)[^]{0,80}(budget|cap|limit|上限|预算)/i;

let browser = null;
let baseline = null;
let original = null;
let edited = null;
let restored = null;
let originalSolver = null;
let editedSolver = null;
let rejection = null;
let needsRestore = false;
let failure = null;

try {
  // 1. Baseline API contract: five limits with their caps, storage accounting, versions.
  const first = await getSettings();
  assert.equal(first.status, 200,
    `GET /api/settings returned ${first.status}: ${JSON.stringify(first.payload).slice(0, 400)}`);
  assertSettingsPayload(first.payload, 'GET /api/settings');
  baseline = first.payload;
  original = { ...baseline.limits.maxDataGiB };
  edited = nextMaxDataGiB(original);
  originalSolver = baseline.defaults.solverTimeoutSeconds;
  editedSolver = originalSolver === 1200 ? 600 : 1200;

  // 2. UI: 文件 → 设置… exposes the dialog and all five inputs.
  browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
  page.setDefaultTimeout(30000);
  const pageErrors = [];
  page.on('pageerror', error => pageErrors.push(error.message));

  await page.goto(`${baseUrl}/`);
  await page.getByText('Etch Workbench').first().waitFor();
  let dialog = await openSettings(page);
  for (const key of LIMIT_KEYS) {
    await dialog.locator(`[aria-label="设置 ${key}"]`).waitFor();
  }

  // 3. Edit maxDataGiB through the dialog, verify the PUT and the reload persistence.
  const input = dialog.locator('[aria-label="设置 maxDataGiB"]');
  assert.equal(Number(await input.inputValue()), original.value,
    'the dialog must start from the effective maxDataGiB');
  const solverInput = dialog.locator('[aria-label="设置 solverTimeoutSeconds"]');
  assert.equal(Number(await solverInput.inputValue()), originalSolver,
    'the dialog must start from the effective solverTimeoutSeconds');
  needsRestore = true;
  await input.fill(String(edited));
  await solverInput.fill(String(editedSolver));
  await dialog.getByRole('button', { name: '保存设置', exact: true }).click();
  await dialog.getByText(/已保存/).waitFor();

  const afterSave = await getSettings();
  assert.equal(afterSave.status, 200, `GET /api/settings after save returned ${afterSave.status}`);
  assertSettingsPayload(afterSave.payload, 'GET /api/settings after save');
  assert.equal(afterSave.payload.limits.maxDataGiB.value, edited,
    `GET must reflect the saved maxDataGiB ${edited}`);
  assert.equal(afterSave.payload.limits.maxDataGiB.source, 'settings',
    'a saved maxDataGiB must report source=settings');
  assert.equal(afterSave.payload.defaults.solverTimeoutSeconds, editedSolver,
    `GET must reflect the saved solverTimeoutSeconds ${editedSolver}`);
  for (const key of LIMIT_KEYS.filter(item => item !== 'maxDataGiB')) {
    assert.equal(afterSave.payload.limits[key].value, baseline.limits[key].value,
      `saving maxDataGiB must not change ${key}`);
  }

  await page.reload();
  await page.getByText('Etch Workbench').first().waitFor();
  dialog = await openSettings(page);
  const persisted = dialog.locator('[aria-label="设置 maxDataGiB"]');
  await persisted.waitFor();
  assert.equal(await persisted.inputValue(), String(edited),
    `the dialog must show the persisted maxDataGiB ${edited} after reload`);
  const persistedSolver = dialog.locator('[aria-label="设置 solverTimeoutSeconds"]');
  assert.equal(await persistedSolver.inputValue(), String(editedSolver),
    `the dialog must show the persisted solverTimeoutSeconds ${editedSolver} after reload`);
  await dialog.getByRole('button', { name: '关闭', exact: true }).click();
  await dialog.waitFor({ state: 'detached' });
  assert.deepEqual(pageErrors, []);

  // 4. Effect: a 1 GiB data budget must reject the next run before it is created.
  const capped = await putSettings({ limits: { maxDataGiB: 1 } });
  assert.equal(capped.status, 200,
    `PUT maxDataGiB=1 returned ${capped.status}: ${JSON.stringify(capped.payload).slice(0, 400)}`);
  assertSettingsPayload(capped.payload, 'PUT maxDataGiB=1');
  assert.equal(capped.payload.limits.maxDataGiB.value, 1);

  const runsBefore = await listRunIds();
  const rejected = await call('/api/runs', { method: 'POST', body: { kind: 'simulate', project: PROJECT } });
  const rejectionText = typeof rejected.payload === 'string'
    ? rejected.payload
    : JSON.stringify(rejected.payload);
  assert.ok([422, 507].includes(rejected.status),
    `POST /api/runs under a 1 GiB budget must be rejected with 422/507, got ${rejected.status}: ${rejectionText.slice(0, 500)}`);
  assert.ok(MENTIONS_DATA_BUDGET.test(rejectionText),
    `rejection must mention the storage/data budget: ${rejectionText.slice(0, 500)}`);

  const runsAfter = await listRunIds();
  assert.equal(runsAfter.length, runsBefore.length,
    `rejected run must not create a run record (${runsBefore.length} -> ${runsAfter.length})`);
  for (const runId of runsBefore) {
    assert.ok(runsAfter.includes(runId), `rejected run must not remove the existing run ${runId}`);
  }
  rejection = { status: rejected.status, detail: rejectionDetail(rejected.payload) };

  // Restore the original effective value (stored override or env/default via null).
  restored = await restoreOriginal(original, baseline);
  needsRestore = false;
} catch (error) {
  failure = error;
} finally {
  // 5. Always leave the data budget as we found it, even after a failed assertion.
  if (needsRestore && original) {
    try {
      restored = await restoreOriginal(original, baseline);
      needsRestore = false;
    } catch (error) {
      if (failure === null) failure = error;
      else console.error(`restore failed after the first failure: ${error?.message ?? error}`);
    }
  }
  if (browser) await browser.close();
}

// 6. One-line PASS JSON, or a non-zero exit with the failure detail.
if (failure !== null) {
  console.error(`FAILED: ${failure?.stack ?? failure}`);
  process.exitCode = 1;
} else {
  console.log(JSON.stringify({
    status: 'passed',
    baseUrl,
    originalMaxDataGiB: {
      value: original.value,
      source: original.source,
      minimum: original.minimum,
      maximum: original.maximum,
      envValue: original.envValue,
    },
    editedMaxDataGiB: edited,
    restoredMaxDataGiB: restored,
    originalSolverTimeoutSeconds: originalSolver,
    editedSolverTimeoutSeconds: editedSolver,
    restoredSolverTimeoutSeconds: baseline.defaults.solverTimeoutSeconds,
    rejection,
  }));
}
