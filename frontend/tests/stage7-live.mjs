/** Real Chromium Stage 7 acceptance: UI inputs, bounded native reference, flux, replay and 2D/3D. */
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { promisify } from 'node:util';
import { execFile } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const execFileAsync = promisify(execFile);
const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '../..');
const gpuMode = process.env.STAGE7_GPU === '1';
const verifiedEngine = gpuMode ? 'GPU_DISK' : 'CPU_DISK';
const evidence = path.join(root, gpuMode ? 'docs/stage9-evidence/browser-multiparticle' : 'docs/stage7-evidence/browser');
const baseUrl = (process.env.WORKBENCH_URL ?? 'http://127.0.0.1:8782').replace(/\/$/, '');
await fs.mkdir(evidence, { recursive: true });

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const context = await browser.newContext({ acceptDownloads: true, viewport: { width: 1680, height: 1200 } });
const page = await context.newPage();
page.setDefaultTimeout(25000);
const pageErrors = [];
const consoleErrors = [];
const failedRequests = [];
page.on('pageerror', error => pageErrors.push({ message: error.message, stack: error.stack }));
page.on('console', message => { if (message.type() === 'error') consoleErrors.push(message.text()); });
page.on('requestfailed', request => failedRequests.push({ url: request.url(), error: request.failure()?.errorText }));

async function fill(label, value) {
  const input = page.getByLabel(label, { exact: true });
  await input.fill(String(value));
  await input.press('Tab');
}

