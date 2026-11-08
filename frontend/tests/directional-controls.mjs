/** Offline Chromium check of the real directional form and schema serialization.
 * Backend/ViennaPS execution is covered separately by tests/test_directional.py.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { chromium } from 'playwright';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '../..');
const materials = JSON.parse(await fs.readFile(path.join(root, 'docs/materials-catalog.json'), 'utf8')).materials;
const browser = await chromium.launch({ headless: true, args: ['--no-sandbox', '--allow-file-access-from-files'] });
const context = await browser.newContext({ acceptDownloads: true, viewport: { width: 1600, height: 1000 } });
const page = await context.newPage();
const errors = [];
page.on('pageerror', error => errors.push(error.message));

await page.addInitScript(({ materials: catalog }) => {
  const runs = new Map();
  const projects = new Map();
  let nextRun = 0;
  let lastValidatedProject = null;
  const json = (data, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'content-type': 'application/json' } });
  const makeRun = project => {
    const runId = `browserdirectional${++nextRun}`;
    const steps = project.steps.filter(step => step.enabled);
    const snapshots = [{ stepId: 'initial', name: '初始结构', stepIndex: 0, snapshot: 'initial.json', vtk: 'initial.vtk', pointCount: 2, lineCount: 1, materialIds: [10] },
      ...steps.map((step, index) => ({ stepId: step.id, name: step.name, stepIndex: index + 1, snapshot: `${step.id}.json`, vtk: `${step.id}.vtk`, pointCount: 2, lineCount: 1, materialIds: [10, 70] }))];
    const actualExecution = steps.map(step => {
      const sign = step.operation === 'etch' ? -1 : 1;
      const length = Math.hypot(...step.direction);
      const direction = step.direction.map(value => value / length);
      return { stepId: step.id, model: step.model, operation: step.operation, noOp: false, actualEngine: 'not-applicable', actualSeed: null,
        actualRate: step.directionalRateMagnitude * sign,
        actualParameters: { requestedDirection: step.direction, nativeUnitDirection: direction,
          nativeDirectionalVelocity: step.directionalRateMagnitude * sign, nativeIsotropicVelocity: step.isotropicRateMagnitude * sign,
          calculateVisibility: step.calculateVisibility, maskMaterials: [], raysPerPoint: 'not-applicable', randomSeed: 'not-applicable' } };
    });
    const info = { runId, kind: 'simulate', status: 'succeeded', currentStepId: null, completedStepIds: steps.map(step => step.id),
      startedAt: new Date().toISOString(), elapsedSeconds: 0.2, requestedInputHash: 'browser-mock-hash', stepTotal: steps.length,
      actualExecution, log: { cursor: 0, nextCursor: 0, truncated: false, text: '' }, snapshots };
    const replay = { format: 'viennaps-replay', schemaVersion: project.schemaVersion, normalizedProject: project,
      executionRequirements: { viennapsVersion: '4.6.2', viennalsVersion: '5.8.5', cpuThreads: project.solver.cpuThreads, steps: [] } };
    runs.set(runId, { project, info, replay });
    return { runId, status: 'pending', requestedInputHash: info.requestedInputHash };
  };
  const snapshot = stepId => ({ schemaVersion: 1, dimension: 2, runId: 'browserdirectional', stepId,
    stepName: stepId, stepIndex: stepId === 'initial' ? 0 : 1, axisMap: { x: 0, h: 1, z: 2 }, coordinateUnit: 'um',
    points: [[-1, 0, 0], [1, 0, 0]], lines: [[0, 1]], cellData: { materialIds: [10] }, pointData: {},
    materials: { '10': { name: 'Si', color: '#777777', levelSetIndexes: [0], sourceStepId: null } },
    mesh: { vtk: `${stepId}.vtk`, pointCount: 2, lineCount: 1 }, gridDelta: 0.1, materialIdsByLevelSetIndex: [10], noOp: false });
  window.__directionalBrowserState = { get lastValidatedProject() { return lastValidatedProject; }, runs, projects };
  window.fetch = async (input, init = {}) => {
    const rawUrl = typeof input === 'string' ? input : input.url;
    const url = new URL(rawUrl, window.location.href);
    const pathname = url.pathname;
    const method = String(init.method ?? 'GET').toUpperCase();
    const body = init.body ? JSON.parse(init.body) : null;
    if (pathname === '/api/health') return json({ status: 'ok', viennaps: '4.6.2', viennals: '5.8.5' });
    if (pathname === '/api/capabilities') return json({ capabilities: {}, resourcePolicy: { run: { maximumSteps: 20 } } });
    if (pathname === '/api/materials') return json({ schemaVersion: 1, engineVersion: '4.6.2', materials: catalog });
    if (pathname === '/api/projects' && method === 'GET') return json({ projects: [...projects.values()].map(row => ({ id: row.id, name: row.project.name, updatedAt: new Date().toISOString() })) });
    if (pathname === '/api/runs' && method === 'GET') return json({ runs: [...runs.values()].map(row => ({ runId: row.info.runId, kind: row.info.kind, status: row.info.status, createdAt: row.info.startedAt })) });
    if (pathname === '/api/projects/validate' && method === 'POST') {
      lastValidatedProject = body;
      return json({ valid: true, warnings: [], requestedInputHash: 'browser-mock-hash', normalizedProject: body });
    }
    if (pathname === '/api/projects' && method === 'POST') {
      lastValidatedProject = body;
      projects.set('saved-directional', { id: 'saved-directional', project: body });
      return json({ id: 'saved-directional', project: body }, 201);
    }
    const projectMatch = pathname.match(/^\/api\/projects\/([^/]+)$/);
    if (projectMatch && method === 'GET') return json(projects.get(projectMatch[1]) ?? { detail: 'missing' }, projects.has(projectMatch[1]) ? 200 : 404);
    if (pathname === '/api/runs' && method === 'POST') {
      lastValidatedProject = body.project;
      return json(makeRun(body.project), 202);
    }
    if (pathname === '/api/runs/replay' && method === 'POST') {
      lastValidatedProject = body.normalizedProject;
      return json(makeRun(body.normalizedProject), 202);
    }
    const replayMatch = pathname.match(/^\/api\/runs\/([^/]+)\/replay-project$/);
    if (replayMatch) return json(runs.get(replayMatch[1])?.replay ?? { detail: 'missing' }, runs.has(replayMatch[1]) ? 200 : 404);
    const snapshotMatch = pathname.match(/^\/api\/runs\/([^/]+)\/snapshots\/([^/]+)$/);
    if (snapshotMatch) return json(snapshot(snapshotMatch[2]));
    const runMatch = pathname.match(/^\/api\/runs\/([^/]+)$/);
    if (runMatch) return json(runs.get(runMatch[1])?.info ?? { detail: 'missing' }, runs.has(runMatch[1]) ? 200 : 404);
    if (pathname.startsWith('/api/runs/') && pathname.endsWith('/download')) return new Response(new Blob(['offline browser fixture']), { status: 200 });
    return json({ detail: `unexpected offline test request ${method} ${pathname}` }, 404);
  };
}, { materials });

await page.goto(pathToFileURL(path.join(root, 'frontend/dist/index.html')).href);
await page.getByRole('button', { name: '＋ 添加定向步骤' }).click();
await page.getByLabel('项目名称').fill('浏览器定向过程');
await page.getByLabel('过程模型').selectOption('directional');
await page.getByLabel('工艺操作').selectOption('deposit');
await page.getByLabel('沉积材料').selectOption('W');
await page.getByLabel('方向 x 分量').fill('0.6');
await page.getByLabel('方向 x 分量').press('Tab');
await page.getByLabel('方向 h 分量').fill('-0.8');
await page.getByLabel('方向 h 分量').press('Tab');
await page.getByLabel('定向速率大小').fill('0.3');
await page.getByLabel('定向速率大小').press('Tab');
await page.getByLabel('各向同性速率分量').fill('0.05');
await page.getByLabel('各向同性速率分量').press('Tab');
await page.getByRole('button', { name: '运行工艺' }).click();
await page.getByText('DirectionalProcess 原生参数 · 历史记录').waitFor();

let sent = await page.evaluate(() => window.__directionalBrowserState.lastValidatedProject);
assert.equal(sent.schemaVersion, 5);
assert.equal(sent.dimension, 2);
assert.deepEqual(sent.steps[0], { id: sent.steps[0].id, name: '步骤 1', enabled: true, operation: 'deposit', model: 'directional',
  direction: [0.6, -0.8, 0], directionalRateMagnitude: 0.3, isotropicRateMagnitude: 0.05, calculateVisibility: true,
  duration: 0.2, depositMaterial: 'W' });

await page.getByRole('button', { name: '保存项目' }).click();
await page.getByLabel('已保存项目').selectOption('saved-directional');
await page.getByRole('button', { name: '打开', exact: true }).click();
assert.equal(await page.getByLabel('定向速率大小').inputValue(), '0.3');

const projectDownload = page.waitForEvent('download');
await page.getByRole('button', { name: '导出项目' }).click();
const projectFile = await projectDownload;
const projectPath = path.join('/tmp', 'directional-project-browser.json');
await projectFile.saveAs(projectPath);
assert.equal(JSON.parse(await fs.readFile(projectPath, 'utf8')).schemaVersion, 5);

const replayDownload = page.waitForEvent('download');
await page.getByRole('button', { name: '导出重放配置' }).click();
const replayFile = await replayDownload;
const replayPath = path.join('/tmp', 'directional-replay-browser.json');
await replayFile.saveAs(replayPath);
const replay = JSON.parse(await fs.readFile(replayPath, 'utf8'));
assert.equal(replay.schemaVersion, 5);
assert.deepEqual(replay.executionRequirements.steps, []);
const replayResponse = page.waitForResponse(response => response.url().includes('/api/runs/replay') && response.request().method() === 'POST');
await page.locator('input[type=file]').setInputFiles(replayPath);
assert.equal((await replayResponse).status(), 202);

await page.getByRole('button', { name: '切换到 3D' }).click();
await page.getByLabel('方向 z 分量').waitFor();
sent = await page.evaluate(() => window.__directionalBrowserState.lastValidatedProject);
assert.equal(sent.schemaVersion, 5);
assert.deepEqual(sent.steps[0].direction, [0.6, 0, -0.8]);
await page.getByRole('button', { name: '切换到 2D' }).click();
await page.getByLabel('方向 h 分量').waitFor();
sent = await page.evaluate(() => window.__directionalBrowserState.lastValidatedProject);
assert.deepEqual(sent.steps[0].direction, [0.6, -0.8, 0]);

assert.deepEqual(errors, []);
console.log(JSON.stringify({ status: 'passed', browser: 'Chromium', schemaVersion: 5,
  controls: ['direction components', 'directional rate', 'isotropic rate', 'visibility', 'operation', 'deposit material'],
  saveOpenExportReplay: true, dimensionMapping: true, apiMode: 'offline mocked; native execution covered by Python integration acceptance' }, null, 2));

await context.close();
await browser.close();
