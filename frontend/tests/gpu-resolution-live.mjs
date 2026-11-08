/** Chromium acceptance for GPU_DISK input and 2D/3D resolutions above former caps. */
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { promisify } from 'node:util';
import { execFile } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const execFileAsync = promisify(execFile);
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const evidence = path.join(root, 'docs/stage9-evidence/gpu-coverage-audit/browser');
const baseUrl = (process.env.WORKBENCH_URL ?? 'http://127.0.0.1:8797').replace(/\/$/, '');
const python = path.join(root, '.venv/bin/python');
const input2d = path.join(root, 'docs/stage9-evidence/high-resolution/2d-above-former-resolution-limits.project.json');
const direct2d = path.join(root, 'docs/stage9-evidence/high-resolution/2d-above-former-resolution-limits.direct.json');
const source3d = path.join(root, 'docs/stage9-evidence/high-resolution/3d-above-former-resolution-limits.project.json');
await fs.mkdir(evidence, { recursive: true });

const project3d = JSON.parse(await fs.readFile(source3d, 'utf8'));
project3d.name = `Chromium 3D fine grid ${Date.now()}`;
project3d.domain.xExtent = 2.6;
project3d.domain.yExtent = 2.6;
const input3d = path.join(evidence, '3d-fine-grid.project.json');
const direct3d = path.join(evidence, '3d-fine-grid.direct.json');
await fs.writeFile(input3d, `${JSON.stringify(project3d, null, 2)}\n`);
await execFileAsync(python, [path.join(root, 'scripts/bounded_child.py'), '--cwd', root,
  '--record', path.join(evidence, '3d-fine-grid.direct.guard.json'),
  '--log-path', path.join(evidence, '3d-fine-grid.direct.log'), '--timeout-seconds', '120',
  '--max-rss-mib', '1536', '--max-log-bytes', '400000', '--', python,
  path.join(root, 'tests/fixtures/gpu_singleparticle_direct_reference.py'), input3d, direct3d],
{ cwd: root, timeout: 150000, maxBuffer: 1024 * 1024 });

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const context = await browser.newContext({ viewport: { width: 1680, height: 1150 } });
const page = await context.newPage();
page.setDefaultTimeout(30000);
const pageErrors = [];
const consoleErrors = [];
page.on('pageerror', error => pageErrors.push({ message: error.message, stack: error.stack }));
page.on('console', message => { if (message.type() === 'error') consoleErrors.push(message.text()); });

async function loadProject(inputPath, project) {
  await page.getByRole('navigation', { name: '项目操作' }).locator('input[type="file"]').setInputFiles(inputPath);
  await page.waitForFunction(({ name, delta, xExtent, dimension }) =>
    document.querySelector('input[aria-label="项目名称"]')?.value === name &&
    document.querySelector('input[aria-label="网格间距"]')?.value === String(delta) &&
    document.querySelector('input[aria-label="x 横向总宽度"]')?.value === String(xExtent) &&
    document.querySelector(`button[aria-label="切换到 ${dimension}D"]`)?.getAttribute('aria-pressed') === 'true',
  { name: project.name, delta: project.domain.gridDelta, xExtent: project.domain.xExtent, dimension: project.dimension });
  assert.equal(await page.getByLabel('网格间距', { exact: true }).inputValue(), String(project.domain.gridDelta));
  assert.equal(await page.getByLabel('x 横向总宽度', { exact: true }).inputValue(), String(project.domain.xExtent));
  if (project.dimension === 3)
    assert.equal(await page.getByLabel('y 横向总宽度', { exact: true }).inputValue(), String(project.domain.yExtent));
  const gpuOption = page.getByLabel('引擎偏好', { exact: true }).locator('option[value="gpu"]');
  await gpuOption.waitFor({ state: 'attached' });
  assert.equal(await gpuOption.isDisabled(), false, `GPU_DISK should be available for measured ${project.dimension}D input`);
  assert.equal(await page.getByLabel('引擎偏好', { exact: true }).inputValue(), 'gpu');
}

