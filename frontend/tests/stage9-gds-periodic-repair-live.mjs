/** Real Chromium/API/product run for the periodic GDS extent repair. */
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import { createHash } from 'node:crypto';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const baseUrl = (process.env.WORKBENCH_URL ?? 'http://127.0.0.1:8803').replace(/\/$/, '');
const dataDir = path.resolve(process.env.VIENNAPS_WORKBENCH_DATA_DIR ?? path.join(root, 'var/browser-8803'));
const evidenceDir = path.join(root, 'docs/stage9-evidence/gpu-coverage-audit/browser/gds-periodic-repair');
const sourcePath = path.join(root, 'docs/stage9-evidence/gpu-coverage-audit/gds-multilayer/multilayer-two-adjacent-polygons.gds');
const projectPath = path.join(root, 'docs/stage9-evidence/gpu-coverage-audit/gds-specialized/sf6o2-3d-gds-two-layer-film-mask-periodic-gpu_disk/project.json');
const directPath = path.join(path.dirname(projectPath), 'direct.json');
const inputPath = path.join(evidenceDir, 'periodic-sf6o2-3d-input.project.json');
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
  assert.deepEqual(actual.triangles, expected.cells, `${label}: topology`);
  assert.deepEqual(actual.cellData.materialIds, expected.materialIds, `${label}: cell material ids`);
  assert.deepEqual(actual.materialIdsByLevelSetIndex, expected.materialIdsByLevelSetIndex,
    `${label}: level-set material mapping`);
}

const project = JSON.parse(await fs.readFile(projectPath, 'utf8'));
const direct = JSON.parse(await fs.readFile(directPath, 'utf8'));
project.name = `Periodic GDS repair Chromium ${Date.now()}`;
project.solver.enginePreference = 'gpu';
assert.equal(createHash('sha256').update(await fs.readFile(sourcePath)).digest('hex'), project.geometry.assetId);
await fs.writeFile(inputPath, `${JSON.stringify(project, null, 2)}\n`);

const capability = await api('/api/capabilities');
assert.equal(capability.response.status, 200);
const knownNativeFailures = capability.body.capabilities.knownNativeFailures ?? [];
assert.equal(knownNativeFailures.some(row => row.model === 'specialized:sf6o2'
  && row.dimension === 3 && row.boundaryXY === 'periodic' && row.geometryKind === 'gds'), false,
'the repaired signature must no longer be disabled by a stale native-crash entry');

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const context = await browser.newContext({ viewport: { width: 1680, height: 1100 } });
const page = await context.newPage();
page.setDefaultTimeout(30000);
const pageErrors = [];
page.on('pageerror', error => pageErrors.push({ message: error.message, stack: error.stack }));

