/** Real Chromium GPU acceptance for measured GDS single- and multi-layer routes. */
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { promisify } from 'node:util';
import { execFile } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const execFileAsync = promisify(execFile);
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const evidenceRoot = path.join(root, 'docs/stage9-evidence/gpu-coverage-audit/browser/gds-gpu');
const baseUrl = (process.env.WORKBENCH_URL ?? 'http://127.0.0.1:8803').replace(/\/$/, '');
const dataDir = path.resolve(process.env.VIENNAPS_WORKBENCH_DATA_DIR ?? path.join(root, 'var/browser-8803'));
const fixture = path.join(root, 'docs/stage5-evidence/two-structure-two-layer.gds');
const multilayerEvidence = path.join(root, 'docs/stage9-evidence/gpu-coverage-audit/gds-multilayer');
const multilayerSummary = JSON.parse(await fs.readFile(path.join(multilayerEvidence, 'summary.json'), 'utf8'));
const layerCountEvidence = path.join(root, 'docs/stage9-evidence/gpu-coverage-audit/gds-layer-count/grid-aligned-0.1');
const layerCountSummary = JSON.parse(await fs.readFile(path.join(layerCountEvidence, 'summary.json'), 'utf8'));
const materialRoot = path.join(root, 'docs/stage9-evidence/gpu-coverage-audit/gds-geometry/material-scope');
const cases = [
  { name: '2d-film-w-reflective', project: path.join(materialRoot, 'singleParticle-2d-gds-CELL_A-film-W-reflective-etch-gpu_disk/project.json'),
    direct: path.join(materialRoot, 'singleParticle-2d-gds-CELL_A-film-W-reflective-etch-gpu_disk/direct.json'),
    materialLabel: '图层 1 的材料', material: 'W', mask: false },
  { name: '3d-mask-sio2-periodic', project: path.join(materialRoot, 'singleParticle-3d-gds-CELL_B-masked-mask-SiO2-periodic-etch-gpu_disk/project.json'),
    direct: path.join(materialRoot, 'singleParticle-3d-gds-CELL_B-masked-mask-SiO2-periodic-etch-gpu_disk/direct.json'),
    materialLabel: '图层 2 的材料', material: 'SiO2', mask: true },
  { name: '2d-two-layer-film-film', project: path.join(root, 'docs/stage9-evidence/gpu-coverage-audit/gds-multilayer/singleParticle-2d-gds-MULTI-two-layer-two-film-distinct-reflective-etch-gpu_disk/project.json'),
    direct: path.join(root, 'docs/stage9-evidence/gpu-coverage-audit/gds-multilayer/singleParticle-2d-gds-MULTI-two-layer-two-film-distinct-reflective-etch-gpu_disk/direct.json'),
    multiLayer: true },
  { name: '3d-two-layer-film-mask', project: path.join(root, 'docs/stage9-evidence/gpu-coverage-audit/gds-multilayer/singleParticle-3d-gds-MULTI-masked-two-layer-film-mask-distinct-periodic-etch-gpu_disk/project.json'),
    direct: path.join(root, 'docs/stage9-evidence/gpu-coverage-audit/gds-multilayer/singleParticle-3d-gds-MULTI-masked-two-layer-film-mask-distinct-periodic-etch-gpu_disk/direct.json'),
    multiLayer: true },
];
const multilayerFixture = path.join(multilayerEvidence, 'multilayer-two-adjacent-polygons.gds');
const maximumLayerCase = layerCountSummary.rows.find(row => row.dimension === 3
  && row.model === 'singleParticle' && row.operation === 'etch' && row.boundaryXY === 'periodic'
  && row.gdsLayerMappings.every(mapping => !mapping.mask));
assert.ok(maximumLayerCase, 'the accepted 64-layer 3D periodic film case is required');
cases.push({ name: '3d-periodic-64-mappings', project: path.join(root, maximumLayerCase.project),
  direct: path.join(root, maximumLayerCase.directReference), multiLayer: true, maximumLayer: true });
const maximumLayerFixture = path.join(layerCountEvidence, 'sixty-four-layer-mapping.gds');
const specializedGdsEvidence = path.join(root, 'docs/stage9-evidence/gpu-coverage-audit/gds-specialized');
cases.push({ name: '3d-teos-gds-distinct-deposition-and-mask-materials',
  project: path.join(specializedGdsEvidence, 'teosDeposition-3d-gds-two-layer-film-mask-reflective-gpu_disk/project.json'),
  direct: path.join(specializedGdsEvidence, 'teosDeposition-3d-gds-two-layer-film-mask-reflective-gpu_disk/direct.json'),
  multiLayer: true });
