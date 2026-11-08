/** Real Chromium, product-worker and direct ViennaPS acceptance for GPU custom materials. */
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { promisify } from 'node:util';
import { execFile } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';
import { assertGpuReadback } from './helpers/gpu-readback.mjs';

const execFileAsync = promisify(execFile);
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const baseUrl = (process.env.WORKBENCH_URL ?? 'http://127.0.0.1:8804').replace(/\/$/, '');
const dataDir = path.resolve(process.env.VIENNAPS_WORKBENCH_DATA_DIR
  ?? path.join(root, 'var/browser-custom-gpu-acceptance-2026-10-28'));
const evidenceDir = path.join(root, 'docs/stage9-evidence/gpu-custom-material/browser');
const reportPath = path.join(evidenceDir, 'acceptance.json');
const python = path.join(root, '.venv/bin/python');
await fs.mkdir(evidenceDir, { recursive: true });

async function api(route, options = {}) {
  const response = await fetch(`${baseUrl}${route}`, options);
  const body = await response.json();
  return { response, body };
}

async function waitForRun(runId) {
  const deadline = Date.now() + 180000;
  while (Date.now() < deadline) {
    const { response, body } = await api(`/api/runs/${runId}`);
    assert.equal(response.status, 200);
    if (['succeeded', 'failed', 'cancelled', 'timed_out', 'interrupted'].includes(body.status)) {
      assert.equal(body.status, 'succeeded', body.failureReason ?? `run ended ${body.status}`);
      return body;
    }
    await new Promise(resolve => setTimeout(resolve, 200));
  }
  throw new Error(`run ${runId} did not finish within 180 seconds`);
}

function signature(snapshot, dimension) {
  return { points: snapshot.points, cells: dimension === 2 ? snapshot.lines : snapshot.triangles,
    materialIds: snapshot.cellData.materialIds,
    materialIdsByLevelSetIndex: snapshot.materialIdsByLevelSetIndex };
}

function directSignature(snapshot) {
  return { points: snapshot.points, cells: snapshot.cells, materialIds: snapshot.materialIds,
    materialIdsByLevelSetIndex: snapshot.materialIdsByLevelSetIndex };
}

async function directReference(project, name) {
  const projectPath = path.join(evidenceDir, `${name}.project.json`);
  const outputPath = path.join(evidenceDir, `${name}.direct.json`);
  const guardPath = path.join(evidenceDir, `${name}.direct.guard.json`);
  const logPath = path.join(evidenceDir, `${name}.direct.log`);
  await fs.writeFile(projectPath, `${JSON.stringify(project, null, 2)}\n`);
  await execFileAsync(python, [path.join(root, 'scripts/bounded_child.py'), '--cwd', root,
    '--record', guardPath, '--log-path', logPath, '--timeout-seconds', '150', '--max-rss-mib', '1800',
    '--max-log-bytes', '500000', '--', python,
    path.join(root, 'tests/fixtures/stage9_custom_material_gpu_reference.py'), projectPath, outputPath],
  { cwd: root, timeout: 165000, maxBuffer: 1024 * 1024 });
  const direct = JSON.parse(await fs.readFile(outputPath, 'utf8'));
  const guard = JSON.parse(await fs.readFile(guardPath, 'utf8'));
  assert.equal(guard.returnCode, 0);
  assert.deepEqual(guard.remainingOwnedGroupMembers, []);
  assert.equal(direct.engine, 'GPU_DISK');
  return { direct, projectPath, outputPath, guardPath, logPath };
}

const inventory = await api('/api/gpu-devices');
assert.equal(inventory.response.status, 200);
assert.equal(inventory.body.status, 'available', JSON.stringify(inventory.body));
assert.equal(inventory.body.probe?.bounded, true);
const device = inventory.body.devices.find(row => row.name === 'NVIDIA GeForce RTX 3060 Laptop GPU');
assert.ok(device, `RTX 3060 Laptop GPU not found: ${JSON.stringify(inventory.body.devices)}`);

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const context = await browser.newContext({ viewport: { width: 1680, height: 1100 }, acceptDownloads: true });
const page = await context.newPage();
page.setDefaultTimeout(30000);
const pageErrors = [];
page.on('pageerror', error => pageErrors.push({ message: error.message, stack: error.stack }));
const cases = [];

