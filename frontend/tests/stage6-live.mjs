/** Real Chromium acceptance for Stage 6 custom materials and native outputs. */
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { promisify } from 'node:util';
import { execFile } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const execFileAsync = promisify(execFile);
const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '../..');
const evidence = path.join(root, 'docs/stage6-evidence');
const baseUrl = (process.env.WORKBENCH_URL ?? 'http://127.0.0.1:8781').replace(/\/$/, '');
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
  return result;
}

async function submit(buttonName, endpoint) {
  const request = page.waitForRequest(item => item.url().endsWith(endpoint) && item.method() === 'POST');
  const response = page.waitForResponse(item => item.url().endsWith(endpoint) && item.request().method() === 'POST');
  await page.getByRole('button', { name: buttonName, exact: true }).click();
  const [sent, returned] = await Promise.all([request, response]);
  assert.equal(returned.status(), 202, returned.status() === 202 ? '' : await returned.text());
  return { input: sent.postDataJSON(), runId: (await returned.json()).runId };
}

async function apiSnapshot(runId, stepId) {
  const response = await fetch(`${baseUrl}/api/runs/${runId}/snapshots/${encodeURIComponent(stepId)}`);
  assert.equal(response.status, 200, response.status === 200 ? '' : await response.text());
  return response.json();
}

function apiSignature(snapshot) {
  return {
    points: snapshot.points,
    topology: snapshot.dimension === 3 ? { triangles: snapshot.triangles } : { lines: snapshot.lines },
    materialIds: snapshot.cellData.materialIds,
    materialIdsByLevelSetIndex: snapshot.materialIdsByLevelSetIndex,
  };
}