const knownPeriodicFailureProject = path.join(specializedGdsEvidence,
  'sf6o2-3d-gds-film-mask-periodic-gpu_disk/project.json');
assert.equal(multilayerSummary.passed, true);
assert.equal(multilayerSummary.caseCount, 80);
assert.equal(layerCountSummary.passed, true);
assert.equal(layerCountSummary.mappingCount, 64);
assert.equal(layerCountSummary.caseCount, 32);

await fs.mkdir(evidenceRoot, { recursive: true });
const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const context = await browser.newContext({ acceptDownloads: true, viewport: { width: 1680, height: 1100 } });
const page = await context.newPage();
page.setDefaultTimeout(30000);
const pageErrors = [];
const consoleErrors = [];
const failedRequests = [];
page.on('pageerror', error => pageErrors.push({ message: error.message, stack: error.stack }));
page.on('console', message => { if (message.type() === 'error') consoleErrors.push(message.text()); });
page.on('requestfailed', request => failedRequests.push({ url: request.url(), error: request.failure()?.errorText }));

async function waitForRun(runId) {
  const deadline = Date.now() + 120000;
  let run;
  while (Date.now() < deadline) {
    const response = await fetch(`${baseUrl}/api/runs/${runId}`, { cache: 'no-store' });
    assert.equal(response.status, 200);
    run = await response.json();
    if (['succeeded', 'failed', 'cancelled', 'timed_out', 'interrupted'].includes(run.status)) break;
    await new Promise(resolve => setTimeout(resolve, 200));
  }
  assert.equal(run?.status, 'succeeded', run?.failureReason ?? `run ${runId} did not finish`);
  return run;
}

function productSignature(snapshot) {
  return { points: snapshot.points,
    topology: snapshot.dimension === 3 ? { triangles: snapshot.triangles } : { lines: snapshot.lines },
    materialIds: snapshot.cellData.materialIds,
    materialIdsByLevelSetIndex: snapshot.materialIdsByLevelSetIndex };
}

function directSignature(snapshot, dimension) {
  return { points: snapshot.points,
    topology: dimension === 3 ? { triangles: snapshot.cells } : { lines: snapshot.cells },
    materialIds: snapshot.materialIds,
    materialIdsByLevelSetIndex: snapshot.materialIdsByLevelSetIndex };
}