try {
  await page.goto(`${baseUrl}/`);
  await page.getByText('本机服务已连接').waitFor();
  await page.locator('.material-admin-panel').waitFor();
  const registerMaterial = async name => {
    await page.getByLabel('新材料名称').fill(name);
    await page.getByLabel('新材料颜色').fill('#7755aa');
    await page.locator('.material-admin-panel textarea[aria-label="新材料备注"]').fill('Stage 9 GPU native registry identity acceptance');
    const materialRequest = page.waitForRequest(request => request.url().endsWith('/api/materials') && request.method() === 'POST');
    const materialResponse = page.waitForResponse(response => response.url().endsWith('/api/materials')
      && response.request().method() === 'POST');
    await page.getByRole('button', { name: '注册自定义材料', exact: true }).click();
    const [sentMaterial, returnedMaterial] = await Promise.all([materialRequest, materialResponse]);
    assert.equal(returnedMaterial.status(), 201, await returnedMaterial.text());
    const result = await returnedMaterial.json();
    assert.equal(sentMaterial.postDataJSON().name, name);
    assert.equal(result.id, 177 + result.customId);
    assert.ok(result.id >= 177 && result.id <= 240);
    await page.getByText(new RegExp(`已保存自定义材料 ${name}`)).waitFor();
    return result;
  };
  let materialName = `GpuProbeFilm${Date.now()}`;
  let material = await registerMaterial(materialName);

  for (const dimension of [2, 3]) {
    if (dimension === 3) {
      await page.getByRole('button', { name: '切换到 3D', exact: true }).click();
      await page.getByRole('button', { name: '切换到 3D', exact: true }).waitFor({ state: 'attached' });
      await page.waitForFunction(() => document.querySelector('[aria-label="切换到 3D"]')?.getAttribute('aria-pressed') === 'true');
      const convertedSave = page.waitForResponse(response => response.url().endsWith('/api/projects')
        && response.request().method() === 'POST');
      await page.getByRole('button', { name: '保存项目', exact: true }).click();
      const convertedResponse = await convertedSave;
      assert.equal(convertedResponse.status(), 201, await convertedResponse.text());
      const converted = await convertedResponse.json();
      assert.ok(converted.project.view3D.visibleMaterialIds.includes(material.id));
      assert.ok(converted.project.view3D.visibleMaterialIds.length <= 16);
      await page.getByRole('button', { name: '新建工程', exact: true }).click();
      await page.getByLabel('基底材料', { exact: true }).waitFor();
      materialName = `GpuProbe3DFilm${Date.now()}`;
      material = await registerMaterial(materialName);
    }
    const evidenceStem = `custom-material-id${material.id}-${dimension}d`;
    await page.getByRole('button', { name: '平面', exact: true }).click();
    await page.getByLabel('基底材料', { exact: true }).selectOption(materialName);
    await page.getByLabel('项目名称', { exact: true }).fill(`GPU custom material ${dimension}D ${Date.now()}`);
    const fill = async (label, value) => {
      const input = page.getByLabel(label, { exact: true });
      await input.fill(String(value));
      await input.press('Tab');
    };
    await fill('网格间距', 0.2);
    await fill('x 横向总宽度', 4);
    if (dimension === 3) await fill('y 横向总宽度', 4);
    await fill('平面高度', 0.4);
    await page.getByRole('button', { name: '＋ 添加单粒子步骤', exact: true }).click();
    await page.getByLabel('速率模式', { exact: true }).selectOption('material');
    const customRateRow = page.locator('.rate-table[role="table"] .rate-row')
      .filter({ has: page.getByText(materialName, { exact: true }) });
    await customRateRow.locator('input[type="number"]').fill('0.2');
    await customRateRow.locator('input[type="number"]').press('Tab');
    await fill('持续时间', 0.2);
    await fill('每点射线数', 32);
    await fill('随机种子', 271828);

    const engineSelector = page.getByLabel('引擎偏好', { exact: true });
    const gpuOption = engineSelector.locator('option[value="gpu"]');
    assert.equal(await gpuOption.isDisabled(), false,
      `${dimension}D custom material was not exposed by the measured GPU material scope`);
    await engineSelector.selectOption('gpu');
    const deviceSelector = page.getByLabel('计算 GPU', { exact: true });
    assert.ok((await deviceSelector.locator('option').evaluateAll(options => options.map(option => option.value))).includes(device.uuid));
    await deviceSelector.selectOption(device.uuid);

    const saveResponse = page.waitForResponse(response => response.url().endsWith('/api/projects')
      && response.request().method() === 'POST');
    await page.getByRole('button', { name: '保存项目', exact: true }).click();
    const savedResponse = await saveResponse;
    assert.equal(savedResponse.status(), 201, await savedResponse.text());
    const saved = await savedResponse.json();
    const projectId = saved.id;
    assert.equal(saved.project.materialDefinitions.find(row => row.name === materialName).id, material.id);
    assert.equal(saved.project.solver.enginePreference, 'gpu');
    assert.equal(saved.project.solver.gpuDeviceId, device.uuid);
    if (dimension === 3) {
      assert.ok(saved.project.view3D.visibleMaterialIds.includes(material.id),
        '3D conversion should keep the custom substrate in the visible-material selection');
      assert.ok(saved.project.view3D.visibleMaterialIds.length <= 16,
        '3D conversion must respect the project schema 16-material visibility limit');
    }

    const exportDownload = page.waitForEvent('download');
    const exportValidation = page.waitForResponse(response => response.url().endsWith('/api/projects/validate')
      && response.request().method() === 'POST');
    await page.getByRole('button', { name: '导出项目', exact: true }).click();
    const [download, validation] = await Promise.all([exportDownload, exportValidation]);
    assert.equal(validation.status(), 200, await validation.text());
    const exportedPath = path.join(evidenceDir, `${evidenceStem}-export.json`);
    await download.saveAs(exportedPath);
    const exported = JSON.parse(await fs.readFile(exportedPath, 'utf8'));
    assert.equal(exported.materialDefinitions.find(row => row.name === materialName).id, material.id);
    assert.equal(exported.solver.gpuDeviceId, device.uuid);

    await page.getByLabel('已保存项目', { exact: true }).selectOption(projectId);
    const openResponse = page.waitForResponse(response => response.url().endsWith(`/api/projects/${projectId}`)
      && response.request().method() === 'GET');
    await page.getByRole('button', { name: '打开', exact: true }).click();
    const opened = await openResponse;
    assert.equal(opened.status(), 200);
    await page.waitForFunction(({ id, name }) => document.querySelector('select[aria-label="计算 GPU"]')?.value === id
      && document.querySelector('select[aria-label="基底材料"]')?.value === name,
    { id: device.uuid, name: materialName });
    assert.equal(await page.getByLabel('引擎偏好', { exact: true }).inputValue(), 'gpu');

    const runRequestPromise = page.waitForRequest(request => request.url().endsWith('/api/runs')
      && request.method() === 'POST');
    const runResponsePromise = page.waitForResponse(response => response.url().endsWith('/api/runs')
      && response.request().method() === 'POST');
    await page.getByRole('button', { name: '运行工艺', exact: true }).click();
    const [runRequest, runResponse] = await Promise.all([runRequestPromise, runResponsePromise]);
    assert.equal(runResponse.status(), 202, await runResponse.text());
    const submitted = runRequest.postDataJSON().project;
    assert.equal(submitted.geometry.substrateMaterial, materialName);
    assert.equal(submitted.materialDefinitions.find(row => row.name === materialName).id, material.id);
    assert.equal(submitted.solver.enginePreference, 'gpu');
    assert.equal(submitted.solver.gpuDeviceId, device.uuid);
    assert.equal(submitted.steps.length, 1, 'each dimensional acceptance case should contain one process step');
    assert.equal(submitted.steps[0].materialRates[materialName], 0.2);
    if (dimension === 3) {
      assert.ok(submitted.view3D.visibleMaterialIds.includes(material.id));
      assert.ok(submitted.view3D.visibleMaterialIds.length <= 16);
    }
    const runId = (await runResponse.json()).runId;
    const run = await waitForRun(runId);
    const execution = run.actualExecution.find(row => row.stepId === submitted.steps[0].id);
    assert.equal(execution.actualEngine, 'GPU_DISK');
    assertGpuReadback(execution.actualParameters.cudaDeviceReadback, device.uuid, `${dimension}D custom-material run`);
    assert.equal(execution.actualParameters.actualMaterialRatesById[String(material.id)], -0.2);

    const { direct, projectPath, outputPath, guardPath, logPath } = await directReference(submitted, `custom-material-${dimension}d`);
    assert.deepEqual(signature(await (await api(`/api/runs/${runId}/snapshots/initial`)).body, dimension),
      directSignature(direct.initial), `${dimension}D initial custom-material mesh`);
    const finalResponse = await api(`/api/runs/${runId}/snapshots/${encodeURIComponent(submitted.steps[0].id)}`);
    assert.equal(finalResponse.response.status, 200);
    assert.deepEqual(signature(finalResponse.body, dimension), directSignature(direct.final),
      `${dimension}D final custom-material mesh`);
    assert.ok(finalResponse.body.materialIdsByLevelSetIndex.includes(material.id));

    const replay = await api(`/api/runs/${runId}/replay-project`);
    assert.equal(replay.response.status, 200);
    assert.equal(replay.body.normalizedProject.materialDefinitions.find(row => row.name === materialName).id, material.id);
    assert.equal(replay.body.normalizedProject.solver.gpuDeviceId, device.uuid);
    assert.equal(replay.body.normalizedProject.solver.enginePreference, 'gpu');
    const replayStart = await api('/api/runs/replay', { method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify(replay.body) });
    assert.equal(replayStart.response.status, 202, JSON.stringify(replayStart.body));
    const replayRun = await waitForRun(replayStart.body.runId);
    const replayExecution = replayRun.actualExecution.find(row => row.stepId === submitted.steps[0].id);
    assert.equal(replayExecution.actualEngine, 'GPU_DISK');
    assertGpuReadback(replayExecution.actualParameters.cudaDeviceReadback, device.uuid, `${dimension}D custom-material replay`);
    const replayFinal = await api(`/api/runs/${replayRun.runId}/snapshots/${encodeURIComponent(submitted.steps[0].id)}`);
    assert.deepEqual(signature(replayFinal.body, dimension), directSignature(direct.final),
      `${dimension}D replay custom-material mesh`);

    const runDirectory = path.join(dataDir, 'runs', runId);
    const worker = JSON.parse(await fs.readFile(path.join(runDirectory, 'worker-result.json'), 'utf8'));
    const productGuard = JSON.parse(await fs.readFile(path.join(runDirectory, 'child-record.json'), 'utf8'));
    assert.equal(worker.cudaDeviceReadback.uuid, device.uuid);
    assert.equal(productGuard.returnCode, 0);
    assert.deepEqual(productGuard.remainingOwnedGroupMembers, []);
    const replayDirectory = path.join(dataDir, 'runs', replayStart.body.runId);
    const replayWorker = JSON.parse(await fs.readFile(path.join(replayDirectory, 'worker-result.json'), 'utf8'));
    const replayGuard = JSON.parse(await fs.readFile(path.join(replayDirectory, 'child-record.json'), 'utf8'));
    const productEvidence = path.join(evidenceDir, `${evidenceStem}-product`);
    await fs.mkdir(productEvidence, { recursive: true });
    await fs.copyFile(projectPath, path.join(productEvidence, 'project.json'));
    await fs.copyFile(path.join(runDirectory, 'worker-result.json'), path.join(productEvidence, 'worker-result.json'));
    await fs.copyFile(path.join(runDirectory, 'child-record.json'), path.join(productEvidence, 'child-record.json'));
    const initialRecord = worker.snapshots.find(row => row.stepId === 'initial');
    const finalRecord = worker.snapshots.find(row => row.stepId === submitted.steps[0].id);
    assert.ok(initialRecord && finalRecord, 'product worker is missing custom-material snapshots');
    await fs.copyFile(path.join(runDirectory, initialRecord.snapshot), path.join(productEvidence, 'initial.json'));
    await fs.copyFile(path.join(runDirectory, finalRecord.snapshot), path.join(productEvidence, 'final.json'));
    await fs.writeFile(path.join(productEvidence, 'submitted-project.json'), `${JSON.stringify(submitted, null, 2)}\n`);
    await fs.writeFile(path.join(productEvidence, 'exported-project.json'), `${JSON.stringify(exported, null, 2)}\n`);
    await fs.copyFile(path.join(replayDirectory, 'worker-result.json'), path.join(productEvidence, 'replay-worker-result.json'));
    await fs.copyFile(path.join(replayDirectory, 'child-record.json'), path.join(productEvidence, 'replay-child-record.json'));
    await fs.writeFile(path.join(productEvidence, 'replay-final.json'), `${JSON.stringify(replayFinal.body, null, 2)}\n`);
    cases.push({ dimension, geometry: 'plane', material: { name: materialName, id: material.id, customId: material.customId },
      runId, replayRunId: replayRun.runId, engine: execution.actualEngine, gpuDeviceId: device.uuid,
      installedVersions: worker.versions, savedProjectId: projectId,
      directReference: path.relative(root, outputPath), project: path.relative(root, projectPath),
      directGuard: path.relative(root, guardPath), directLog: path.relative(root, logPath),
      productEvidence: path.relative(root, productEvidence), exportedProject: path.relative(root, exportedPath),
      productGuard: { returnCode: productGuard.returnCode, remainingOwnedGroupMembers: productGuard.remainingOwnedGroupMembers },
      replayGuard: { returnCode: replayGuard.returnCode, remainingOwnedGroupMembers: replayGuard.remainingOwnedGroupMembers },
      exactInitialFinalAndReplayMatchToDirectViennaPS: true,
      savedReopenedExportedAndReplayedWithStableMaterialId: true,
      productEngineActualMaterialRate: execution.actualParameters.actualMaterialRatesById[String(material.id)] });
  }
  assert.deepEqual(pageErrors, []);
  const report = { status: 'passed', service: baseUrl, isolatedDataDir: dataDir, installedVersions: cases[0]?.installedVersions,
    device, deviceInventoryProbe: inventory.body.probe,
    runnerGuard: process.env.BROWSER_ACCEPTANCE_GUARD ? path.relative(root, path.resolve(process.env.BROWSER_ACCEPTANCE_GUARD)) : null,
    registeredMaterial: material, cases, pageErrors };
  await fs.writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`);
  console.log(JSON.stringify({ status: report.status, cases: cases.map(row => `${row.dimension}D`),
    materialId: material.id, gpuDeviceId: device.uuid, reportPath: path.relative(root, reportPath) }, null, 2));
} catch (error) {
  await fs.writeFile(path.join(evidenceDir, 'acceptance.failure.json'), `${JSON.stringify({ status: 'failed',
    failure: { message: error.message, stack: error.stack }, cases, pageErrors }, null, 2)}\n`);
  throw error;
} finally {
  await context.close();
  await browser.close();
}