async function waitForRun(runId) {
  const terminal = new Set(['succeeded', 'failed', 'cancelled', 'timed_out', 'interrupted']);
  const deadline = Date.now() + 180000;
  let result;
  do {
    const response = await fetch(`${baseUrl}/api/runs/${runId}`, { cache: 'no-store' });
    assert.equal(response.status, 200);
    result = await response.json();
    if (terminal.has(result.status)) break;
    await new Promise(resolve => setTimeout(resolve, 200));
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

function directSignature(snapshot, dimension) {
  return {
    points: snapshot.points,
    topology: dimension === 3 ? { triangles: snapshot.cells } : { lines: snapshot.cells },
    materialIds: snapshot.materialIds,
    materialIdsByLevelSetIndex: snapshot.materialIdsByLevelSetIndex,
  };
}

async function createDirectReference(project, dimension, label) {
  const directory = path.join(evidence, label);
  await fs.mkdir(directory, { recursive: true });
  const projectPath = path.join(directory, 'project.json');
  const outputPath = path.join(directory, 'direct.json');
  await fs.writeFile(projectPath, `${JSON.stringify(project, null, 2)}\n`);
  await execFileAsync(path.join(root, '.venv/bin/python'), [path.join(root, 'scripts/bounded_child.py'),
    '--cwd', root, '--record', path.join(directory, 'direct.guard.json'), '--log-path', path.join(directory, 'direct.log'),
    '--timeout-seconds', '150', '--max-rss-mib', '1536', '--max-log-bytes', '300000', '--',
    path.join(root, '.venv/bin/python'), path.join(root, 'tests/fixtures/stage7_multiparticle_reference.py'), projectPath, outputPath, verifiedEngine],
  { cwd: root, timeout: 180000, maxBuffer: 1024 * 1024 });
  return { directory, projectPath, result: JSON.parse(await fs.readFile(outputPath, 'utf8')) };
}

async function compareRun(runId, project, direct, dimension) {
  const initial = await apiSnapshot(runId, 'initial');
  assert.deepEqual(apiSignature(initial), directSignature(direct.result.initial, dimension), `${dimension}D initial native mesh differs`);
  for (const step of project.steps.filter(row => row.enabled)) {
    const expected = direct.result.steps[step.id];
    assert.ok(expected, `direct ViennaPS reference lacks ${step.id}`);
    const actual = await apiSnapshot(runId, step.id);
    assert.deepEqual(apiSignature(actual), directSignature(expected.snapshot, dimension), `${dimension}D mesh mismatch for ${step.id}`);
    const recordResponse = await fetch(`${baseUrl}/api/runs/${runId}`);
    const record = await recordResponse.json();
    const actualStep = record.actualExecution.find(row => row.stepId === step.id);
    assert.ok(actualStep, `run history lacks actual execution for ${step.id}`);
    if (expected.noOp) {
      assert.equal(actualStep.noOp, true, `${step.id} should be a genuine no-op`);
      assert.equal(actualStep.actualEngine, 'not-applicable');
      assert.equal(actualStep.actualSeed ?? null, null);
      assert.equal(expected.callback.count, 0);
    } else {
      assert.equal(actualStep.model, 'multiParticle');
      assert.equal(actualStep.actualEngine, verifiedEngine);
      assert.equal(actualStep.actualSeed, project.solver.random.seed);
      const params = actualStep.actualParameters;
      assert.equal(params.multiParticle.nativeProcess, 'viennaps.MultiParticleProcess');
      assert.deepEqual(params.multiParticle.speciesOrder, step.species.map(item => item.label));
      assert.equal(params.cpuThreads, project.solver.cpuThreads);
      assert.equal(params.raysPerPoint, project.solver.raysPerPoint);
      const callback = params.rateCallbackSummary;
      assert.ok(callback?.invocationCount > 0, 'native rate callback was called');
      assert.deepEqual(callback.materials, expected.callback.materials);
      assert.equal(expected.callback.count, callback.invocationCount);
      assert.ok(expected.callback.samples.every(sample => step.operation === 'etch'
        ? sample.returnedVelocity < 0 : sample.returnedVelocity > 0), 'native callback sign follows the selected operation');
    }
  }
  return initial;
}

async function createDirectFluxReference(project, stepId, directory) {
  const outputPath = path.join(directory, `flux-${stepId}.json`);
  await execFileAsync(path.join(root, '.venv/bin/python'), [path.join(root, 'scripts/bounded_child.py'),
    '--cwd', root, '--record', path.join(directory, `flux-${stepId}.guard.json`), '--log-path', path.join(directory, `flux-${stepId}.log`),
    '--timeout-seconds', '90', '--max-rss-mib', '1536', '--max-log-bytes', '300000', '--',
    path.join(root, '.venv/bin/python'), path.join(root, 'tests/fixtures/stage7_multiparticle_flux_reference.py'),
    path.join(directory, 'project.json'), stepId, outputPath, verifiedEngine],
  { cwd: root, timeout: 120000, maxBuffer: 1024 * 1024 });
  return JSON.parse(await fs.readFile(outputPath, 'utf8'));
}

async function compareFlux(runId, direct) {
  const response = await fetch(`${baseUrl}/api/runs/${runId}/flux-result`);
  assert.equal(response.status, 200, response.status === 200 ? '' : await response.text());
  const flux = await response.json();
  assert.equal(flux.execution.actualEngine, verifiedEngine);
  assert.deepEqual(flux.points, direct.points);
  assert.deepEqual(flux.topology, direct.topology);
  assert.deepEqual(flux.pointData, direct.pointData);
  assert.deepEqual(flux.cellData, direct.cellData);
  assert.deepEqual(flux.fields, direct.fields);
  assert.deepEqual(flux.execution.rays.nativeRayTracingMetadata, direct.ray);
  assert.ok(direct.speciesOrder.every((label, index) => flux.fields.some(field => field.name.startsWith(`${label}${index}`))));
  return flux;
}

async function prepareRecipe({ includeNoop }) {
  await page.getByRole('button', { name: '＋ 添加多粒子步骤', exact: true }).click();
  const first = page.locator('.step-card').nth(0);
  await first.locator('.step-main').click();
  await page.getByLabel('粒子标签 1').fill('ArgonIonUI');
  await page.getByLabel('粒子标签 1').press('Tab');
  await page.getByLabel('粒子标签 2').fill('FluorineNeutralUI');
  await page.getByLabel('粒子标签 2').press('Tab');
  await fill('sourcePower', 1.1);
  await fill('thetaRMin', 2);
  await fill('thetaRMax', 52);
  await fill('minAngle', 58);
  await fill('B_sp', -0.5);
  await fill('meanEnergy', 100);
  await fill('sigmaEnergy', 12);
  await fill('thresholdEnergy', 8);
  await fill('inflectAngle', 22);
  await fill('n', 2);
  await fill('默认黏附概率', 0.4);
  await page.getByLabel('FluorineNeutralUI 新增材料黏附覆盖').selectOption('SiO2');
  await page.getByRole('button', { name: '＋ 按材料设置黏附', exact: true }).click();
  await fill('SiO2 黏附概率', 0.6);
  await page.getByLabel('多粒子新增速率材料').selectOption('SiO2');
  await page.getByRole('button', { name: '＋ 添加材料系数行', exact: true }).click();
  await fill('默认系数 · ArgonIonUI', 0.025);
  await fill('默认系数 · FluorineNeutralUI', 0.025);
  await fill('SiO2 · ArgonIonUI 系数', 0.02);
  await fill('SiO2 · FluorineNeutralUI 系数', 0.04);
  await fill('每点射线数', 64);
  await fill('随机种子', 271828);

  await page.getByRole('button', { name: '＋ 添加多粒子步骤', exact: true }).click();
  const second = page.locator('.step-card').nth(1);
  await second.locator('.step-main').click();
  await page.getByLabel('工艺操作').selectOption('deposit');
  await fill('每点射线数', 64);
  await fill('随机种子', 271828);

  if (includeNoop) {
    await page.getByRole('button', { name: '＋ 添加多粒子步骤', exact: true }).click();
    const third = page.locator('.step-card').nth(2);
    await third.locator('.step-main').click();
    await fill('默认系数 · argonIon', 0);
    await fill('默认系数 · fluorineNeutral', 0);
  }
}

async function saveProjectAndWait(name) {
  const response = page.waitForResponse(item => item.url().endsWith('/api/projects') && item.request().method() === 'POST');
  await page.getByRole('button', { name: '保存项目', exact: true }).click();
  assert.equal((await response).status(), 201);
  await page.waitForFunction(projectName => Array.from(document.querySelectorAll('select[aria-label="已保存项目"] option'))
    .some(option => option.textContent?.includes(projectName)), name);
}

try {
  await page.goto(`${baseUrl}/`);
  await page.getByText('本机服务已连接').waitFor();
  await page.getByLabel('工程维度').waitFor();
  await page.getByLabel('项目名称').fill(`Stage 7 Chromium ${Date.now()}`);
  await prepareRecipe({ includeNoop: true });
  if (gpuMode) await page.getByLabel('引擎偏好', { exact: true }).selectOption('gpu');
  const processRun = await submit('运行工艺', '/api/runs');
  const project2d = processRun.input.project;
  assert.equal(project2d.schemaVersion, 7);
  assert.equal(project2d.dimension, 2);
  assert.deepEqual(project2d.steps.map(step => step.model), ['multiParticle', 'multiParticle', 'multiParticle']);
  assert.equal(project2d.steps[0].operation, 'etch');
  assert.equal(project2d.steps[1].operation, 'deposit');
  assert.equal(project2d.solver.enginePreference, gpuMode ? 'gpu' : 'cpu');
  assert.equal(project2d.steps[0].species[0].sourcePower, 1.1);
  assert.equal(project2d.steps[0].species[0].thetaRMax, 52);
  assert.equal(project2d.steps[0].species[1].stickingProbabilityByMaterial.SiO2, 0.6);
  assert.equal(project2d.steps[0].rateCoefficients.byMaterial.SiO2.ArgonIonUI, 0.02);
  assert.equal(project2d.solver.random.seed, 271828);
  assert.equal(project2d.solver.raysPerPoint, 64);
  await waitForRun(processRun.runId);
  const direct2d = await createDirectReference(project2d, 2, '2d');
  assert.equal(direct2d.result.versions.viennaps, '4.6.2');
  assert.equal(direct2d.result.versions.viennals, '5.8.5');
  await compareRun(processRun.runId, project2d, direct2d, 2);

  await page.locator('.step-card').first().locator('.step-main').click();
  const directFlux = await createDirectFluxReference(project2d, project2d.steps[0].id, direct2d.directory);
  const fluxRun = await submit('单次计算所选粒子过程通量', '/api/runs/flux');
  assert.equal(fluxRun.input.stepId, project2d.steps[0].id);
  await waitForRun(fluxRun.runId);
  const flux = await compareFlux(fluxRun.runId, directFlux);

  const processHistoryItem = page.locator('.history-item').filter({ hasText: processRun.runId.slice(0, 8) });
  await processHistoryItem.waitFor();
  await processHistoryItem.click();
  await page.getByLabel('当前结果步骤').waitFor();
  await saveProjectAndWait(project2d.name);
  const savedOptions = await page.getByLabel('已保存项目').locator('option').evaluateAll(rows => rows.map(row => ({ value: row.value, text: row.textContent ?? '' })));
  const saved = savedOptions.find(row => row.value && row.text.includes(project2d.name));
  assert.ok(saved);
  await page.getByLabel('已保存项目').selectOption(saved.value);
  await page.getByRole('button', { name: '打开', exact: true }).click();
  await page.getByLabel('过程模型').waitFor();
  assert.equal(await page.getByLabel('过程模型').inputValue(), 'multiParticle');

  const replayDownload = page.waitForEvent('download');
  await page.getByRole('button', { name: '导出重放配置', exact: true }).click();
  const replayPath = path.join(direct2d.directory, 'replay-project.json');
  await (await replayDownload).saveAs(replayPath);
  const replay = JSON.parse(await fs.readFile(replayPath, 'utf8'));
  assert.equal(replay.executionRequirements.viennapsVersion, '4.6.2');
  assert.equal(replay.executionRequirements.viennalsVersion, '5.8.5');
  assert.equal(replay.executionRequirements.steps[0].engine, verifiedEngine);
  assert.equal(replay.executionRequirements.steps[0].seed, 271828);
  const replayRequest = page.waitForRequest(item => item.url().endsWith('/api/runs/replay') && item.method() === 'POST');
  const replayResponse = page.waitForResponse(item => item.url().endsWith('/api/runs/replay') && item.request().method() === 'POST');
  await page.locator('input[type="file"][accept*="application/json"]').setInputFiles(replayPath);
  const [replaySent, replayReturned] = await Promise.all([replayRequest, replayResponse]);
  assert.equal(replayReturned.status(), 202, replayReturned.status() === 202 ? '' : await replayReturned.text());
  assert.equal(replaySent.postDataJSON().normalizedProject.steps[0].model, 'multiParticle');
  const replayRunId = (await replayReturned.json()).runId;
  await waitForRun(replayRunId);
  await compareRun(replayRunId, project2d, direct2d, 2);

  const zipDownload = page.waitForEvent('download');
  await page.getByRole('button', { name: '下载运行结果 ZIP', exact: true }).click();
  const zipPath = path.join(direct2d.directory, 'stage7-run.zip');
  await (await zipDownload).saveAs(zipPath);
  assert.ok((await fs.stat(zipPath)).size > 0);

  const exportDownload = page.waitForEvent('download');
  await page.getByRole('button', { name: '导出项目', exact: true }).click();
  const projectExportPath = path.join(direct2d.directory, 'project-export.json');
  await (await exportDownload).saveAs(projectExportPath);
  const exportedProject = JSON.parse(await fs.readFile(projectExportPath, 'utf8'));
  assert.deepEqual(exportedProject.steps, project2d.steps);

  const historyItem = page.locator('.history-item').filter({ hasText: processRun.runId.slice(0, 8) });
  await historyItem.waitFor();
  await historyItem.click();
  assert.equal((await apiSnapshot(processRun.runId, project2d.steps[1].id)).dimension, 2);

  await page.getByRole('button', { name: '切换到 3D', exact: true }).click();
  await page.getByLabel('工程维度').getByRole('button', { name: '切换到 3D' }).waitFor();
  assert.equal(await page.getByLabel('工程维度').getByRole('button', { name: '切换到 3D' }).getAttribute('aria-pressed'), 'true');
  await fill('每点射线数', 64);
  await fill('随机种子', 271828);
  const run3d = await submit('运行工艺', '/api/runs');
  const project3d = run3d.input.project;
  assert.equal(project3d.dimension, 3);
  assert.equal(project3d.steps[0].model, 'multiParticle');
  assert.equal(project3d.solver.enginePreference, gpuMode ? 'gpu' : 'cpu');
  await waitForRun(run3d.runId);
  const direct3d = await createDirectReference(project3d, 3, '3d');
  await compareRun(run3d.runId, project3d, direct3d, 3);

  await page.locator('.step-card').first().locator('.step-main').click();
  const directFlux3d = await createDirectFluxReference(project3d, project3d.steps[0].id, direct3d.directory);
  const flux3dRun = await submit('单次计算所选粒子过程通量', '/api/runs/flux');
  await waitForRun(flux3dRun.runId);
  const flux3d = await compareFlux(flux3dRun.runId, directFlux3d);
  assert.deepEqual(pageErrors, []);

  const report = {
    passed: true, engine: verifiedEngine,
    browser: 'Chromium', service: baseUrl,
    runs: { process2d: processRun.runId, flux2d: fluxRun.runId, replay2d: replayRunId,
      process3d: run3d.runId, flux3d: flux3dRun.runId },
    inputReadback: { project2d, project3d },
    exactDirectViennaPSMatch: { process2d: true, process3d: true, replay2d: true, flux2d: true, flux3d: true },
    outputFields: { flux2d: flux.fields.map(field => field.name), flux3d: flux3d.fields.map(field => field.name) },
    zeroRateNoop: project2d.steps[2].id,
    savedProjectReopened: true, historyReloaded: true, projectExport: projectExportPath, runZip: zipPath,
    pageErrors, consoleErrors, failedRequests,
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
