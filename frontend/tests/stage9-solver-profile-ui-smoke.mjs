/** Read-only browser smoke for the deployed solver-profile GPU gate. */
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const baseUrl = (process.env.WORKBENCH_URL ?? 'http://127.0.0.1:8789').replace(/\/$/, '');
const caseDirs = [
  path.join(root, 'docs/stage9-evidence/gpu-coverage-audit/multiparticle-species-probe/operator-split/geometry-scope/2d-plane-two-ion'),
  path.join(root, 'docs/stage9-evidence/gpu-coverage-audit/multiparticle-species-probe/operator-split/geometry-scope/2d-trench-two-ion-cfl-0125-ray64'),
  path.join(root, 'docs/stage9-evidence/gpu-coverage-audit/multiparticle-species-probe/operator-split/geometry-scope/2d-trench-two-ion-cfl-025-ray1024'),
  path.join(root, 'docs/stage9-evidence/gpu-coverage-audit/geometry-presets/multiParticle-2d-trench-direct-etch-gpu_disk'),
  path.join(root, 'docs/stage9-evidence/gpu-coverage-audit/multiparticle-species-probe/operator-split/geometry-scope/3d-plane-two-ion-cfl-025-ray64'),
  path.join(root, 'docs/stage9-evidence/gpu-coverage-audit/multiparticle-species-probe/operator-split/geometry-scope/3d-trench-two-ion-cfl-0125-ray64'),
  path.join(root, 'docs/stage9-evidence/gpu-coverage-audit/multiparticle-species-probe/operator-split/geometry-scope/3d-trench-two-ion-cfl-0125-ray256'),
  path.join(root, 'docs/stage9-evidence/gpu-coverage-audit/multiparticle-species-probe/operator-split/geometry-scope/3d-trench-two-ion-cfl-0125-ray512'),
  path.join(root, 'docs/stage9-evidence/gpu-coverage-audit/multiparticle-species-probe/operator-split/geometry-scope/3d-trench-two-ion-cfl-015-ray1024'),
  path.join(root, 'docs/stage9-evidence/gpu-coverage-audit/multiparticle-species-probe/operator-split/geometry-scope/3d-trench-two-ion-cfl-015-ray512'),
  path.join(root, 'docs/stage9-evidence/gpu-coverage-audit/geometry-presets/multiParticle-3d-trench-direct-etch-gpu_disk'),
  path.join(root, 'docs/stage9-evidence/gpu-coverage-audit/multiparticle-species-probe/operator-split/geometry-scope/3d-hole-two-ion-cfl-005-ray1024'),
  path.join(root, 'docs/stage9-evidence/gpu-coverage-audit/multiparticle-species-probe/operator-split/geometry-scope/3d-hole-two-ion-cfl-0125-ray1024'),
];
const RTX_UUID = 'GPU-5532bb0f-735e-bc93-98ca-b7662a8ebf6d';

async function api(route) {
  const response = await fetch(`${baseUrl}${route}`);
  assert.equal(response.status, 200, `${route} returned ${response.status}`);
  return response.json();
}

const projectsBefore = await api('/api/projects');
const runsBefore = await api('/api/runs');
const pageErrors = [];
let knownFailureGate = null;
const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const page = await browser.newPage({ viewport: { width: 1680, height: 1100 } });
page.setDefaultTimeout(30000);
page.on('pageerror', error => pageErrors.push({ message: error.message, stack: error.stack }));
const results = [];

