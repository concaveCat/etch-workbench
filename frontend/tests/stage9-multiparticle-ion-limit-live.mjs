/** Exercise the installed GPU ion limit in the real UI, then run the same input on CPU_DISK. */
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const baseUrl = (process.env.WORKBENCH_URL ?? 'http://127.0.0.1:8803').replace(/\/$/, '');
const projectPath = path.join(root, 'docs/stage9-evidence/gpu-coverage-audit/geometry-presets/multiParticle-2d-plane-direct-species-two-ion-different-energy-etch-cpu_disk/project.json');
const directPath = path.join(path.dirname(projectPath), 'direct.json');
const evidenceDir = path.join(root, 'docs/stage9-evidence/gpu-coverage-audit/browser/multiparticle-ion-limit');
const inputPath = path.join(evidenceDir, 'gpu-preference-input.json');
const reportPath = path.join(evidenceDir, 'acceptance.json');
await fs.mkdir(evidenceDir, { recursive: true });

async function api(url, options = {}) {
  const response = await fetch(`${baseUrl}${url}`, options);
  const body = await response.json();
  return { response, body };
}

async function waitForRun(runId) {
  const deadline = Date.now() + 120000;
  while (Date.now() < deadline) {
    const { response, body } = await api(`/api/runs/${runId}`);
    assert.equal(response.status, 200);
    if (['succeeded', 'failed', 'cancelled', 'timed_out', 'interrupted'].includes(body.status)) {
      assert.equal(body.status, 'succeeded', body.failureReason ?? `run ended ${body.status}`);
      return body;
    }
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  throw new Error(`run ${runId} did not finish within 120 seconds`);
}

function compareSnapshot(actual, expected, label) {
  assert.deepEqual(actual.points, expected.points, `${label}: coordinates`);
  assert.deepEqual(actual.lines, expected.cells, `${label}: topology`);
  assert.deepEqual(actual.cellData.materialIds, expected.materialIds, `${label}: cell material ids`);
  assert.deepEqual(actual.materialIdsByLevelSetIndex, expected.materialIdsByLevelSetIndex,
    `${label}: level-set material mapping`);
}

const project = JSON.parse(await fs.readFile(projectPath, 'utf8'));
const direct = JSON.parse(await fs.readFile(directPath, 'utf8'));
project.name = `Multi-ion GPU limitation browser ${Date.now()}`;
project.solver.enginePreference = 'gpu';
await fs.writeFile(inputPath, `${JSON.stringify(project, null, 2)}\n`);

const capability = await api('/api/capabilities');
assert.equal(capability.response.status, 200);
const limitation = capability.body.capabilities.knownEngineLimitations.find(row =>
  row.model === 'multiParticle' && row.engine === 'GPU_DISK' && row.engineVersion === '4.6.2');
assert.ok(limitation, 'capability API must publish the measured installed GPU ion limit');

const validation = await api('/api/projects/validate', {
  method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(project),
});
assert.equal(validation.response.status, 200, JSON.stringify(validation.body));
assert.ok(validation.body.warnings.some(warning => warning.includes('only converts 1 ion particle type')
  && warning.includes('CPU_DISK remains available')), JSON.stringify(validation.body.warnings));

const blockedRun = await api('/api/runs', {
  method: 'POST', headers: { 'content-type': 'application/json' },
  body: JSON.stringify({ kind: 'simulate', project }),
});
assert.equal(blockedRun.response.status, 422, JSON.stringify(blockedRun.body));
assert.ok(JSON.stringify(blockedRun.body).includes('additional ion species are ignored'));

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
const page = await context.newPage();
page.setDefaultTimeout(20000);
const pageErrors = [];
page.on('pageerror', error => pageErrors.push(error.message));
try {
  await page.goto(`${baseUrl}/`);
  await page.getByText('本机服务已连接').waitFor();
  await page.locator('nav[aria-label="项目操作"] input[type="file"]').setInputFiles(inputPath);
  await page.waitForFunction(name => document.querySelector('input[aria-label="项目名称"]')?.value === name, project.name);

  const engine = page.getByLabel('引擎偏好', { exact: true });
  const gpuOption = engine.locator('option[value="gpu"]');
  assert.equal(await engine.inputValue(), 'gpu');
  assert.equal(await gpuOption.evaluate(option => option.disabled), true);
  const notice = await page.locator('.capability-note').innerText();
  assert.match(notice, /最多转换 1 种离子/);
  assert.match(notice, /额外离子会从通量数组中丢失/);
  assert.equal(await page.getByRole('button', { name: '运行工艺', exact: true }).isDisabled(), true,
    'GPU-preferred project cannot submit an unsupported multi-ion GPU run');

  await engine.selectOption('auto');
  const requestPromise = page.waitForRequest(request => request.url().endsWith('/api/runs') && request.method() === 'POST');
  const responsePromise = page.waitForResponse(response => response.url().endsWith('/api/runs')
    && response.request().method() === 'POST');
  await page.getByRole('button', { name: '运行工艺', exact: true }).click();
  const [runRequest, runResponse] = await Promise.all([requestPromise, responsePromise]);
  assert.equal(runResponse.status(), 202, await runResponse.text());
  const submittedProject = runRequest.postDataJSON().project;
  assert.equal(submittedProject.solver.enginePreference, 'auto');
  await fs.writeFile(path.join(evidenceDir, 'cpu-auto-submitted-project.json'),
    `${JSON.stringify(submittedProject, null, 2)}\n`);
  const runId = (await runResponse.json()).runId;
  const run = await waitForRun(runId);
  const execution = run.actualExecution.find(row => row.stepId === submittedProject.steps[0].id);
  assert.equal(execution.actualEngine, 'CPU_DISK');
  const initial = (await api(`/api/runs/${runId}/snapshots/initial`)).body;
  const final = (await api(`/api/runs/${runId}/snapshots/${encodeURIComponent(submittedProject.steps[0].id)}`)).body;
  compareSnapshot(initial, direct.initial, 'initial');
  compareSnapshot(final, direct.final, 'final');

  const dataDir = path.resolve(process.env.VIENNAPS_WORKBENCH_DATA_DIR ?? path.join(root, 'var'));
  const runDir = path.join(dataDir, 'runs', runId);
  const worker = JSON.parse(await fs.readFile(path.join(runDir, 'worker-result.json'), 'utf8'));
  const guard = JSON.parse(await fs.readFile(path.join(runDir, 'child-record.json'), 'utf8'));
  assert.equal(worker.versions.viennaps, '4.6.2');
  assert.equal(worker.versions.viennals, '5.8.5');
  assert.equal(worker.stepRecords.find(row => row.stepId === submittedProject.steps[0].id).actualEngine, 'CPU_DISK');
  assert.equal(guard.returnCode, 0);
  assert.deepEqual(guard.remainingOwnedGroupMembers, []);
  await fs.copyFile(path.join(runDir, 'worker-result.json'), path.join(evidenceDir, 'cpu-product-worker.json'));
  await fs.copyFile(path.join(runDir, 'child-record.json'), path.join(evidenceDir, 'cpu-product.guard.json'));

  assert.deepEqual(pageErrors, []);
  const report = {
    status: 'passed', browser: 'Chromium', baseUrl,
    installedVersions: worker.versions,
    project: path.relative(root, projectPath), directReference: path.relative(root, directPath),
    input: path.relative(root, inputPath), submittedProject: path.relative(root, path.join(evidenceDir, 'cpu-auto-submitted-project.json')),
    limitation: { model: limitation.model, engine: limitation.engine, engineVersion: limitation.engineVersion,
      maxIonSpecies: limitation.maxIonSpecies, dimensions: limitation.dimensions,
      extraIonBehavior: limitation.extraIonBehavior },
    projectImport: { acceptedWithWarning: true, warning: validation.body.warnings[0], gpuOptionDisabled: true,
      runDisabledWhileGpuSelected: true, unsupportedRunStatus: blockedRun.response.status,
      unsupportedRunDetail: blockedRun.body.detail },
    cpuAutoRun: { runId, status: run.status, actualEngine: execution.actualEngine,
      initialSnapshotExactDirectReferenceMatch: true, finalSnapshotExactDirectReferenceMatch: true,
      childReturnCode: guard.returnCode, remainingOwnedGroupMembers: guard.remainingOwnedGroupMembers,
      productWorker: path.relative(root, path.join(evidenceDir, 'cpu-product-worker.json')),
      productGuard: path.relative(root, path.join(evidenceDir, 'cpu-product.guard.json')) },
    pageErrors,
  };
  await fs.writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`);
  console.log(JSON.stringify({ status: report.status, runId, actualEngine: execution.actualEngine,
    reportPath: path.relative(root, reportPath) }, null, 2));
} finally {
  await context.close();
  await browser.close();
}