async function checkRun(caseRow) {
  const project = JSON.parse(await fs.readFile(caseRow.project, 'utf8'));
  const direct = JSON.parse(await fs.readFile(caseRow.direct, 'utf8'));
  const projectCopy = path.join(evidenceRoot, `${caseRow.name}.project.json`);
  project.name = `GDS GPU ${caseRow.name} ${Date.now()}`;
  await fs.writeFile(projectCopy, `${JSON.stringify(project, null, 2)}\n`);
  await page.locator('input[type="file"][accept*="application/json"]').setInputFiles(projectCopy);
  await page.waitForFunction(name => document.querySelector('input[aria-label="项目名称"]')?.value === name, project.name);
  for (const mapping of project.geometry.layers) {
    await page.getByLabel(`图层 ${mapping.layer} 的材料`, { exact: true }).selectOption(mapping.material);
    if (project.dimension === 3) {
      const mask = page.getByLabel(`GDS 图层 ${mapping.layer} 是掩膜`, { exact: true });
      assert.equal(await mask.isChecked(), Boolean(mapping.mask));
    }
  }
  const gpuOption = page.getByLabel('引擎偏好', { exact: true }).locator('option[value="gpu"]');
  assert.equal(await gpuOption.isDisabled(), false, `GPU must be exposed for measured ${caseRow.name} input`);
  const capabilityNotice = await page.locator('.capability-note').textContent();
  assert.doesNotMatch(capabilityNotice ?? '', /尚未全部核实/);
  await page.getByLabel('引擎偏好', { exact: true }).selectOption('gpu');

  const requestPromise = page.waitForRequest(request => request.url().endsWith('/api/runs') && request.method() === 'POST');
  const responsePromise = page.waitForResponse(response => response.url().endsWith('/api/runs') && response.request().method() === 'POST');
  await page.getByRole('button', { name: '运行工艺', exact: true }).click();
  const [request, response] = await Promise.all([requestPromise, responsePromise]);
  assert.equal(response.status(), 202, response.status() === 202 ? '' : await response.text());
  const sent = request.postDataJSON();
  assert.equal(sent.project.dimension, project.dimension);
  assert.equal(sent.project.solver.enginePreference, 'gpu');
  assert.equal(sent.project.geometry.kind, 'gds');
  assert.equal(sent.project.geometry.layers.length, project.geometry.layers.length);
  for (const expected of project.geometry.layers) {
    const actual = sent.project.geometry.layers.find(mapping => mapping.layer === expected.layer);
    assert.ok(actual, `UI omitted measured GDS layer ${expected.layer}`);
    assert.equal(actual.material, expected.material);
    assert.equal(Boolean(actual.mask), Boolean(expected.mask));
    for (const key of ['baseHeight', 'height']) if (key in expected) assert.equal(actual[key], expected[key]);
  }
  const runId = (await response.json()).runId;
  const run = await waitForRun(runId);
  const runDirectory = path.join(dataDir, 'runs', runId);
  const manifest = JSON.parse(await fs.readFile(path.join(runDirectory, 'manifest.json'), 'utf8'));
  const worker = JSON.parse(await fs.readFile(path.join(runDirectory, 'worker-result.json'), 'utf8'));
  assert.equal(manifest.actualExecution[0].actualEngine, 'GPU_DISK');
  assert.equal(worker.stepRecords[0].actualEngine, 'GPU_DISK');
  const initial = await fetch(`${baseUrl}/api/runs/${runId}/snapshots/initial`).then(async response => {
    assert.equal(response.status, 200); return response.json();
  });
  const final = await fetch(`${baseUrl}/api/runs/${runId}/snapshots/${encodeURIComponent(project.steps[0].id)}`).then(async response => {
    assert.equal(response.status, 200); return response.json();
  });
  const directInitial = direct.initial;
  const directFinal = direct.final;
  assert.deepEqual(productSignature(initial), directSignature(directInitial, project.dimension));
  assert.deepEqual(productSignature(final), directSignature(directFinal, project.dimension));
  return { name: caseRow.name, runId, runStatus: run.status, actualEngine: manifest.actualExecution[0].actualEngine,
    dimension: project.dimension, operation: project.steps[0].operation, model: project.steps[0].model,
    mappings: sent.project.geometry.layers, boundaryXY: project.domain.boundaryXY ?? project.domain.xBoundary,
    initialAndFinalExactlyMatchDirect: true, directReference: path.relative(root, caseRow.direct),
    productProject: path.relative(root, projectCopy), capabilityNotice };
}

