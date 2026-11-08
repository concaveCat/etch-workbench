/** Real Chromium acceptance of saved-Domain continuation and portable state packages. */
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '../..');
const evidence = path.join(root, 'docs/stage3-evidence');
const baseUrl = (process.env.WORKBENCH_URL ?? 'http://127.0.0.1:8779').replace(/\/$/, '');
await fs.mkdir(evidence, { recursive: true });

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const context = await browser.newContext({ acceptDownloads: true, viewport: { width: 1600, height: 1000 } });
const page = await context.newPage();
page.setDefaultTimeout(20000);
const pageErrors = [];
page.on('pageerror', error => pageErrors.push(error.message));
page.on('close', () => console.log('Chromium page closed'));
browser.on('disconnected', () => console.log('Chromium browser disconnected'));

async function waitForRun(runId) {
  const terminal = new Set(['succeeded', 'failed', 'cancelled', 'timed_out', 'interrupted']);
  const deadline = Date.now() + 120000;
  let run;
  do {
    const response = await fetch(`${baseUrl}/api/runs/${runId}`, { cache: 'no-store' });
    assert.equal(response.status, 200, `GET run ${runId} returned HTTP ${response.status}`);
    run = await response.json();
    if (terminal.has(run.status)) break;
    await new Promise(resolve => setTimeout(resolve, 200));
  } while (Date.now() < deadline);
  assert.ok(terminal.has(run?.status), `run ${runId} did not finish within 120 seconds`);
  assert.equal(run.status, 'succeeded', run.failureReason ?? `run ended as ${run.status}`);
  return run;
}

async function startFromUi() {
  const requestPromise = page.waitForRequest(request => request.url().endsWith('/api/runs') && request.method() === 'POST');
  const responsePromise = page.waitForResponse(response => response.url().endsWith('/api/runs') && response.request().method() === 'POST');
  await page.getByRole('button', { name: '运行工艺' }).click();
  const [request, response] = await Promise.all([requestPromise, responsePromise]);
  assert.equal(response.status(), 202, await response.text());
  return { sent: request.postDataJSON().project, runId: (await response.json()).runId };
}

async function snapshot(runId, stepId) {
  const response = await fetch(`${baseUrl}/api/runs/${runId}/snapshots/${stepId}`);
  assert.equal(response.status, 200);
  return response.json();
}

function compareSnapshot(actual, expected, dimension) {
  assert.deepEqual(actual[dimension === 2 ? 'lines' : 'triangles'], expected.cells);
  assert.deepEqual(actual.cellData.materialIds, expected.materialIds);
  assert.deepEqual(actual.materialIdsByLevelSetIndex, expected.materialMap);
  assert.deepEqual(actual.points, expected.points);
  assert.equal(actual.gridDelta, expected.gridDelta);
}

async function directReference(projectInput, label) {
  const input = path.join(evidence, `${label}-direct-input.json`);
  const output = path.join(evidence, `${label}-direct-reference.json`);
  const state = path.join(evidence, `${label}-direct-reference.vpsd`);
  const guard = path.join(evidence, `${label}-reference-guard.json`);
  const log = path.join(evidence, `${label}-reference.log`);
  await fs.writeFile(input, `${JSON.stringify(projectInput, null, 2)}\n`);
  const child = spawnSync(path.join(root, '.venv/bin/python'), [path.join(root, 'scripts/bounded_child.py'), '--cwd', root,
    '--record', guard, '--log-path', log, '--timeout-seconds', '60', '--max-rss-mib', '2048', '--max-log-bytes', '2097152', '--',
    path.join(root, '.venv/bin/python'), path.join(root, 'tests/fixtures/domain_continuation_direct_reference.py'), input, output, state],
  { encoding: 'utf8', timeout: 70000 });
  assert.equal(child.status, 0, `${child.stderr}\n${await fs.readFile(log, 'utf8').catch(() => '')}`);
  const guardRecord = JSON.parse(await fs.readFile(guard, 'utf8'));
  assert.equal(guardRecord.terminationReason, 'exited');
  assert.deepEqual(guardRecord.remainingOwnedGroupMembers, []);
  return JSON.parse(await fs.readFile(output, 'utf8'));
}

