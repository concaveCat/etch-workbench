/** Real browser + isolated local service + bounded independent ViennaPS reference. */
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '../..');
const evidence = path.join(root, 'docs/cpu-thread-evidence');
const dataDir = path.resolve(process.env.VIENNAPS_WORKBENCH_DATA_DIR ?? path.join(root, 'var/cpu-thread-live-data'));
const baseUrl = (process.env.WORKBENCH_URL ?? 'http://127.0.0.1:8789').replace(/\/$/, '');
const projectPath = path.join(evidence, 'cpu-maximum-project.json');
const referencePath = path.join(evidence, 'cpu-maximum-direct-reference.json');
const fixture = path.join(root, 'tests/fixtures/materials_direct_reference.py');
const python = path.join(root, '.venv/bin/python');

await fs.mkdir(evidence, { recursive: true });
const capabilityResponse = await fetch(`${baseUrl}/api/capabilities`);
assert.equal(capabilityResponse.status, 200);
const { resourcePolicy } = await capabilityResponse.json();
const cpuPolicy = resourcePolicy.run.cpuThreads;
const maximum = cpuPolicy.maximum;
assert.ok(Number.isSafeInteger(maximum) && maximum >= 1, `invalid dynamic CPU maximum: ${maximum}`);

const project = {
  schemaVersion: 3,
  name: `CPU 最大线程验收 ${new Date().toISOString()}`,
  dimension: 2,
  units: { length: 'um', time: 's', rate: 'um/s', angle: 'deg' },
  domain: { gridDelta: 0.2, xExtent: 4, xBoundary: 'reflective' },
  geometry: { kind: 'plane', height: 0, substrateMaterial: 'Si' },
  steps: [{ id: 'step_cpu_threads_max', name: '最大线程原生刻蚀', enabled: true,
    operation: 'etch', model: 'isotropic', rateMagnitude: 0.2, duration: 0.4 }],
  solver: { timeStepRatio: 0.25, raysPerPoint: 32, cpuThreads: 1,
    random: { mode: 'fixed', seed: 314159 }, enginePreference: 'cpu', timeoutSeconds: 90 },
  view: { panX: 0, panH: 0, zoom: 1, showGrid: true },
};
await fs.writeFile(projectPath, `${JSON.stringify(project, null, 2)}\n`);

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
  assert.ok(terminal.has(run?.status), `run ${runId} did not finish in 120 seconds`);
  assert.equal(run.status, 'succeeded', run.failureReason ?? `run ended as ${run.status}`);
  return run;
}

async function startFromUi() {
  const requestPromise = page.waitForRequest(request => request.url().endsWith('/api/runs') && request.method() === 'POST');
  const responsePromise = page.waitForResponse(response => response.url().endsWith('/api/runs') && response.request().method() === 'POST');
  await page.getByRole('button', { name: '运行工艺' }).click();
  const [request, response] = await Promise.all([requestPromise, responsePromise]);
  assert.equal(response.status(), 202, await response.text());
  return { sent: request.postDataJSON().project, runId: (await response.json()).runId };
}

function assertSnapshotMatchesReference(actual, expected, label) {
  const lines = actual.lines;
  assert.equal(actual.points.length, expected.points.length, `${label}: point count`);
  assert.equal(lines.length, expected.lines.length, `${label}: line count`);
  assert.deepEqual(actual.cellData.materialIds, expected.materialIds, `${label}: cell material IDs`);
  assert.deepEqual(actual.materialIdsByLevelSetIndex, expected.materialMap, `${label}: level-set material map`);
  let maxNearestCoordinateDistance = 0;
  for (const point of actual.points) {
    const distance = Math.min(...expected.points.map(other => Math.hypot(...point.map((value, index) => value - other[index]))));
    maxNearestCoordinateDistance = Math.max(maxNearestCoordinateDistance, distance);
  }
  assert.ok(maxNearestCoordinateDistance <= 1e-8, `${label}: max nearest point distance ${maxNearestCoordinateDistance}`);
  return { pointCount: actual.points.length, lineCount: lines.length, maxNearestCoordinateDistance };
}

