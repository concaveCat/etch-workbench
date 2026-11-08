/** Real Chromium acceptance for Stage 5 GDS upload, native preview, and Slice. */
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '../..');
const evidence = path.join(root, 'docs/stage5-evidence');
const fixture = path.join(evidence, 'two-structure-two-layer.gds');
const baseUrl = (process.env.WORKBENCH_URL ?? 'http://127.0.0.1:8779').replace(/\/$/, '');
await fs.mkdir(evidence, { recursive: true });

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const context = await browser.newContext({ acceptDownloads: true, viewport: { width: 1600, height: 1000 } });
const page = await context.newPage();
page.setDefaultTimeout(20000);
const pageErrors = [];
const consoleErrors = [];
const failedRequests = [];
page.on('pageerror', error => pageErrors.push({ message: error.message, stack: error.stack }));
page.on('console', message => { if (message.type() === 'error') consoleErrors.push(message.text()); });
page.on('requestfailed', request => failedRequests.push({ url: request.url(), error: request.failure()?.errorText }));

async function fill(label, value) {
  const input = page.getByLabel(label);
  await input.fill(String(value));
  await input.press('Tab');
}

async function waitForRun(runId) {
  const terminal = new Set(['succeeded', 'failed', 'cancelled', 'timed_out', 'interrupted']);
  const deadline = Date.now() + 120000;
  let result;
  do {
    const response = await fetch(`${baseUrl}/api/runs/${runId}`, { cache: 'no-store' });
    assert.equal(response.status, 200);
    result = await response.json();
    if (terminal.has(result.status)) break;
    await new Promise(resolve => setTimeout(resolve, 150));
  } while (Date.now() < deadline);
  assert.ok(terminal.has(result?.status), `run ${runId} did not finish`);
  assert.equal(result.status, 'succeeded', result.failureReason ?? result.status);
  await page.getByText('还没有可显示的网格').waitFor({ state: 'hidden' });
  return result;
}

async function submit(buttonName, endpoint) {
  const request = page.waitForRequest(item => item.url().endsWith(endpoint) && item.method() === 'POST');
  const response = page.waitForResponse(item => item.url().endsWith(endpoint) && item.request().method() === 'POST');
  await page.getByRole('button', { name: buttonName, exact: true }).click();
  const [sent, returned] = await Promise.all([request, response]);
  const status = returned.status();
  assert.equal(status, 202, status === 202 ? '' : await returned.text());
  return { input: sent.postDataJSON(), runId: (await returned.json()).runId };
}

async function waitForSaved(name) {
  await page.waitForFunction(projectName => Array.from(document.querySelectorAll('select[aria-label="已保存项目"] option'))
    .some(option => option.textContent?.includes(projectName)), name);
  const options = await page.getByLabel('已保存项目').locator('option')
    .evaluateAll(rows => rows.map(row => ({ value: row.value, text: row.textContent ?? '' })));
  const selected = options.find(row => row.value && row.text.includes(name));
  assert.ok(selected, `saved project ${name} missing from list`);
  await page.getByLabel('已保存项目').selectOption(selected.value);
  await page.getByRole('button', { name: '打开', exact: true }).click();
}

function apiSignature(snapshot) {
  return { points: snapshot.points,
    topology: snapshot.dimension === 3 ? { triangles: snapshot.triangles } : { lines: snapshot.lines },
    materialIds: snapshot.cellData.materialIds,
    materialIdsByLevelSetIndex: snapshot.materialIdsByLevelSetIndex };
}

function directSignature(mesh, key) {
  const value = mesh[key];
  const topology = value.dimension === 3 ? { triangles: value.topology.triangles } : { lines: value.topology.lines };
  return { points: value.points, topology, materialIds: value.cellScalars.MaterialIds,
    materialIdsByLevelSetIndex: value.materialIdsByLevelSetIndex };
}

async function apiSnapshot(runId, stepId) {
  const response = await fetch(`${baseUrl}/api/runs/${runId}/snapshots/${stepId}`);
  assert.equal(response.status, 200, response.status === 200 ? '' : await response.text());
  return response.json();
}