async function runFromUi(project, directPath) {
  const request = page.waitForRequest(item => item.url().endsWith('/api/runs') && item.method() === 'POST');
  const response = page.waitForResponse(item => item.url().endsWith('/api/runs') && item.request().method() === 'POST');
  await page.getByRole('button', { name: '运行工艺', exact: true }).click();
  const [sent, returned] = await Promise.all([request, response]);
  assert.equal(returned.status(), 202, await returned.text());
  const input = sent.postDataJSON();
  assert.equal(input.project.domain.gridDelta, project.domain.gridDelta);
  assert.equal(input.project.domain.xExtent, project.domain.xExtent);
  assert.equal(input.project.solver.enginePreference, 'gpu');
  const runId = (await returned.json()).runId;
  let info;
  const deadline = Date.now() + 120000;
  do {
    const result = await fetch(`${baseUrl}/api/runs/${runId}`, { cache: 'no-store' });
    assert.equal(result.status, 200);
    info = await result.json();
    if (['succeeded', 'failed', 'cancelled', 'timed_out', 'interrupted'].includes(info.status)) break;
    await new Promise(resolve => setTimeout(resolve, 200));
  } while (Date.now() < deadline);
  assert.equal(info.status, 'succeeded', info.failureReason ?? info.status);
  const stepId = project.steps[0].id;
  const initialResponse = await fetch(`${baseUrl}/api/runs/${runId}/snapshots/initial`, { cache: 'no-store' });
  const finalResponse = await fetch(`${baseUrl}/api/runs/${runId}/snapshots/${stepId}`, { cache: 'no-store' });
  assert.equal(initialResponse.status, 200);
  assert.equal(finalResponse.status, 200);
  const initial = await initialResponse.json();
  const final = await finalResponse.json();
  const execution = info.actualExecution.find(row => row.stepId === stepId);
  assert.equal(execution.actualEngine, 'GPU_DISK');
  const reference = JSON.parse(await fs.readFile(directPath, 'utf8'));
  function productSignature(snapshot) {
    return { points: snapshot.points, cells: project.dimension === 2 ? snapshot.lines : snapshot.triangles,
      materialIds: snapshot.cellData.materialIds, materialIdsByLevelSetIndex: snapshot.materialIdsByLevelSetIndex };
  }
  function directSignature(snapshot) {
    return { points: snapshot.points, cells: snapshot.cells, materialIds: snapshot.materialIds,
      materialIdsByLevelSetIndex: snapshot.materialIdsByLevelSetIndex };
  }
  assert.deepEqual(productSignature(initial), directSignature(reference.initial));
  assert.deepEqual(productSignature(final), directSignature(reference.final));
  return { runId, gridDelta: project.domain.gridDelta, xExtent: project.domain.xExtent,
    yExtent: project.domain.yExtent ?? null, actualEngine: execution.actualEngine, actualSeed: execution.actualSeed,
    pointCount: final.mesh.pointCount,
    cellCount: project.dimension === 2 ? final.mesh.lineCount : final.mesh.triangleCount,
    productDirectInitialAndFinalMatch: true, inputHash: info.requestedInputHash };
}

let report = { status: 'running', browser: 'Chromium', baseUrl, cases: [], pageErrors, consoleErrors };
try {
  await page.goto(`${baseUrl}/`);
  await page.getByText('本机服务已连接').waitFor();
  const project2d = JSON.parse(await fs.readFile(input2d, 'utf8'));
  await loadProject(input2d, project2d);
  report.cases.push({ dimension: 2, ...(await runFromUi(project2d, direct2d)) });
  await loadProject(input3d, project3d);
  report.cases.push({ dimension: 3, ...(await runFromUi(project3d, direct3d)) });
  assert.deepEqual(pageErrors, []);
  report.status = 'passed';
  await fs.writeFile(path.join(evidence, 'gpu-resolution-live.json'), `${JSON.stringify(report, null, 2)}\n`);
  console.log(JSON.stringify({ ...report, evidencePath: path.join(evidence, 'gpu-resolution-live.json') }, null, 2));
} finally {
  await context.close();
  await browser.close();
}