try {
  await page.goto(`${baseUrl}/`);
  await page.getByText('本机服务已连接').waitFor();
  const results = [];
  const dimensions = (process.env.DOMAIN_ACCEPTANCE_DIMENSIONS ?? '2,3').split(',').map(Number);
  for (const dimension of dimensions) {
    console.log(`Chromium Stage 3 ${dimension}D: starting from browser project input`);
    const projectName = `Domain状态起始 Chromium ${dimension}D ${Date.now()}`;
    const projectInput = {
      schemaVersion: 5, name: projectName, dimension,
      units: { length: 'um', time: 's', rate: 'um/s', angle: 'deg' },
      domain: { gridDelta: 0.2, xExtent: 4, ...(dimension === 2 ? { xBoundary: 'reflective' } : { yExtent: 4, boundaryXY: 'reflective' }) },
      geometry: { kind: 'plane', height: 0, substrateMaterial: 'Si' },
      steps: [{ id: 'step_first_etch', name: '保存态前的刻蚀', enabled: true, operation: 'etch', model: 'isotropic',
        rateMagnitude: 0.2, duration: 0.4, rateMode: 'uniform' }],
      solver: { timeStepRatio: 0.25, raysPerPoint: 32, cpuThreads: 1, random: { mode: 'fixed', seed: 314159 },
        enginePreference: 'cpu', timeoutSeconds: 90 },
      ...(dimension === 2 ? { view: { panX: 0, panH: 0, zoom: 1, showGrid: true } } : { view3D: { projection: 'parallel', camera: null,
        displayMode: 'surface', visibleMaterialIds: [], opacityByMaterial: {}, clip: { enabled: false, axis: 'x', position: 0, keep: 'positive' },
        section: { enabled: false, axis: 'y', position: 0 } } }),
    };
    const sourcePath = path.join(evidence, `browser-continuation-${dimension}d-project.json`);
    const projectExport = path.join(evidence, `browser-continuation-${dimension}d-project.zip`);
    const replayExport = path.join(evidence, `browser-continuation-${dimension}d-replay.zip`);
    await fs.writeFile(sourcePath, `${JSON.stringify(projectInput, null, 2)}\n`);
    await page.locator('input[type=file]').setInputFiles(sourcePath);
    await page.waitForFunction(name => document.querySelector('input[aria-label="项目名称"]')?.value === name, projectName);

    const first = await startFromUi();
    assert.equal(first.sent.schemaVersion, 5);
    assert.equal(first.sent.dimension, dimension);
    const firstRun = await waitForRun(first.runId);
    assert.equal(firstRun.runMode, 'from-initial');
    await page.getByTitle('刷新记录').click();
    const firstHistory = page.locator('.history-item').filter({ hasText: first.runId.slice(0, 8) });
    await firstHistory.waitFor();
    await firstHistory.click();
    await page.getByRole('button', { name: '以此状态开始新工艺' }).waitFor();
    const firstRow = firstRun.snapshots.find(row => row.stepId === 'step_first_etch');
    assert.ok(firstRow?.nativeState?.stateId);
    await page.getByRole('button', { name: '以此状态开始新工艺' }).click();
    await page.getByText(/新工艺起始状态.*step_first_etch/).waitFor();
    await page.getByRole('button', { name: '＋ 添加各向同性步骤' }).click();
    await page.getByLabel('工艺操作').selectOption('deposit');
    await page.getByLabel('沉积材料').selectOption('W');
    await page.getByLabel('速率大小').fill('0.2');
    await page.getByLabel('速率大小').press('Tab');
    await page.getByLabel('持续时间').fill('0.4');
    await page.getByLabel('持续时间').press('Tab');

    const continued = await startFromUi();
    console.log(`Chromium Stage 3 ${dimension}D: continuation submitted`);
    assert.equal(continued.sent.schemaVersion, 6);
    assert.equal(continued.sent.dimension, dimension);
    assert.equal(continued.sent.continuationState.stateId, firstRow.nativeState.stateId);
    assert.equal(continued.sent.steps.length, 1);
    assert.equal(continued.sent.steps[0].operation, 'deposit');
    assert.equal(continued.sent.steps[0].depositMaterial, 'W');
    const continuedRun = await waitForRun(continued.runId);
    assert.equal(continuedRun.runMode, 'from-domain-state');
    assert.equal(continuedRun.initialState.stateId, firstRow.nativeState.stateId);

    const directProject = structuredClone(projectInput);
    directProject.steps = [projectInput.steps[0], continued.sent.steps[0]];
    const direct = await directReference(directProject, `browser-continuation-${dimension}d`);
    console.log(`Chromium Stage 3 ${dimension}D: direct reference compared`);
    compareSnapshot(await snapshot(continued.runId, 'initial'), direct.afterRead, dimension);
    compareSnapshot(await snapshot(continued.runId, continued.sent.steps[0].id), direct.resumed, dimension);

    // Local save/open and project export must keep the content-addressed state.
    const saveResponse = page.waitForResponse(response => response.url().endsWith('/api/projects') && response.request().method() === 'POST');
    await page.getByRole('button', { name: '保存项目' }).click();
    assert.equal((await saveResponse).status(), 201);
    await page.getByLabel('已保存项目').locator('option').filter({ hasText: projectName }).first().waitFor({ state: 'attached' });
    const options = await page.getByLabel('已保存项目').locator('option').evaluateAll(items => items.map(item => ({ value: item.value, text: item.textContent ?? '' })));
    const saved = options.find(item => item.value && item.text.includes(projectName));
    assert.ok(saved);
    await page.getByLabel('已保存项目').selectOption(saved.value);
    await page.getByRole('button', { name: '打开', exact: true }).click();
    await page.locator('.step-card').first().locator('.step-main').click();
    await page.getByLabel('工艺操作').waitFor();
    const projectDownload = page.waitForEvent('download');
    await page.getByRole('button', { name: '导出项目' }).click();
    await (await projectDownload).saveAs(projectExport);
    assert.equal((await fs.readFile(projectExport)).subarray(0, 2).toString('ascii'), 'PK');
    const packageProbe = spawnSync(path.join(root, '.venv/bin/python'), ['-c',
      'import json,sys,zipfile; z=zipfile.ZipFile(sys.argv[1]); p=json.loads(z.read("project.json")); print(json.dumps({"names":sorted(z.namelist()),"schemaVersion":p["schemaVersion"],"stateId":p["continuationState"]["stateId"],"paths":("/" in p["continuationState"]["stateId"])}))', projectExport],
    { encoding: 'utf8' });
    assert.equal(packageProbe.status, 0, packageProbe.stderr);
    const projectPackage = JSON.parse(packageProbe.stdout);
    console.log(`Chromium Stage 3 ${dimension}D: project state package exported`);
    assert.deepEqual(projectPackage.names, ['domain-state.vpsd', 'project.json']);
    assert.equal(projectPackage.schemaVersion, 6);
    assert.equal(projectPackage.stateId, firstRow.nativeState.stateId);

    const importResponsePromise = page.waitForResponse(response => response.url().endsWith('/api/continuation-package/import') && response.request().method() === 'POST');
    await page.locator('input[type=file]').setInputFiles(projectExport);
    const importedResponse = await importResponsePromise;
    assert.equal(importedResponse.status(), 200);
    await page.getByText('ViennaPS Domain 状态包已校验并导入').waitFor();
    console.log(`Chromium Stage 3 ${dimension}D: project state package imported in UI`);
    await page.getByText(new RegExp(firstRow.nativeState.stateId.slice(0, 12))).waitFor().catch(() => {});

    // A state-backed replay is always exported with the exact .vpsd artifact.
    const replayDownloadEvent = page.waitForEvent('download');
    await page.getByRole('button', { name: '导出重放配置' }).click();
    await (await replayDownloadEvent).saveAs(replayExport);
    assert.equal((await fs.readFile(replayExport)).subarray(0, 2).toString('ascii'), 'PK');
    const replayProbe = spawnSync(path.join(root, '.venv/bin/python'), ['-c',
      'import json,sys,zipfile; z=zipfile.ZipFile(sys.argv[1]); r=json.loads(z.read("replay-project.json")); print(json.dumps({"names":sorted(z.namelist()),"mode":r["replayMode"],"stateId":r["normalizedProject"]["continuationState"]["stateId"]}))', replayExport],
    { encoding: 'utf8' });
    assert.equal(replayProbe.status, 0, replayProbe.stderr);
    const replayPackage = JSON.parse(replayProbe.stdout);
    assert.deepEqual(replayPackage.names, ['domain-state.vpsd', 'project.json', 'replay-project.json']);
    assert.equal(replayPackage.mode, 'from-domain-state');
    assert.equal(replayPackage.stateId, firstRow.nativeState.stateId);

    const packageImport = page.waitForResponse(response => response.url().endsWith('/api/continuation-package/import') && response.request().method() === 'POST');
    console.log(`Chromium Stage 3 ${dimension}D: replay package ready for UI import`);
    const replayStart = page.waitForResponse(response => response.url().endsWith('/api/runs/replay') && response.request().method() === 'POST');
    await page.locator('input[type=file]').setInputFiles(replayExport);
    const packageImported = await packageImport;
    assert.equal(packageImported.status(), 200);
    const replayResponse = await replayStart;
    assert.equal(replayResponse.status(), 202);
    const replayRunId = (await replayResponse.json()).runId;
    const replayRun = await waitForRun(replayRunId);
    console.log(`Chromium Stage 3 ${dimension}D: replay run completed`);
    assert.equal(replayRun.runMode, 'from-domain-state');
    compareSnapshot(await snapshot(replayRunId, continued.sent.steps[0].id), direct.resumed, dimension);
    assert.deepEqual(pageErrors, []);

    results.push({ dimension, sourceRunId: first.runId, continuationRunId: continued.runId, replayRunId,
      schemaSubmitted: continued.sent.schemaVersion, replayMode: replayRun.runMode,
      stateRoundtripDirectMatch: true, continuedOutputDirectMatch: true,
      projectPackageEntries: projectPackage.names, replayPackageEntries: replayPackage.names,
      browserPageErrors: pageErrors });
    await fs.writeFile(path.join(evidence, `browser-continuation-${dimension}d.json`), `${JSON.stringify(results.at(-1), null, 2)}\n`);
  }
  const report = { status: 'passed', browser: 'Chromium', baseUrl, results, pageErrors };
  await fs.writeFile(path.join(evidence, 'browser-continuation-live.json'), `${JSON.stringify(report, null, 2)}\n`);
  console.log(JSON.stringify(report, null, 2));
} finally {
  await context.close();
  await browser.close();
}