try {
  await page.goto(`${baseUrl}/`);
  await page.getByText('本机服务已连接').waitFor();
  for (const caseDir of caseDirs) {
    const projectPath = path.join(caseDir, 'project.json');
    const project = JSON.parse(await fs.readFile(projectPath, 'utf8'));
    const validate = page.waitForResponse(response => response.url().endsWith('/api/projects/validate')
      && response.request().method() === 'POST');
    await page.locator('nav[aria-label="项目操作"] input[type="file"]').setInputFiles(projectPath);
    assert.equal((await validate).status(), 200);
    await page.waitForFunction(name => document.querySelector('input[aria-label="项目名称"]')?.value === name, project.name);
    await page.locator('.activity-rail').getByRole('button', { name: '设置', exact: true }).click();
    const preference = page.getByLabel('引擎偏好', { exact: true });
    const gpuOption = preference.locator('option[value="gpu"]');
    assert.equal(await gpuOption.isDisabled(), false, `${project.geometry.kind} measured profile should enable GPU`);
    assert.equal(await page.getByLabel('计算 GPU', { exact: true }).inputValue(), RTX_UUID);
    const supportedRatio = project.solver.timeStepRatio;
    const representativeRatio = 0.1;
    const ratio = page.getByLabel('时间步比例', { exact: true });
    await ratio.fill(String(representativeRatio));
    await ratio.blur();
    await page.waitForFunction(() => document.querySelector('select[aria-label="引擎偏好"] option[value="gpu"]')?.disabled === false);
    const rays = page.getByLabel('每点射线数', { exact: true });
    const supportedRays = await rays.inputValue();
    await rays.fill('128');
    await rays.blur();
    await page.waitForFunction(() => document.querySelector('select[aria-label="引擎偏好"] option[value="gpu"]')?.disabled === false);
    await rays.fill(supportedRays);
    await rays.blur();
    await ratio.fill(String(supportedRatio));
    await ratio.blur();
    await page.waitForFunction(() => document.querySelector('select[aria-label="引擎偏好"] option[value="gpu"]')?.disabled === false);
    results.push({ project: path.relative(root, projectPath), dimension: project.dimension, geometry: project.geometry.kind,
      supportedTimeStepRatio: supportedRatio, raysPerPoint: project.solver.raysPerPoint,
      randomMode: project.solver.random.mode, representativeTimeStepRatio: representativeRatio,
      selectedGpuUuid: await page.getByLabel('计算 GPU', { exact: true }).inputValue(),
      representativeTimeStepEnabled: true, representativeRayCountEnabled: true });
  }
  const knownFailurePath = path.join(root, 'docs/stage9-evidence/gpu-coverage-audit/multiparticle-species-probe/operator-split/geometry-scope/3d-trench-two-ion-cfl-0125-ray512/project.json');
  const knownFailureProject = JSON.parse(await fs.readFile(knownFailurePath, 'utf8'));
  const knownFailureImport = page.waitForResponse(response => response.url().endsWith('/api/projects/validate')
    && response.request().method() === 'POST');
  await page.locator('nav[aria-label="项目操作"] input[type="file"]').setInputFiles(knownFailurePath);
  assert.equal((await knownFailureImport).status(), 200);
  await page.waitForFunction(name => document.querySelector('input[aria-label="项目名称"]')?.value === name, knownFailureProject.name);
  await page.locator('.activity-rail').getByRole('button', { name: '设置', exact: true }).click();
  await page.getByLabel('时间步比例', { exact: true }).fill('0.2');
  await page.getByLabel('时间步比例', { exact: true }).blur();
  await page.getByLabel('每点射线数', { exact: true }).fill('1024');
  await page.getByLabel('每点射线数', { exact: true }).blur();
  await page.waitForFunction(() => document.querySelector('select[aria-label="引擎偏好"] option[value="gpu"]')?.disabled === true);
  knownFailureProject.solver.timeStepRatio = 0.2;
  knownFailureProject.solver.raysPerPoint = 1024;
  const knownFailureResponse = await page.request.post(`${baseUrl}/api/runs`, {
    data: { kind: 'simulate', project: knownFailureProject },
  });
  assert.equal(knownFailureResponse.status(), 422, await knownFailureResponse.text());
  knownFailureGate = { dimension: 3, geometry: 'trench', timeStepRatio: 0.2,
    raysPerPoint: 1024, gpuOptionDisabled: true, validationStatus: knownFailureResponse.status() };
  assert.deepEqual(pageErrors, []);
} finally {
  await page.close();
  await browser.close();
}

const projectsAfter = await api('/api/projects');
const runsAfter = await api('/api/runs');
assert.deepEqual(projectsAfter.projects.map(row => row.id), projectsBefore.projects.map(row => row.id));
assert.deepEqual(runsAfter.runs.map(row => row.runId), runsBefore.runs.map(row => row.runId));
const report = { status: 'passed', service: baseUrl,
  readOnly: true, projectCountBefore: projectsBefore.projects.length,
  projectCountAfter: projectsAfter.projects.length, runCountBefore: runsBefore.runs.length,
  runCountAfter: runsAfter.runs.length, deviceUuid: RTX_UUID, cases: results, pageErrors };
report.knownFailureGate = knownFailureGate;
const output = path.join(root, 'docs/stage9-evidence/ui-refactor-alignment/production-8789-profile-smoke.json');
await fs.writeFile(output, `${JSON.stringify(report, null, 2)}\n`);
console.log(JSON.stringify({ status: report.status, output: path.relative(root, output), cases: results }, null, 2));
