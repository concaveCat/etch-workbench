/** Browser check of a schema-7 combination with a host-verified GPU_DISK row. */
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '../..');
const evidence = path.join(root, 'docs/cpu-thread-evidence');
const projectPath = path.join(evidence, 'schema7-multiparticle-gpu-project.json');
const directReferencePath = path.join(root, 'docs/stage9-evidence/gpu-coverage-audit/default-ui-multiparticle/direct.json');
const baseUrl = (process.env.WORKBENCH_URL ?? 'http://127.0.0.1:8789').replace(/\/$/, '');
const project = JSON.parse(await fs.readFile(path.join(root, 'docs/stage9-evidence/gpu-coverage-audit/default-ui-multiparticle/project.json'), 'utf8'));
project.name = `WebUI 默认多粒子 GPU_DISK 浏览器验收 ${new Date().toISOString()}`;
project.solver.enginePreference = 'cpu';
await fs.mkdir(evidence, { recursive: true });
await fs.writeFile(projectPath, `${JSON.stringify(project, null, 2)}\n`);
const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
const page = await context.newPage();
page.setDefaultTimeout(20000);
const pageErrors = [];
page.on('pageerror', error => pageErrors.push(error.message));

async function waitForRun(runId) {
  const deadline = Date.now() + 120000;
  while (Date.now() < deadline) {
    const run = await fetch(`${baseUrl}/api/runs/${runId}`, { cache: 'no-store' }).then(response => response.json());
    if (['succeeded', 'failed', 'cancelled', 'timed_out', 'interrupted'].includes(run.status)) {
      assert.equal(run.status, 'succeeded', run.failureReason ?? `GPU run ended as ${run.status}`);
      return run;
    }
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  throw new Error(`GPU run ${runId} did not finish in time`);
}

async function assertRunMatchesDirect(runId, directStep) {
  const response = await fetch(`${baseUrl}/api/runs/${runId}/snapshots/${project.steps[0].id}`);
  assert.equal(response.status, 200);
  const snapshot = await response.json();
  assert.deepEqual(snapshot.points, directStep.snapshot.points);
  assert.deepEqual(snapshot.lines, directStep.snapshot.cells);
  assert.deepEqual(snapshot.cellData.materialIds, directStep.snapshot.materialIds);
  assert.deepEqual(snapshot.materialIdsByLevelSetIndex, directStep.snapshot.materialIdsByLevelSetIndex);
  return snapshot;
}

try {
  await page.goto(`${baseUrl}/`);
  await page.getByText('本机服务已连接').waitFor();
  await page.locator('nav[aria-label="项目操作"] input[type="file"]').setInputFiles(projectPath);
  await page.waitForFunction(name => document.querySelector('input[aria-label="项目名称"]')?.value === name, project.name);
  const engineSelect = page.getByLabel('引擎偏好');
  const gpuOption = page.locator('select[aria-label="引擎偏好"] option[value="gpu"]');
  await page.waitForFunction(() => Boolean(document.querySelector('select[aria-label="引擎偏好"]')));
  assert.equal(await gpuOption.isDisabled(), false, 'the schema-7 project matching a verified GPU row must expose GPU_DISK');
  const preselectionNotice = await page.locator('.capability-note').innerText();
  assert.doesNotMatch(preselectionNotice, /尚未全部核实/);
  await engineSelect.selectOption('gpu');
  await page.waitForTimeout(500);

  const requestPromise = page.waitForRequest(request => request.url().endsWith('/api/runs') && request.method() === 'POST');
  const responsePromise = page.waitForResponse(response => response.url().endsWith('/api/runs') && response.request().method() === 'POST');
  await page.getByRole('button', { name: '运行工艺' }).click();
  const [request, response] = await Promise.all([requestPromise, responsePromise]);
  const sent = request.postDataJSON().project;
  assert.equal(sent.steps[0].species[0].label, 'argonIon');
  assert.equal(sent.steps[0].species[1].label, 'fluorineNeutral');
  assert.equal(sent.solver.enginePreference, 'gpu');
  assert.equal(response.status(), 202, await response.text());
  const submission = await response.json();
  const run = await waitForRun(submission.runId);
  const runDir = path.join(path.resolve(process.env.VIENNAPS_WORKBENCH_DATA_DIR), 'runs', submission.runId);
  const manifest = JSON.parse(await fs.readFile(path.join(runDir, 'manifest.json'), 'utf8'));
  const workerResult = JSON.parse(await fs.readFile(path.join(runDir, 'worker-result.json'), 'utf8'));
  assert.equal(manifest.actualExecution[0].actualEngine, 'GPU_DISK');
  assert.equal(manifest.actualExecution[0].requestedEnginePreference, 'gpu');
  assert.equal(workerResult.actualSteps[0].engine, 'GPU_DISK');
  assert.equal(workerResult.stepRecords[0].actualEngine, 'GPU_DISK');
  const direct = JSON.parse(await fs.readFile(directReferencePath, 'utf8'));
  const directStep = direct.steps[project.steps[0].id];
  const snapshot = await assertRunMatchesDirect(submission.runId, directStep);
  const callback = manifest.actualExecution[0].actualParameters.rateCallbackSummary;
  assert.equal(callback.invocationCount, directStep.callback.count);
  assert.deepEqual(callback.materials, directStep.callback.materials);

  const replayResponse = await fetch(`${baseUrl}/api/runs/${submission.runId}/replay-project`);
  assert.equal(replayResponse.status, 200);
  const replayEnvelope = await replayResponse.json();
  assert.equal(replayEnvelope.normalizedProject.solver.enginePreference, 'gpu');
  assert.equal(replayEnvelope.executionRequirements.steps[0].engine, 'GPU_DISK');
  const replaySubmissionResponse = await fetch(`${baseUrl}/api/runs/replay`, {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(replayEnvelope),
  });
  const replaySubmission = await replaySubmissionResponse.json();
  assert.equal(replaySubmissionResponse.status, 202, JSON.stringify(replaySubmission));
  const replay = await waitForRun(replaySubmission.runId);
  const replayManifest = JSON.parse(await fs.readFile(path.join(path.resolve(process.env.VIENNAPS_WORKBENCH_DATA_DIR), 'runs', replaySubmission.runId, 'manifest.json'), 'utf8'));
  assert.equal(replayManifest.actualExecution[0].actualEngine, 'GPU_DISK');
  await assertRunMatchesDirect(replaySubmission.runId, directStep);
  assert.deepEqual(pageErrors, []);

  const report = { status: 'passed', project: path.relative(root, projectPath), runId: submission.runId,
    replayRunId: replaySubmission.runId,
    schemaVersion: sent.schemaVersion, model: sent.steps[0].model, dimension: sent.dimension,
    operation: sent.steps[0].operation, actualEngine: manifest.actualExecution[0].actualEngine,
    runStatus: run.status, replayStatus: replay.status, gpuOptionEnabled: true,
    directReference: path.relative(root, directReferencePath), exactDirectComparison: true,
    rateCallbackCount: callback.invocationCount, preselectionNotice };
  await fs.writeFile(path.join(evidence, 'schema7-gpu-ui-acceptance.json'), `${JSON.stringify(report, null, 2)}\n`);
  console.log(JSON.stringify(report, null, 2));
} finally {
  await context.close();
  await browser.close();
}
