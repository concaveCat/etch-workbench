/** Real Chromium/API gate for ViennaPS 4.6.2 periodic specialized GDS crashes. */
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const baseUrl = (process.env.WORKBENCH_URL ?? 'http://127.0.0.1:8803').replace(/\/$/, '');
const dataDir = path.resolve(process.env.VIENNAPS_WORKBENCH_DATA_DIR ?? path.join(root, 'var/browser-8803'));
const evidenceRoot = path.join(root, 'docs/stage9-evidence/gpu-coverage-audit/browser/gds-native-failure-gate');
const fixture = path.join(root, 'docs/stage9-evidence/gpu-coverage-audit/gds-multilayer/multilayer-two-adjacent-polygons.gds');
const projectPath = path.join(root, 'docs/stage9-evidence/gpu-coverage-audit/gds-specialized/sf6o2-3d-gds-film-mask-periodic-gpu_disk/project.json');
await fs.mkdir(evidenceRoot, { recursive: true });

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
  await page.locator('input[type="file"][accept*=".gds"]').setInputFiles(fixture);
  const upload = await uploadPromise;
  assert.equal(upload.status(), 201, `GDS upload returned ${upload.status()}`);

  const project = JSON.parse(await fs.readFile(projectPath, 'utf8'));
  project.name = `Periodic GDS native crash diagnostic ${Date.now()}`;
  const importedPath = path.join(evidenceRoot, 'periodic-sf6o2-3d.project.json');
  await fs.writeFile(importedPath, `${JSON.stringify(project, null, 2)}\n`);
  const validationPromise = page.waitForResponse(response => response.url().endsWith('/api/projects/validate')
    && response.request().method() === 'POST');
  await page.locator('input[type="file"][accept*="application/json"]').setInputFiles(importedPath);
  const validation = await validationPromise;
  const validationBody = await validation.json();
  assert.equal(validation.status(), 200, JSON.stringify(validationBody));
  assert.ok(validationBody.warnings.some(message => message.includes('Process.apply SIGSEGV')),
    'project import should retain a visible native failure warning');
  await page.waitForFunction(name => document.querySelector('input[aria-label="项目名称"]')?.value === name, project.name);
  await page.waitForFunction(() => document.querySelector('.capability-note')?.textContent?.includes('原生崩溃'));

  const capabilityResponse = await fetch(`${baseUrl}/api/capabilities`, { cache: 'no-store' });
  assert.equal(capabilityResponse.status, 200);
  const capabilityPayload = await capabilityResponse.json();
  const knownFailures = capabilityPayload.capabilities?.knownNativeFailures ?? [];
  const matchingFailure = knownFailures.find(item => item.model === 'specialized:sf6o2'
    && item.dimension === 3 && item.operation === 'etch' && item.boundaryXY === 'periodic'
    && item.geometryKind === 'gds' && item.engineVersion === '4.6.2');
  const engineSelect = page.getByLabel('引擎偏好', { exact: true });
  const engineStates = Object.fromEntries(await Promise.all(['auto', 'cpu', 'gpu'].map(async engine =>
    [engine, await engineSelect.locator(`option[value="${engine}"]`).evaluate(option => option.disabled)])));
  const engineOptions = await engineSelect.evaluate(element => Array.from(element.options)
    .map(option => ({ value: option.value, label: option.textContent, disabled: option.disabled })));
  const runButton = page.getByRole('button', { name: '运行工艺', exact: true });
  const notice = await page.locator('.capability-note').textContent();
  console.log(JSON.stringify({ capabilityKeys: Object.keys(capabilityPayload.capabilities ?? {}),
    knownFailureCount: knownFailures.length, matchingFailure: Boolean(matchingFailure), engineStates, engineOptions,
    runButtonDisabled: await runButton.isDisabled(), notice }, null, 2));
  assert.ok(matchingFailure, 'the running service must load the exact matrix failure record');
  assert.deepEqual([...matchingFailure.engines].sort(), ['CPU_DISK', 'GPU_DISK']);

  for (const engine of ['auto', 'cpu', 'gpu']) assert.equal(engineStates[engine], true,
    `${engine} must be disabled for this native crash signature`);
  assert.equal(await runButton.isDisabled(), true);
  assert.match(notice ?? '', /ViennaPS 4\.6\.2 原生崩溃/);
  assert.match(notice ?? '', /Process\.apply.*SIGSEGV/);
  assert.match(notice ?? '', /CPU_DISK 与 GPU_DISK/);

  const runResponse = await fetch(`${baseUrl}/api/runs`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ kind: 'simulate', project }),
  });
  const runFailure = await runResponse.json();
  assert.equal(runResponse.status, 422, JSON.stringify(runFailure));
  assert.match(JSON.stringify(runFailure), /native Process\.apply SIGSEGV/);
  assert.deepEqual(pageErrors, []);

  const report = {
    status: 'passed', service: baseUrl, isolatedDataDir: dataDir,
    engineVersions: { viennaps: '4.6.2', viennals: '5.8.5' },
    failure: { model: matchingFailure.model, dimension: matchingFailure.dimension,
      operation: matchingFailure.operation, boundaryXY: matchingFailure.boundaryXY,
      geometryKind: matchingFailure.geometryKind, engines: matchingFailure.engines },
    projectLoad: { status: validation.status, warningRetained: true, importedProject: path.relative(root, importedPath) },
    ui: { autoDisabled: true, cpuDisabled: true, gpuDisabled: true, runButtonDisabled: true, notice },
    api: { runRequestStatus: runResponse.status, reason: runFailure }, pageErrors,
  };
  await fs.writeFile(path.join(evidenceRoot, 'acceptance.json'), `${JSON.stringify(report, null, 2)}\n`);
  console.log(JSON.stringify({ status: report.status, failure: report.failure, ui: report.ui, apiStatus: report.api.runRequestStatus,
    evidence: path.relative(root, path.join(evidenceRoot, 'acceptance.json')) }, null, 2));
} catch (error) {
  const report = { status: 'failed', failure: { message: error.message, stack: error.stack }, pageErrors };
  await fs.writeFile(path.join(evidenceRoot, 'acceptance.failure.json'), `${JSON.stringify(report, null, 2)}\n`);
  throw error;
} finally {
  await context.close();
  await browser.close();
}
