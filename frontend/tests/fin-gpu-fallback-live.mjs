/** Browser/API acceptance for native Fin requests that safely use Boolean Fin. */
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const caseDir = path.join(root, 'docs/stage9-evidence/gpu-coverage-audit/specialized-geometries/sf6o2-2d-fin-direct-auto-fallback-gpu_disk');
const evidenceDir = path.join(root, 'docs/stage9-evidence/gpu-coverage-audit/browser');
const baseUrl = (process.env.WORKBENCH_URL ?? 'http://127.0.0.1:8803').replace(/\/$/, '');
const project = JSON.parse(await fs.readFile(path.join(caseDir, 'project.json'), 'utf8'));
const direct = JSON.parse(await fs.readFile(path.join(caseDir, 'direct.json'), 'utf8'));
const projectPath = path.join(evidenceDir, 'fin-auto-fallback-browser.project.json');
project.name = `Fin GPU safe fallback browser ${Date.now()}`;
await fs.mkdir(evidenceDir, { recursive: true });
await fs.writeFile(projectPath, `${JSON.stringify(project, null, 2)}\n`);

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const context = await browser.newContext({ viewport: { width: 1680, height: 1100 } });
const page = await context.newPage();
page.setDefaultTimeout(30000);
const pageErrors = [];
page.on('pageerror', error => pageErrors.push(error.message));

async function api(route, init) {
  const response = await fetch(`${baseUrl}${route}`, { cache: 'no-store', ...init });
  const body = await response.json();
  assert.ok(response.ok, `${route}: ${response.status} ${JSON.stringify(body)}`);
  return body;
}

async function waitForRun(runId) {
  const deadline = Date.now() + 120000;
  while (Date.now() < deadline) {
    const run = await api(`/api/runs/${runId}`);
    if (['succeeded', 'failed', 'cancelled', 'timed_out', 'interrupted'].includes(run.status)) {
      assert.equal(run.status, 'succeeded', run.failureReason ?? `run ${runId} ended ${run.status}`);
      return run;
    }
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  throw new Error(`run ${runId} timed out`);
}

function compareSnapshot(snapshot, reference) {
  assert.deepEqual(snapshot.points, reference.points);
  assert.deepEqual(snapshot.lines, reference.cells);
  assert.deepEqual(snapshot.cellData.materialIds, reference.materialIds);
  assert.deepEqual(snapshot.materialIdsByLevelSetIndex, reference.materialIdsByLevelSetIndex);
}

let report = { status: 'running', baseUrl, project: path.relative(root, projectPath), pageErrors };
try {
  await page.goto(`${baseUrl}/`);
  await page.getByText('本机服务已连接').waitFor();
  await page.locator('nav[aria-label="项目操作"] input[type="file"]').setInputFiles(projectPath);
  await page.waitForFunction(name => document.querySelector('input[aria-label="项目名称"]')?.value === name, project.name);

  const gpuOption = page.locator('select[aria-label="引擎偏好"] option[value="gpu"]');
  assert.equal(await gpuOption.evaluate(option => option.disabled), false,
    'GPU must be exposed for the effective Boolean Fin signature');
  const geometryNote = await page.locator('.geometry-fields').innerText();
  assert.match(geometryNote, /安全构造回退.*将使用.*布尔 Fin/s);
  await page.getByLabel('引擎偏好', { exact: true }).selectOption('gpu');

  const requestPromise = page.waitForRequest(request => request.url().endsWith('/api/runs') && request.method() === 'POST');
  const responsePromise = page.waitForResponse(response => response.url().endsWith('/api/runs') && response.request().method() === 'POST');
  await page.getByRole('button', { name: '运行工艺', exact: true }).click();
  const [request, response] = await Promise.all([requestPromise, responsePromise]);
  assert.equal(response.status(), 202, await response.text());
  const sentProject = request.postDataJSON().project;
  assert.equal(sentProject.geometry.construction, 'native', 'UI input retains the requested legacy construction');
  assert.equal(sentProject.solver.enginePreference, 'gpu');
  const runId = (await response.json()).runId;
  const run = await waitForRun(runId);
  const initial = await api(`/api/runs/${runId}/snapshots/initial`);
  const final = await api(`/api/runs/${runId}/snapshots/${encodeURIComponent(sentProject.steps[0].id)}`);
  const geometryReadback = initial.geometryReadback;
  assert.equal(geometryReadback.requestedConstruction, 'native');
  assert.equal(geometryReadback.construction, 'boolean');
  assert.equal(geometryReadback.automaticFallback, true);
  assert.deepEqual(geometryReadback.fallbackModels, ['sf6o2']);
  compareSnapshot(initial, direct.initial);
  compareSnapshot(final, direct.final);
  const execution = run.actualExecution.find(row => row.stepId === sentProject.steps[0].id);
  assert.equal(execution.actualEngine, 'GPU_DISK');

  await page.getByRole('button', { name: '保存项目', exact: true }).click();
  await page.waitForTimeout(300);
  const saved = (await api('/api/projects')).projects.find(row => row.name === project.name);
  assert.ok(saved, 'native Fin request should save after a fallback run');
  const reopened = await api(`/api/projects/${saved.id}`);
  assert.equal(reopened.project.geometry.construction, 'native');

  const replay = await api(`/api/runs/${runId}/replay-project`);
  assert.equal(replay.normalizedProject.geometry.construction, 'native');
  const replaySubmission = await api('/api/runs/replay', {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(replay),
  });
  const replayRun = await waitForRun(replaySubmission.runId);
  const replayInitial = await api(`/api/runs/${replaySubmission.runId}/snapshots/initial`);
  const replayFinal = await api(`/api/runs/${replaySubmission.runId}/snapshots/${encodeURIComponent(sentProject.steps[0].id)}`);
  compareSnapshot(replayInitial, direct.initial);
  compareSnapshot(replayFinal, direct.final);
  assert.equal(replayRun.actualExecution[0].actualEngine, 'GPU_DISK');
  assert.deepEqual(pageErrors, []);

  report = {
    ...report, status: 'passed', runId, replayRunId: replaySubmission.runId,
    projectGeometryRequest: sentProject.geometry.construction,
    actualGeometryConstruction: geometryReadback.construction,
    fallbackModels: geometryReadback.fallbackModels,
    engine: execution.actualEngine, initialAndFinalMatchDirectViennaPS: true,
    replayMatchesDirectViennaPS: true, savedProjectId: saved.id, pageErrors,
  };
} finally {
  await context.close();
  await browser.close();
  await fs.writeFile(path.join(evidenceDir, 'fin-auto-fallback-live.json'), `${JSON.stringify(report, null, 2)}\n`);
}
console.log(JSON.stringify(report, null, 2));