try {
  await page.goto(`${baseUrl}/`);
  await page.getByText('本机服务已连接').waitFor();
  const uploadPromise = page.waitForResponse(response => response.url().includes('/api/gds-assets?')
    && response.request().method() === 'POST');
  await page.locator('input[type="file"][accept*=".gds"]').setInputFiles(sourcePath);
  const upload = await uploadPromise;
  assert.equal(upload.status(), 201, `GDS upload returned ${upload.status()}`);

  const validation = await api('/api/projects/validate', {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(project),
  });
  assert.equal(validation.response.status, 200, JSON.stringify(validation.body));
  assert.equal(validation.body.warnings.some(message => message.includes('Process.apply SIGSEGV')), false);

  const importResponse = page.waitForResponse(response => response.url().endsWith('/api/projects/validate')
    && response.request().method() === 'POST');
  await page.locator('nav[aria-label="项目操作"] input[type="file"]').setInputFiles(inputPath);
  const imported = await importResponse;
  assert.equal(imported.status(), 200, await imported.text());
  await page.waitForFunction(name => document.querySelector('input[aria-label="项目名称"]')?.value === name, project.name);

  const engine = page.getByLabel('引擎偏好', { exact: true });
  assert.equal(await engine.inputValue(), 'gpu');
  assert.equal(await engine.locator('option[value="gpu"]').evaluate(option => option.disabled), false);
  assert.equal(await page.getByRole('button', { name: '运行工艺', exact: true }).isDisabled(), false);
  const capabilityNote = await page.locator('.capability-note').innerText();
  assert.equal(capabilityNote.includes('原生崩溃'), false);

  const inspectResponse = page.waitForResponse(response => response.url().endsWith(`/api/gds-assets/${project.geometry.assetId}/inspect`)
    && response.request().method() === 'POST');
  await page.getByRole('button', { name: '检查 ViennaPS 原生 GDS 读入' }).click();
  const inspected = await inspectResponse;
  assert.equal(inspected.status(), 200, await inspected.text());
  const inspectionBody = await inspected.json();
  const inspection = inspectionBody.inspection;
  assert.equal(inspection.supportPurpose, 'align-native-periodic-gds-bounds-with-project-domain');
  assert.deepEqual(inspection.supportBoundsUm, [-6, 6, -4, 4]);
  assert.equal(inspection.nativeBounds[0], -6);
  assert.equal(inspection.nativeBounds[1], 6);
  assert.equal(inspection.nativeBounds[2], -4);
  assert.equal(inspection.nativeBounds[3], 4);
  assert.ok(inspection.supportLayers.includes(inspection.supportLayer));
  await page.getByText(/周期边界使用未映射的隐藏 GDS 支持图层/).waitFor();

  const requestPromise = page.waitForRequest(request => request.url().endsWith('/api/runs') && request.method() === 'POST');
  const responsePromise = page.waitForResponse(response => response.url().endsWith('/api/runs')
    && response.request().method() === 'POST');
  await page.getByRole('button', { name: '运行工艺', exact: true }).click();
  const [runRequest, runResponse] = await Promise.all([requestPromise, responsePromise]);
  assert.equal(runResponse.status(), 202, await runResponse.text());
  const submittedProject = runRequest.postDataJSON().project;
  assert.equal(submittedProject.solver.enginePreference, 'gpu');
  await fs.writeFile(path.join(evidenceDir, 'submitted-project.json'), `${JSON.stringify(submittedProject, null, 2)}\n`);
  const runId = (await runResponse.json()).runId;
  const run = await waitForRun(runId);
  const execution = run.actualExecution.find(row => row.stepId === submittedProject.steps[0].id);
  assert.equal(execution.actualEngine, 'GPU_DISK');

  const initialResponse = await api(`/api/runs/${runId}/snapshots/initial`);
  const finalResponse = await api(`/api/runs/${runId}/snapshots/${encodeURIComponent(submittedProject.steps[0].id)}`);
  assert.equal(initialResponse.response.status, 200);
  assert.equal(finalResponse.response.status, 200);
  compareSnapshot(initialResponse.body, direct.initial, 'initial');
  compareSnapshot(finalResponse.body, direct.final, 'final');

  const runDir = path.join(dataDir, 'runs', runId);
  const worker = JSON.parse(await fs.readFile(path.join(runDir, 'worker-result.json'), 'utf8'));
  const guard = JSON.parse(await fs.readFile(path.join(runDir, 'child-record.json'), 'utf8'));
  const productGeometry = worker.snapshots[0].geometryReadback;
  assert.equal(productGeometry.supportPurpose, inspection.supportPurpose);
  assert.deepEqual(productGeometry.supportBoundsUm, inspection.supportBoundsUm);
  assert.deepEqual(productGeometry.nativeBounds, inspection.nativeBounds);
  assert.equal(worker.versions.viennaps, '4.6.2');
  assert.equal(worker.versions.viennals, '5.8.5');
  assert.equal(guard.returnCode, 0);
  assert.deepEqual(guard.remainingOwnedGroupMembers, []);
  await fs.copyFile(path.join(runDir, 'worker-result.json'), path.join(evidenceDir, 'product-worker.json'));
  await fs.copyFile(path.join(runDir, 'child-record.json'), path.join(evidenceDir, 'product.guard.json'));
  assert.deepEqual(pageErrors, []);

  const report = {
    status: 'passed', browser: 'Chromium', service: baseUrl, isolatedDataDir: dataDir,
    installedVersions: worker.versions, project: path.relative(root, projectPath),
    directReference: path.relative(root, directPath), input: path.relative(root, inputPath),
    periodicGdsGpu: {
      model: 'specialized:sf6o2', dimension: 3, boundaryXY: 'periodic', actualEngine: execution.actualEngine,
      runStatus: run.status, periodicBoundsMatch: true, supportLayer: inspection.supportLayer,
      supportBoundsUm: inspection.supportBoundsUm, nativeBounds: inspection.nativeBounds,
      initialAndFinalExactlyMatchDirect: true, childReturnCode: guard.returnCode,
      remainingOwnedGroupMembers: guard.remainingOwnedGroupMembers,
      productWorker: path.relative(root, path.join(evidenceDir, 'product-worker.json')),
      productGuard: path.relative(root, path.join(evidenceDir, 'product.guard.json')),
    },
    projectImport: { status: imported.status(), warnings: validation.body.warnings },
    ui: { gpuAvailable: true, gpuRunButtonEnabled: true, gdsInspectionSupportVisible: true },
    pageErrors,
  };
  await fs.writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`);
  console.log(JSON.stringify({ status: report.status, runId, actualEngine: execution.actualEngine,
    reportPath: path.relative(root, reportPath) }, null, 2));
} catch (error) {
  await fs.writeFile(path.join(evidenceDir, 'acceptance.failure.json'), `${JSON.stringify({
    status: 'failed', failure: { message: error.message, stack: error.stack }, pageErrors,
  }, null, 2)}\n`);
  throw error;
} finally {
  await context.close();
  await browser.close();
}