let report = { status: 'running', service: baseUrl, versions: { viennaps: '4.6.2', viennals: '5.8.5' }, cases: [] };
try {
  await page.goto(`${baseUrl}/`);
  await page.getByText('本机服务已连接').waitFor();
  const uploadPromise = page.waitForResponse(response => response.url().includes('/api/gds-assets?')
    && response.request().method() === 'POST');
  await page.locator('input[type="file"][accept*=".gds"]').setInputFiles(fixture);
  const uploaded = await uploadPromise;
  assert.equal(uploaded.status(), 201, uploaded.status() === 201 ? '' : await uploaded.text());
  const expectedAssetId = JSON.parse(await fs.readFile(cases[0].project, 'utf8')).geometry.assetId;
  const uploadedAsset = await fetch(`${baseUrl}/api/gds-assets/${expectedAssetId}`).then(async response => {
    if (response.status !== 200) assert.fail(`GDS asset lookup returned ${response.status}: ${await response.text()}`);
    return response.json();
  });
  assert.equal(uploadedAsset.assetId, expectedAssetId);
  const multilayerUploadPromise = page.waitForResponse(response => response.url().includes('/api/gds-assets?')
    && response.request().method() === 'POST');
  await page.locator('input[type="file"][accept*=".gds"]').setInputFiles(multilayerFixture);
  const multilayerUploaded = await multilayerUploadPromise;
  assert.equal(multilayerUploaded.status(), 201, multilayerUploaded.status() === 201 ? '' : await multilayerUploaded.text());
  const multilayerProject = JSON.parse(await fs.readFile(cases.find(row => row.multiLayer).project, 'utf8'));
  const multilayerAsset = await fetch(`${baseUrl}/api/gds-assets/${multilayerProject.geometry.assetId}`).then(async response => {
    if (response.status !== 200) assert.fail(`multi-layer GDS asset lookup returned ${response.status}: ${await response.text()}`);
    return response.json();
  });
  assert.equal(multilayerAsset.assetId, multilayerProject.geometry.assetId);
  const maximumLayerUploadPromise = page.waitForResponse(response => response.url().includes('/api/gds-assets?')
    && response.request().method() === 'POST');
  await page.locator('input[type="file"][accept*=".gds"]').setInputFiles(maximumLayerFixture);
  const maximumLayerUploaded = await maximumLayerUploadPromise;
  assert.equal(maximumLayerUploaded.status(), 201, maximumLayerUploaded.status() === 201 ? '' : await maximumLayerUploaded.text());
  const maximumLayerProject = JSON.parse(await fs.readFile(cases.find(row => row.maximumLayer).project, 'utf8'));
  const maximumLayerAsset = await fetch(`${baseUrl}/api/gds-assets/${maximumLayerProject.geometry.assetId}`).then(async response => {
    if (response.status !== 200) assert.fail(`64-layer GDS asset lookup returned ${response.status}: ${await response.text()}`);
    return response.json();
  });
  assert.equal(maximumLayerAsset.assetId, maximumLayerProject.geometry.assetId);
  for (const caseRow of cases) report.cases.push(await checkRun(caseRow));

  const knownFailure = JSON.parse(await fs.readFile(knownPeriodicFailureProject, 'utf8'));
  const knownFailureCopy = path.join(evidenceRoot, 'known-native-periodic-failure.project.json');
  knownFailure.name = `Known periodic native failure ${Date.now()}`;
  await fs.writeFile(knownFailureCopy, `${JSON.stringify(knownFailure, null, 2)}\n`);
  await page.locator('input[type="file"][accept*="application/json"]').setInputFiles(knownFailureCopy);
  await page.waitForFunction(name => document.querySelector('input[aria-label="项目名称"]')?.value === name, knownFailure.name);
  await page.waitForFunction(() => document.querySelector('.capability-note')?.textContent?.includes('原生崩溃'));
  const knownGpuOption = page.getByLabel('引擎偏好', { exact: true }).locator('option[value="gpu"]');
  assert.equal(await knownGpuOption.evaluate(option => option.disabled), true, 'native SIGSEGV route must remain unavailable');
  assert.equal(await page.getByLabel('引擎偏好', { exact: true }).locator('option[value="auto"]').evaluate(option => option.disabled), true);
  assert.equal(await page.getByLabel('引擎偏好', { exact: true }).locator('option[value="cpu"]').evaluate(option => option.disabled), true);
  assert.equal(await page.getByRole('button', { name: '运行工艺', exact: true }).isDisabled(), true,
    'a native crash signature must disable submission on every engine preference');
  const knownFailureNotice = await page.locator('.capability-note').textContent();
  assert.match(knownFailureNotice ?? '', /ViennaPS 4\.6\.2 原生崩溃/);
  assert.match(knownFailureNotice ?? '', /Process\.apply.*SIGSEGV/);
  report.knownFailureGate = { model: 'specialized:sf6o2', dimension: 3, boundaryXY: 'periodic',
    gpuDisabled: true, notice: knownFailureNotice, evidenceProject: path.relative(root, knownFailureCopy) };

  assert.deepEqual(pageErrors, []);
  assert.deepEqual(consoleErrors, []);
  assert.deepEqual(failedRequests, []);
  report = { ...report, status: 'passed', pageErrors, consoleErrors, failedRequests };
  await fs.writeFile(path.join(evidenceRoot, 'acceptance.json'), `${JSON.stringify(report, null, 2)}\n`);
  console.log(JSON.stringify({ status: report.status, cases: report.cases.map(row => ({ name: row.name, runId: row.runId, actualEngine: row.actualEngine })), evidence: path.relative(root, path.join(evidenceRoot, 'acceptance.json')) }, null, 2));
} catch (error) {
  report = { ...report, status: 'failed', failure: { message: error.message, stack: error.stack }, pageErrors, consoleErrors, failedRequests };
  await fs.writeFile(path.join(evidenceRoot, 'acceptance.failure.json'), `${JSON.stringify(report, null, 2)}\n`);
  throw error;
} finally {
  await context.close();
  await browser.close();
}