try {
  await page.goto(`${baseUrl}/`);
  await page.getByText('本机服务已连接').waitFor();
  assert.deepEqual(pageErrors, []);

  const project2d = `Stage 5 GDS UI 2D ${Date.now()}`;
  await page.getByLabel('项目名称').fill(project2d);
  const uploadResponse = page.waitForResponse(response => response.url().includes('/api/gds-assets?') && response.request().method() === 'POST');
  await page.locator('input[type="file"][accept*=".gds"]').setInputFiles(fixture);
  const upload = await uploadResponse;
  assert.equal(upload.status(), 201, upload.status() === 201 ? '' : await upload.text());
  const assetId = createHash('sha256').update(await fs.readFile(fixture)).digest('hex');
  const assetResponse = await fetch(`${baseUrl}/api/gds-assets/${assetId}`);
  assert.equal(assetResponse.status, 200);
  const asset = await assetResponse.json();
  assert.equal(asset.assetId, assetId);
  await page.getByText(/已导入 two-structure-two-layer\.gds/).waitFor();
  await fill('网格间距', 0.25);
  await fill('x 横向总宽度', 8);
  await page.getByLabel('GDS 结构单元').selectOption('CELL_A');
  await fill('声明单位到 µm 的手动比例', 1.5);
  await fill('GDS x 位置', 0.25);
  await fill('GDS h 位置', 0.1);
  const run2d = await submit('生成结构预览', '/api/runs');
  assert.equal(run2d.input.kind, 'preview');
  assert.equal(run2d.input.project.schemaVersion, 7);
  assert.equal(run2d.input.project.dimension, 2);
  assert.equal(run2d.input.project.geometry.assetId, asset.assetId);
  assert.equal(run2d.input.project.geometry.structure, 'CELL_A');
  assert.equal(run2d.input.project.geometry.unitScale, 1.5);
  assert.deepEqual(run2d.input.project.geometry.position, [0.25, 0.1, 0]);
  await waitForRun(run2d.runId);
  const snapshot2d = await apiSnapshot(run2d.runId, 'initial');
  const direct2d = JSON.parse(await fs.readFile(path.join(evidence, 'direct-gds-2d-CELL_A/reference.json'), 'utf8'));
  assert.deepEqual(apiSignature(snapshot2d), directSignature(direct2d, 'initial'));
  await page.getByRole('button', { name: '保存项目', exact: true }).click();
  await waitForSaved(project2d);
  assert.equal(await page.getByLabel('GDS 结构单元').inputValue(), 'CELL_A');

  await page.getByRole('button', { name: '切换到 3D' }).click();
  await page.waitForFunction(() => document.querySelector('button[aria-label="切换到 3D"]')?.getAttribute('aria-pressed') === 'true');
  await fill('网格间距', 0.25);
  await fill('x 横向总宽度', 12);
  await fill('y 横向总宽度', 8);
  await page.getByLabel('GDS 结构单元').selectOption('CELL_B');
  await page.getByLabel('图层 2 的材料').selectOption('W');
  await fill('声明单位到 µm 的手动比例', 1);
  await fill('GDS x 位置', -0.25);
  await fill('GDS y 位置', 0.25);
  await fill('层高附加偏移 z', 0.1);
  await fill('图层 2 底面高度', 0.2);
  await fill('图层 2 厚度', 1.0);
  const inspectRequest = page.waitForRequest(item => item.url().includes(`/api/gds-assets/${asset.assetId}/inspect`) && item.method() === 'POST');
  const inspectResponse = page.waitForResponse(item => item.url().includes(`/api/gds-assets/${asset.assetId}/inspect`) && item.request().method() === 'POST');
  await page.getByRole('button', { name: '检查 ViennaPS 原生 GDS 读入', exact: true }).click();
  const [inspectSent, inspectReturned] = await Promise.all([inspectRequest, inspectResponse]);
  assert.equal(inspectReturned.status(), 200, inspectReturned.status() === 200 ? '' : await inspectReturned.text());
  const inspectPayload = inspectSent.postDataJSON();
  assert.equal(inspectPayload.dimension, 3);
  assert.equal(inspectPayload.structure, 'CELL_B');
  assert.deepEqual(inspectPayload.selectedLayers, [2]);
  await page.getByText('GDSGeometry 布局读取边界', { exact: true }).waitFor();

  const project3d = `Stage 5 GDS UI 3D ${Date.now()}`;
  await page.getByLabel('项目名称').fill(project3d);
  const run3d = await submit('生成结构预览', '/api/runs');
  assert.equal(run3d.input.project.dimension, 3);
  assert.equal(run3d.input.project.geometry.structure, 'CELL_B');
  assert.equal(run3d.input.project.geometry.layers[0].material, 'W');
  assert.deepEqual(run3d.input.project.geometry.position, [-0.25, 0.25, 0.1]);
  assert.equal(run3d.input.project.geometry.layers[0].height, 1);
  await waitForRun(run3d.runId);
  const snapshot3d = await apiSnapshot(run3d.runId, 'initial');
  const direct3d = JSON.parse(await fs.readFile(path.join(evidence, 'direct-gds-3d-CELL_B/reference.json'), 'utf8'));
  assert.deepEqual(apiSignature(snapshot3d), directSignature(direct3d, 'initial'));

  await page.getByRole('button', { name: '保存项目', exact: true }).click();
  await waitForSaved(project3d);
  assert.equal(await page.getByLabel('GDS 结构单元').inputValue(), 'CELL_B');
  assert.equal(await page.getByLabel('图层 2 的材料').inputValue(), 'W');
  const exportDownload = page.waitForEvent('download');
  await page.getByRole('button', { name: '导出项目', exact: true }).click();
  const exported = await exportDownload;
  const packagePath = path.join(evidence, 'browser-gds-project.zip');
  await exported.saveAs(packagePath);
  assert.ok((await fs.stat(packagePath)).size > 0);
  await page.getByRole('button', { name: '新建工程', exact: true }).click();
  await page.locator('input[type="file"][accept*="application/zip"]').setInputFiles(packagePath);
  await page.getByText('GDS 项目包和源布局已校验并导入。', { exact: true }).waitFor();
  assert.equal(await page.getByLabel('GDS 结构单元').inputValue(), 'CELL_B');
  assert.equal(await page.getByLabel('图层 2 的材料').inputValue(), 'W');

  await fill('真实切片位置 y', 1.25);
  const slice = await submit('运行 ViennaPS Slice 并生成 2D 网格', '/api/runs/slice');
  assert.equal(slice.input.project.geometry.structure, 'CELL_B');
  assert.deepEqual(slice.input.sliceRequest, { axis: 1, position: 1.25 });
  await waitForRun(slice.runId);
  const sliceSnapshot = await apiSnapshot(slice.runId, 'true-slice');
  const directSlice = JSON.parse(await fs.readFile(path.join(evidence, 'direct-gds-3d-true-slice/reference.json'), 'utf8'));
  assert.deepEqual(apiSignature(sliceSnapshot), directSignature(directSlice, 'slice'));
  assert.equal(sliceSnapshot.sliceReadback.nativeOperation, 'viennaps.Slice');
  assert.equal(sliceSnapshot.sliceReadback.viewerClipping, false);

  const replayDownload = page.waitForEvent('download');
  await page.getByRole('button', { name: '导出重放配置', exact: true }).click();
  const replayFile = path.join(evidence, 'browser-gds-slice-replay.json');
  await (await replayDownload).saveAs(replayFile);
  const replay = JSON.parse(await fs.readFile(replayFile, 'utf8'));
  assert.equal(replay.analysisMode, 'slice');
  assert.deepEqual(replay.sliceRequest, { axis: 1, position: 1.25 });
  const replayRequest = page.waitForRequest(item => item.url().endsWith('/api/runs/replay') && item.method() === 'POST');
  const replayResponse = page.waitForResponse(item => item.url().endsWith('/api/runs/replay') && item.request().method() === 'POST');
  await page.locator('input[type="file"][accept*="application/json"]').setInputFiles(replayFile);
  const [replaySent, replayReturned] = await Promise.all([replayRequest, replayResponse]);
  assert.equal(replayReturned.status(), 202, replayReturned.status() === 202 ? '' : await replayReturned.text());
  assert.equal(replaySent.postDataJSON().analysisMode, 'slice');
  const sliceReplayId = (await replayReturned.json()).runId;
  await waitForRun(sliceReplayId);
  const replayedSlice = await apiSnapshot(sliceReplayId, 'true-slice');
  assert.deepEqual(apiSignature(replayedSlice), directSignature(directSlice, 'slice'));

  const runDownload = page.waitForEvent('download');
  await page.getByRole('button', { name: '下载运行结果 ZIP', exact: true }).click();
  const runZipPath = path.join(evidence, 'browser-gds-slice-run.zip');
  await (await runDownload).saveAs(runZipPath);
  assert.ok((await fs.stat(runZipPath)).size > 0);
  const historyItem = page.locator('.history-item').filter({ hasText: slice.runId.slice(0, 8) });
  await historyItem.waitFor();
  await historyItem.click();
  assert.equal((await apiSnapshot(slice.runId, 'true-slice')).sliceReadback.viewerClipping, false);
  assert.deepEqual(pageErrors, []);

  const report = { passed: true, service: baseUrl, gdsAssetId: asset.assetId,
    runs: { gds2d: run2d.runId, gds3d: run3d.runId, nativeSlice: slice.runId, sliceReplay: sliceReplayId },
    requestReadback: { gds2dStructure: run2d.input.project.geometry.structure,
      gds2dScale: run2d.input.project.geometry.unitScale, gds2dPosition: run2d.input.project.geometry.position,
      gds3dStructure: run3d.input.project.geometry.structure, gds3dLayer: run3d.input.project.geometry.layers[0],
      gds3dPosition: run3d.input.project.geometry.position, sliceRequest: slice.input.sliceRequest },
    exactDirectNativeMeshMatches: { gds2d: true, gds3d: true, trueSlice: true, replayedSlice: true },
    portableProjectZip: packagePath, runZip: runZipPath, replayFile,
    pageErrors, consoleErrors, failedRequests };
  await fs.writeFile(path.join(evidence, 'browser-live.json'), `${JSON.stringify(report, null, 2)}\n`);
  console.log(JSON.stringify({ passed: true, runs: report.runs, evidence: path.join(evidence, 'browser-live.json') }));
} catch (error) {
  console.error(JSON.stringify({ url: page.url(), bodyText: await page.locator('body').innerText().catch(() => ''),
    pageErrors, consoleErrors, failedRequests }, null, 2));
  throw error;
} finally {
  await browser.close();
}