try {
  await page.goto(`${baseUrl}/`);
  await page.getByText('本机服务已连接').waitFor();
  await page.locator('nav[aria-label="项目操作"] input[type=file]').setInputFiles(projectPath);
  await page.waitForFunction(name => document.querySelector('input[aria-label="项目名称"]')?.value === name, project.name);

  const cpuInput = page.getByLabel('CPU 线程数');
  await page.waitForFunction(expected => {
    const input = document.querySelector('input[aria-label="CPU 线程数"]');
    return input?.max === String(expected) && !input.disabled;
  }, maximum);
  assert.equal(await cpuInput.getAttribute('max'), String(maximum));
  await cpuInput.fill(String(maximum));
  await cpuInput.press('Tab');
  await page.waitForFunction(expected => document.querySelector('input[aria-label="CPU 线程数"]')?.value === String(expected), maximum);

  const saveRequestPromise = page.waitForRequest(request => request.url().endsWith('/api/projects') && request.method() === 'POST');
  const saveResponsePromise = page.waitForResponse(response => response.url().endsWith('/api/projects') && response.request().method() === 'POST');
  await page.getByRole('button', { name: '保存项目' }).click();
  const [saveRequest, saveResponse] = await Promise.all([saveRequestPromise, saveResponsePromise]);
  assert.equal(saveResponse.status(), 201, await saveResponse.text());
  const saved = await saveResponse.json();
  assert.equal(saveRequest.postDataJSON().solver.cpuThreads, maximum);
  assert.equal(saved.project.solver.cpuThreads, maximum);

  await page.getByLabel('已保存项目').selectOption(saved.id);
  await page.getByRole('button', { name: '打开', exact: true }).click();
  await page.waitForFunction(expected => document.querySelector('input[aria-label="CPU 线程数"]')?.value === String(expected), maximum);
  const exported = await page.evaluate(async () => {
    const response = await fetch('/api/projects');
    return response.json();
  });
  assert.ok(exported.projects.some(row => row.id === saved.id));

  const submitted = await startFromUi();
  assert.equal(submitted.sent.solver.cpuThreads, maximum);
  const run = await waitForRun(submitted.runId);
  const runDir = path.join(dataDir, 'runs', submitted.runId);
  const manifest = JSON.parse(await fs.readFile(path.join(runDir, 'manifest.json'), 'utf8'));
  const workerInput = JSON.parse(await fs.readFile(path.join(runDir, 'input.json'), 'utf8'));
  const workerResult = JSON.parse(await fs.readFile(path.join(runDir, 'worker-result.json'), 'utf8'));
  const childRecord = JSON.parse(await fs.readFile(path.join(runDir, 'child-record.json'), 'utf8'));
  assert.equal(manifest.cpuThreads, maximum);
  assert.equal(workerInput.project.solver.cpuThreads, maximum);
  assert.equal(workerResult.cpuThreads, maximum);
  assert.equal(childRecord.returnCode, 0);
  assert.deepEqual(childRecord.remainingOwnedGroupMembers, []);

  const direct = spawnSync(python, [
    path.join(root, 'scripts/bounded_child.py'), '--cwd', root,
    '--record', path.join(evidence, 'direct-reference-guard.json'),
    '--log-path', path.join(evidence, 'direct-reference.log'),
    '--timeout-seconds', '90', '--max-rss-mib', '2048', '--max-log-bytes', '4194304',
    '--terminate-grace-seconds', '2', '--', python, fixture, projectPath, referencePath,
  ], { cwd: root, encoding: 'utf8', timeout: 110000 });
  assert.equal(direct.status, 0, direct.stderr || direct.stdout);
  const directResult = JSON.parse(await fs.readFile(referencePath, 'utf8'));
  const actualSnapshot = await fetch(`${baseUrl}/api/runs/${submitted.runId}/snapshots/${project.steps[0].id}`).then(response => response.json());
  const geometryComparison = assertSnapshotMatchesReference(actualSnapshot, directResult[project.steps[0].id], 'CPU max-thread process result');

  const replayEnvelope = await fetch(`${baseUrl}/api/runs/${submitted.runId}/replay-project`).then(response => response.json());
  assert.equal(replayEnvelope.normalizedProject.solver.cpuThreads, maximum);
  assert.equal(replayEnvelope.executionRequirements.cpuThreads, maximum);
  const replayResponse = await fetch(`${baseUrl}/api/runs/replay`, {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(replayEnvelope),
  });
  const replaySubmission = await replayResponse.json();
  assert.equal(replayResponse.status, 202, JSON.stringify(replaySubmission));
  const replayId = replaySubmission.runId;
  const replay = await waitForRun(replayId);
  const replayManifest = JSON.parse(await fs.readFile(path.join(dataDir, 'runs', replayId, 'manifest.json'), 'utf8'));
  assert.equal(replayManifest.cpuThreads, maximum);
  const replaySnapshot = await fetch(`${baseUrl}/api/runs/${replayId}/snapshots/${project.steps[0].id}`).then(response => response.json());
  assertSnapshotMatchesReference(replaySnapshot, directResult[project.steps[0].id], 'CPU max-thread replay result');

  assert.deepEqual(pageErrors, [], `browser errors: ${pageErrors.join('; ')}`);
  const report = {
    status: 'passed', maximumCpuThreads: maximum, limitSource: cpuPolicy.limitSource,
    sourceProject: path.relative(root, projectPath), runId: submitted.runId, replayRunId: replayId,
    versions: manifest.versions, boundedRunReturnCode: childRecord.returnCode,
    nativeWorkerReportedCpuThreads: workerResult.cpuThreads,
    directReferenceGuard: path.relative(root, path.join(evidence, 'direct-reference-guard.json')),
    geometryComparison,
    replayStatus: replay.status,
  };
  await fs.writeFile(path.join(evidence, 'cpu-maximum-acceptance.json'), `${JSON.stringify(report, null, 2)}\n`);
  console.log(JSON.stringify(report, null, 2));
} finally {
  await context.close();
  await browser.close();
}