function directSignature(mesh, dimension) {
  return {
    points: mesh.points,
    topology: dimension === 3 ? { triangles: mesh.cells } : { lines: mesh.cells },
    materialIds: mesh.materialIds,
    materialIdsByLevelSetIndex: mesh.materialIdsByLevelSetIndex,
  };
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

async function createDirectReference(project, label) {
  const refDir = path.join(evidence, label);
  await fs.mkdir(refDir, { recursive: true });
  const projectPath = path.join(refDir, 'project.json');
  const materialsPath = path.join(refDir, 'custom-materials.json');
  const outputPath = path.join(refDir, 'direct-output');
  await fs.writeFile(projectPath, `${JSON.stringify(project, null, 2)}\n`);
  await fs.writeFile(materialsPath, `${JSON.stringify(project.materialDefinitions ?? [], null, 2)}\n`);
  const py = path.join(root, '.venv/bin/python');
  await execFileAsync(py, [path.join(root, 'scripts/bounded_child.py'), '--cwd', root,
    '--record', path.join(refDir, 'direct.guard.json'), '--log-path', path.join(refDir, 'direct.log'),
    '--timeout-seconds', '60', '--max-rss-mib', '1536', '--max-log-bytes', '300000', '--', py,
    path.join(root, 'tests/fixtures/stage6_material_output_reference.py'), projectPath, materialsPath, outputPath],
  { cwd: root, timeout: 90000, maxBuffer: 1024 * 1024 });
  return { refDir, outputPath, reference: JSON.parse(await fs.readFile(path.join(outputPath, 'reference.json'), 'utf8')) };
}

async function compareNativeSnapshot(runId, stepId, direct, dimension) {
  const snapshot = await apiSnapshot(runId, stepId);
  const expected = direct.reference.snapshots[stepId];
  assert.ok(expected, `direct reference has no ${stepId} snapshot`);
  assert.deepEqual(apiSignature(snapshot), directSignature(expected.mesh, dimension), `native mesh mismatch for ${stepId}`);
  const expectedOutputs = new Map(expected.outputs.map(row => [`${row.kind}:${row.levelSetIndex ?? ''}`, row]));
  const actualOutputs = new Map((snapshot.outputs ?? []).map(row => [`${row.kind}:${row.levelSetIndex ?? ''}`, row]));
  assert.deepEqual([...actualOutputs.keys()].sort(), [...expectedOutputs.keys()].sort(), `native output inventory mismatch for ${stepId}`);
  for (const [key, artifact] of actualOutputs) {
    const expectedArtifact = expectedOutputs.get(key);
    const relative = path.posix.join(stepId, expectedArtifact.fileName);
    const bytes = Buffer.from(await fetch(new URL(artifact.url, baseUrl)).then(async response => {
      assert.equal(response.status, 200, `${key} artifact HTTP status`);
      return response.arrayBuffer();
    }));
    assert.equal(createHash('sha256').update(bytes).digest('hex'), artifact.sha256, `${key} API hash`);
    assert.equal(artifact.sizeBytes, bytes.byteLength, `${key} API size`);
    assert.deepEqual(bytes, await fs.readFile(path.join(direct.outputPath, relative)), `${key} differs from direct ViennaPS output`);
  }
  return snapshot;
}

try {
  await page.goto(`${baseUrl}/`);
  await page.getByText('本机服务已连接').waitFor();
  await page.locator('.material-admin-panel').waitFor();
  assert.deepEqual(pageErrors, []);

  const materialName = `BrowserMat${Date.now()}`;
  await page.getByLabel('新材料名称').fill(materialName);
  await page.getByLabel('新材料颜色').fill('#3478ab');
  await page.getByLabel('新材料密度').fill('2.4');
  await page.getByLabel('新材料导电').check();
  await page.locator('.material-admin-panel textarea[aria-label="新材料备注"]').fill('Chromium Stage 6 material metadata');
  const materialRequest = page.waitForRequest(item => item.url().endsWith('/api/materials') && item.method() === 'POST');
  const materialResponse = page.waitForResponse(item => item.url().endsWith('/api/materials') && item.request().method() === 'POST');
  await page.getByRole('button', { name: '注册自定义材料', exact: true }).click();
  const [materialSent, materialReturned] = await Promise.all([materialRequest, materialResponse]);
  assert.equal(materialReturned.status(), 201, await materialReturned.text());
  const material = await materialReturned.json();
  assert.equal(materialSent.postDataJSON().name, materialName);
  assert.equal(material.id, 177 + material.customId);
  await page.getByText(new RegExp(`已保存自定义材料 ${materialName}`)).waitFor();

  const projectName = `Stage 6 Chromium ${Date.now()}`;
  await page.getByLabel('项目名称').fill(projectName);
  await page.getByRole('button', { name: '平面', exact: true }).click();
  await page.getByLabel('基底材料').selectOption(materialName);
  await fill('网格间距', 0.2);
  await fill('x 横向总宽度', 3);
  await fill('平面高度', 0.4);
  await page.getByLabel('输出原生 level-set').check();
  await page.getByLabel('输出 level-set 网格').check();
  await page.getByLabel('输出 ViennaPS 元数据').check();
  await page.getByRole('button', { name: '＋ 添加各向同性步骤', exact: true }).click();
  const stepCard = page.locator('.step-card').last();
  await stepCard.locator('.step-main').click();
  await fill('速率大小', 0.2);
  await fill('持续时间', 0.2);

  const run = await submit('运行工艺', '/api/runs');
  assert.equal(run.input.project.schemaVersion, 7);
  assert.equal(run.input.project.dimension, 2);
  assert.equal(run.input.project.geometry.substrateMaterial, materialName);
  assert.equal(run.input.project.outputSettings.levelSets, true);
  assert.equal(run.input.project.outputSettings.levelSetMeshes, true);
  assert.equal(run.input.project.outputSettings.volumeMesh, false);
  assert.equal(run.input.project.outputSettings.nativeMetadata, true);
  assert.equal(run.input.project.steps.length, 1);
  assert.equal(run.input.project.steps[0].model, 'isotropic');
  assert.equal(run.input.project.steps[0].rateMagnitude, 0.2);
  assert.equal(run.input.project.steps[0].duration, 0.2);
  await waitForRun(run.runId);
  const validatedResponse = await fetch(`${baseUrl}/api/projects/validate`, { method: 'POST',
    headers: { 'content-type': 'application/json' }, body: JSON.stringify(run.input.project) });
  assert.equal(validatedResponse.status, 200, validatedResponse.status === 200 ? '' : await validatedResponse.text());
  const normalizedProject = (await validatedResponse.json()).normalizedProject;
  assert.deepEqual(normalizedProject.materialDefinitions.find(row => row.name === materialName), {
    name: materialName, id: material.id, customId: material.customId, color: '#3478ab', densityGcm3: 2.4,
    conductive: true, description: 'Chromium Stage 6 material metadata',
  });
  const direct = await createDirectReference(normalizedProject, 'browser-direct-reference');
  assert.equal(direct.reference.versions.viennaps, '4.6.2');
  assert.equal(direct.reference.versions.viennals, '5.8.5');
  const initial = await compareNativeSnapshot(run.runId, 'initial', direct, 2);
  const final = await compareNativeSnapshot(run.runId, run.input.project.steps[0].id, direct, 2);
  assert.ok(final.outputs.some(item => item.kind === 'levelSet'));
  assert.ok(final.outputs.some(item => item.kind === 'levelSetMesh'));
  assert.ok(final.outputs.some(item => item.kind === 'nativeMetadata'));
  assert.ok(final.outputs.some(item => item.kind === 'surfaceMesh'));

  await page.getByLabel('当前结果步骤').selectOption(run.input.project.steps[0].id);
  await page.locator('.native-artifact-list summary').click();
  const artifactLinks = page.locator('.native-artifact-list a.button');
  await artifactLinks.first().waitFor();
  const artifactDownload = page.waitForEvent('download');
  await artifactLinks.first().click();
  const artifactPath = path.join(evidence, 'browser-native-output.download');
  await (await artifactDownload).saveAs(artifactPath);
  const browserBytes = await fs.readFile(artifactPath);
  assert.equal(createHash('sha256').update(browserBytes).digest('hex'), final.outputs[0].sha256);

  const saveResponse = page.waitForResponse(response => response.url().endsWith('/api/projects') && response.request().method() === 'POST');
  await page.getByRole('button', { name: '保存项目', exact: true }).click();
  assert.equal((await saveResponse).status(), 201);
  await waitForSaved(projectName);
  assert.equal(await page.getByLabel('基底材料').inputValue(), materialName);
  assert.equal(await page.getByLabel('输出原生 level-set').isChecked(), true);

  const replayDownload = page.waitForEvent('download');
  await page.getByRole('button', { name: '导出重放配置', exact: true }).click();
  const replayPath = path.join(evidence, 'browser-custom-material-replay.json');
  await (await replayDownload).saveAs(replayPath);
  const replay = JSON.parse(await fs.readFile(replayPath, 'utf8'));
  assert.equal(replay.normalizedProject.geometry.substrateMaterial, materialName);
  assert.equal(replay.normalizedProject.outputSettings.nativeMetadata, true);

  const projectDownload = page.waitForEvent('download');
  await page.getByRole('button', { name: '导出项目', exact: true }).click();
  const exportedProjectPath = path.join(evidence, 'browser-custom-material-project.json');
  await (await projectDownload).saveAs(exportedProjectPath);
  const exportedProject = JSON.parse(await fs.readFile(exportedProjectPath, 'utf8'));
  assert.equal(exportedProject.geometry.substrateMaterial, materialName);
  assert.ok(exportedProject.materialDefinitions.some(row => row.name === materialName && row.id === material.id && row.customId === material.customId));
  await page.getByRole('button', { name: '新建工程', exact: true }).click();
  await page.locator('input[type="file"][accept*="application/json"]').setInputFiles(exportedProjectPath);
  await page.getByText('项目 JSON 已导入。', { exact: true }).waitFor();
  assert.equal(await page.getByLabel('基底材料').inputValue(), materialName);
  assert.equal(await page.getByLabel('输出原生 level-set').isChecked(), true);

  const replayRequest = page.waitForRequest(item => item.url().endsWith('/api/runs/replay') && item.method() === 'POST');
  const replayResponse = page.waitForResponse(item => item.url().endsWith('/api/runs/replay') && item.request().method() === 'POST');
  await page.locator('input[type="file"][accept*="application/json"]').setInputFiles(replayPath);
  const [replaySent, replayReturned] = await Promise.all([replayRequest, replayResponse]);
  assert.equal(replayReturned.status(), 202, replayReturned.status() === 202 ? '' : await replayReturned.text());
  assert.ok(replaySent.postDataJSON().normalizedProject.materialDefinitions.some(row => row.name === materialName && row.id === material.id));
  const replayRunId = (await replayReturned.json()).runId;
  await waitForRun(replayRunId);
  await compareNativeSnapshot(replayRunId, 'initial', direct, 2);
  const replayedFinal = await compareNativeSnapshot(replayRunId, run.input.project.steps[0].id, direct, 2);
  assert.ok(replayedFinal.outputs.some(item => item.kind === 'nativeMetadata'));

  const zipDownload = page.waitForEvent('download');
  await page.getByRole('button', { name: '下载运行结果 ZIP', exact: true }).click();
  const runZipPath = path.join(evidence, 'browser-custom-material-run.zip');
  await (await zipDownload).saveAs(runZipPath);
  assert.ok((await fs.stat(runZipPath)).size > 0);

  const historyItem = page.locator('.history-item').filter({ hasText: run.runId.slice(0, 8) });
  await historyItem.waitFor();
  await historyItem.click();
  assert.equal((await apiSnapshot(run.runId, run.input.project.steps[0].id)).dimension, 2);
  await page.locator('.native-artifact-list').waitFor();
  assert.deepEqual(pageErrors, []);

  const report = {
    passed: true,
    browser: 'Chromium',
    service: baseUrl,
    material: { name: materialName, id: material.id, customId: material.customId },
    runs: { process: run.runId, replay: replayRunId },
    requestReadback: { dimension: run.input.project.dimension, geometry: run.input.project.geometry,
      step: run.input.project.steps[0], outputSettings: run.input.project.outputSettings },
    exactDirectViennaPSMatch: { initial: true, final: true, replayInitial: true, replayFinal: true, nativeOutputBytes: true },
    projectSaveReopenImportExport: true,
    runZip: runZipPath,
    pageErrors,
    consoleErrors,
    failedRequests,
  };
  await fs.writeFile(path.join(evidence, 'browser-live.json'), `${JSON.stringify(report, null, 2)}\n`);
  console.log(JSON.stringify({ passed: true, runs: report.runs, evidence: path.join(evidence, 'browser-live.json'), pageErrors }));
} catch (error) {
  console.error(JSON.stringify({ url: page.url(), bodyText: await page.locator('body').innerText().catch(() => ''),
    pageErrors, consoleErrors, failedRequests }, null, 2));
  throw error;
} finally {
  await context.close();
  await browser.close();
}
