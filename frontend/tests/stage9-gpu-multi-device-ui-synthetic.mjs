/** Verify the multi-GPU UI and persistence path with a synthetic second inventory row.
 * This test never submits a run to the synthetic UUID and does not claim a second card.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const baseUrl = (process.env.WORKBENCH_URL ?? 'http://127.0.0.1:8804').replace(/\/$/, '');
const evidenceDir = path.join(root, 'docs/stage9-evidence/gpu-coverage-audit/gpu-device-selection/browser');
const reportPath = path.join(evidenceDir, 'synthetic-multi-device-ui.json');
const inputPath = path.join(evidenceDir, 'synthetic-multi-device-input.project.json');
const exportPath = path.join(evidenceDir, 'synthetic-multi-device-export.project.json');
const sourcePath = path.join(root, 'docs/stage9-evidence/gpu-coverage-audit/gpu-device-selection/rtx3060-3d-product-direct.project.json');
const secondaryUuid = 'GPU-11111111-2222-3333-4444-555555555555';
await fs.mkdir(evidenceDir, { recursive: true });

const project = JSON.parse(await fs.readFile(sourcePath, 'utf8'));
project.name = `Synthetic secondary GPU selector ${Date.now()}`;
project.solver.enginePreference = 'auto';
delete project.solver.gpuDeviceId;
await fs.writeFile(inputPath, `${JSON.stringify(project, null, 2)}\n`);

const inventoryResponse = await fetch(`${baseUrl}/api/gpu-devices`);
const physicalInventory = await inventoryResponse.json();
assert.equal(inventoryResponse.status, 200);
assert.equal(physicalInventory.status, 'available');
const physicalDevice = physicalInventory.devices.find(row => row.name === 'NVIDIA GeForce RTX 3060 Laptop GPU');
assert.ok(physicalDevice, 'host inventory must contain the physical RTX 3060 used by the separate GPU tests');
const secondaryDevice = { ...physicalDevice, index: 1, uuid: secondaryUuid, pciBusId: '0000:02:00.0',
  name: 'Synthetic secondary device (selector test only)' };
const syntheticInventory = { ...physicalInventory, devices: [physicalDevice, secondaryDevice],
  preferredGpuDeviceId: physicalDevice.uuid };

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const context = await browser.newContext({ viewport: { width: 1680, height: 1100 }, acceptDownloads: true });
const page = await context.newPage();
page.setDefaultTimeout(30000);
const pageErrors = [];
page.on('pageerror', error => pageErrors.push({ message: error.message, stack: error.stack }));

try {
  await page.route('**/api/gpu-devices', route => route.fulfill({ status: 200, contentType: 'application/json',
    body: JSON.stringify(syntheticInventory) }));
  await page.goto(`${baseUrl}/`);
  await page.getByText('本机服务已连接').waitFor();
  const importValidation = page.waitForResponse(response => response.url().endsWith('/api/projects/validate')
    && response.request().method() === 'POST');
  await page.locator('nav[aria-label="项目操作"] input[type="file"]').setInputFiles(inputPath);
  const imported = await importValidation;
  assert.equal(imported.status(), 200, await imported.text());

  const engine = page.getByLabel('引擎偏好', { exact: true });
  await engine.selectOption('gpu');
  const device = page.getByLabel('计算 GPU', { exact: true });
  const options = await device.locator('option').evaluateAll(rows => rows.map(row => ({ value: row.value, label: row.textContent })));
  assert.ok(options.some(row => row.value === physicalDevice.uuid));
  assert.ok(options.some(row => row.value === secondaryUuid));
  await device.selectOption(secondaryUuid);
  assert.equal(await device.inputValue(), secondaryUuid);

  const saveRequestPromise = page.waitForRequest(request => request.url().endsWith('/api/projects') && request.method() === 'POST');
  const saveResponsePromise = page.waitForResponse(response => response.url().endsWith('/api/projects') && response.request().method() === 'POST');
  await page.getByRole('button', { name: '保存项目', exact: true }).click();
  const [saveRequest, saveResponse] = await Promise.all([saveRequestPromise, saveResponsePromise]);
  assert.equal(saveResponse.status(), 201, await saveResponse.text());
  const saved = await saveResponse.json();
  assert.equal(saveRequest.postDataJSON().solver.gpuDeviceId, secondaryUuid);
  assert.equal(saved.project.solver.gpuDeviceId, secondaryUuid);

  await page.getByLabel('已保存项目', { exact: true }).selectOption(saved.id);
  const reopenPromise = page.waitForResponse(response => response.url().endsWith(`/api/projects/${saved.id}`)
    && response.request().method() === 'GET');
  await page.getByRole('button', { name: '打开', exact: true }).click();
  const reopened = await reopenPromise;
  assert.equal(reopened.status(), 200);
  await page.waitForFunction(uuid => document.querySelector('select[aria-label="计算 GPU"]')?.value === uuid, secondaryUuid);

  const downloadPromise = page.waitForEvent('download');
  const exportValidation = page.waitForResponse(response => response.url().endsWith('/api/projects/validate')
    && response.request().method() === 'POST');
  await page.getByRole('button', { name: '导出项目', exact: true }).click();
  const [download, validation] = await Promise.all([downloadPromise, exportValidation]);
  assert.equal(validation.status(), 200, await validation.text());
  await download.saveAs(exportPath);
  const exported = JSON.parse(await fs.readFile(exportPath, 'utf8'));
  assert.equal(exported.solver.gpuDeviceId, secondaryUuid);

  const reimportValidation = page.waitForResponse(response => response.url().endsWith('/api/projects/validate')
    && response.request().method() === 'POST');
  await page.locator('nav[aria-label="项目操作"] input[type="file"]').setInputFiles(exportPath);
  const reimported = await reimportValidation;
  assert.equal(reimported.status(), 200, await reimported.text());
  await page.waitForFunction(uuid => document.querySelector('select[aria-label="计算 GPU"]')?.value === uuid, secondaryUuid);
  assert.equal(await page.getByRole('button', { name: '运行工艺', exact: true }).isDisabled(), false);
  assert.deepEqual(pageErrors, []);

  const report = { status: 'passed', testKind: 'synthetic-inventory-ui-persistence-only',
    service: baseUrl, physicalDevice, syntheticDevice: secondaryDevice,
    selectedDeviceUuid: secondaryUuid, optionsContainedBothDevices: true,
    savedReopenedExportedAndImportedUuid: secondaryUuid, simulationExecuted: false,
    physicalSecondaryGpuClaimed: false,
    runnerGuard: process.env.BROWSER_ACCEPTANCE_GUARD
      ? path.relative(root, path.resolve(process.env.BROWSER_ACCEPTANCE_GUARD)) : null,
    pageErrors };
  await fs.writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`);
  console.log(JSON.stringify({ status: report.status, selectedDeviceUuid: secondaryUuid,
    simulationExecuted: false, reportPath: path.relative(root, reportPath) }, null, 2));
} catch (error) {
  await fs.writeFile(path.join(evidenceDir, 'synthetic-multi-device-ui.failure.json'), `${JSON.stringify({
    status: 'failed', failure: { message: error.message, stack: error.stack }, pageErrors,
  }, null, 2)}\n`);
  throw error;
} finally {
  await context.close();
  await browser.close();
}
