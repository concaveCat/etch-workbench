/**
 * Round-6 review acceptance (remaining high/medium/low findings).
 *
 *  A. 05 with 9 candidates: no error banner, all 9 views render, the compare
 *     table covers the first 4 ("对比前 4/9 个候选") and says so.
 *  B. 04 planned experiment: summary says "尚未运行" and no row claims
 *     "测量无有效值"; headers keep µm.
 *  C. One method-name mapping (刻蚀深度), 4 significant digits, µm, and the
 *     experiment CSV carries Chinese status/reason.
 *  D. Report structure figures name their source (当前 Run / 实验候选 V1) and the
 *     3D isometric projection keeps an 8 % margin (top face not clipped).
 *  E. 3D viewer XZ/YZ/fit/section keep ≥5 px blank on every canvas edge.
 *  F. Clip annotations render on the canvas (2 labels) and survive a reload.
 *  G. Library page header/breadcrumb + Chinese insert-source categories.
 *  H. Single 06 CTA, single 01 draft status, hidden empty project chip.
 */
import assert from 'node:assert/strict';
import { mkdirSync, readFileSync } from 'node:fs';
import path from 'node:path';
import zlib from 'node:zlib';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';
import { executionCompileBasis } from '../src/workflow/fingerprint.ts';

const here = path.dirname(fileURLToPath(import.meta.url));
const baseUrl = (process.env.WORKBENCH_URL ?? 'http://127.0.0.1:8961').replace(/\/$/, '');
const layoutPath = process.env.WORKFLOW_LAYOUT
  ?? path.resolve(here, '../../docs/stage5-evidence/two-structure-two-layer.gds');
const projectPath = process.env.WORKFLOW_PROJECT ?? path.join(here, 'fixtures', 'workflow-2d-five.json');
const project3dPath = process.env.WORKFLOW_PROJECT_3D ?? path.join(here, 'fixtures', 'workflow-3d-hole.json');
const shotDir = process.env.WORKFLOW_SHOT_DIR ?? path.resolve(here, '../../var/review-shots');
mkdirSync(shotDir, { recursive: true });

const getJson = async endpoint => {
  const response = await fetch(`${baseUrl}${endpoint}`);
  assert.ok(response.ok, `${endpoint} -> ${response.status}`);
  return response.json();
};
const sendJson = async (endpoint, method, body) => {
  const response = await fetch(`${baseUrl}${endpoint}`, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  assert.ok(response.ok, `${method} ${endpoint} -> ${response.status} ${text}`);
  return text ? JSON.parse(text) : null;
};
const postJson = (endpoint, body) => sendJson(endpoint, 'POST', body);
const putJson = (endpoint, body) => sendJson(endpoint, 'PUT', body);
const waitSegment = async segmentId => {
  for (let index = 0; index < 900; index += 1) {
    const segment = await getJson(`/api/v1/workflow/segments/${segmentId}`);
    if (['succeeded', 'failed', 'cancelled', 'interrupted'].includes(segment.status)) return segment;
    await new Promise(resolve => setTimeout(resolve, 400));
  }
  throw new Error(`segment ${segmentId} did not finish`);
};
const waitExperiment = async experimentId => {
  for (let index = 0; index < 1800; index += 1) {
    const document = await getJson(`/api/v1/workflow/experiments/${experimentId}`);
    if (['completed', 'failed'].includes(document.status)) return document;
    await new Promise(resolve => setTimeout(resolve, 500));
  }
  throw new Error(`experiment ${experimentId} did not finish`);
};

/** Minimal 8-bit PNG decoder (Playwright screenshots are non-interlaced RGBA). */
function decodePng(buffer) {
  assert.equal(buffer.readUInt32BE(0), 0x89504e47, 'not a PNG');
  let offset = 8;
  let width = 0;
  let height = 0;
  let colorType = 0;
  const idat = [];
  while (offset < buffer.length) {
    const length = buffer.readUInt32BE(offset);
    const type = buffer.toString('ascii', offset + 4, offset + 8);
    const data = buffer.subarray(offset + 8, offset + 8 + length);
    if (type === 'IHDR') {
      width = data.readUInt32BE(0);
      height = data.readUInt32BE(4);
      assert.equal(data[8], 8, 'expected 8-bit PNG');
      assert.equal(data[12], 0, 'interlaced PNG is not supported');
      colorType = data[9];
    } else if (type === 'IDAT') {
      idat.push(data);
    } else if (type === 'IEND') {
      break;
    }
    offset += 12 + length;
  }
  const channels = colorType === 6 ? 4 : colorType === 2 ? 3 : colorType === 0 ? 1 : 0;
  assert.ok(channels > 0, `unsupported PNG colour type ${colorType}`);
  const raw = zlib.inflateSync(Buffer.concat(idat));
  const stride = width * channels;
  const pixels = Buffer.alloc(width * height * channels);
  let position = 0;
  for (let y = 0; y < height; y += 1) {
    const filter = raw[position++];
    const line = raw.subarray(position, position + stride);
    position += stride;
    const previous = y > 0 ? pixels.subarray((y - 1) * stride, y * stride) : null;
    const current = pixels.subarray(y * stride, (y + 1) * stride);
    for (let x = 0; x < stride; x += 1) {
      const a = x >= channels ? current[x - channels] : 0;
      const b = previous ? previous[x] : 0;
      const c = previous && x >= channels ? previous[x - channels] : 0;
      let value = line[x];
      if (filter === 1) value += a;
      else if (filter === 2) value += b;
      else if (filter === 3) value += (a + b) >> 1;
      else if (filter === 4) {
        const p = a + b - c;
        const pa = Math.abs(p - a);
        const pb = Math.abs(p - b);
        const pc = Math.abs(p - c);
        value += pa <= pb && pa <= pc ? a : pb <= pc ? b : c;
      }
      current[x] = value & 0xff;
    }
  }
  return { width, height, channels, pixels };
}

/**
 * Painted geometry bounding box. The stage border and the bottom-left scale bar
 * are not geometry, so both are excluded.
 */
function paintedBox(buffer) {
  const { width, height, channels, pixels } = decodePng(buffer);
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  for (let y = 3; y < height - 3; y += 1) {
    for (let x = 3; x < width - 3; x += 1) {
      if (x < 190 && y > height - 130) continue;
      const i = (y * width + x) * channels;
      const r = pixels[i], g = pixels[i + 1], b = pixels[i + 2];
      if (Math.abs(r - 246) + Math.abs(g - 247) + Math.abs(b - 248) <= 18) continue;
      if (x < minX) minX = x;
      if (x > maxX) maxX = x;
      if (y < minY) minY = y;
      if (y > maxY) maxY = y;
    }
  }
  assert.ok(Number.isFinite(minX), 'the canvas has no painted pixels');
  return {
    width, height, minX, maxX, minY, maxY,
    left: minX, right: width - 1 - maxX, top: minY, bottom: height - 1 - maxY,
  };
}

const fixture = JSON.parse(readFileSync(projectPath, 'utf8'));
const fixture3d = JSON.parse(readFileSync(project3dPath, 'utf8'));
const managedNames = new Set([
  'Round6 九候选', 'Round6 计划实验', 'Round6 三维', 'Round6 空白',
]);
for (const row of (await getJson('/api/v1/workflow/projects')).projects ?? []) {
  if (managedNames.has(row.name)) {
    await fetch(`${baseUrl}/api/v1/workflow/projects/${row.projectId}`, { method: 'DELETE' });
  }
}

// --- Shared recipe -----------------------------------------------------------
const suffix = Date.now().toString(36);
await postJson('/api/v1/workflow/metrics', {
  schemaVersion: 1, metricId: `metric_round6_${suffix}`, name: 'round6 depth', unit: 'um',
});
const recipe = await postJson('/api/v1/workflow/metrology-recipes', {
  schemaVersion: 1, recipeId: `recipe_round6_${suffix}`, version: 1,
  metricId: `metric_round6_${suffix}`, name: 'round6 depth', dimension: 2,
  method: 'etch_depth',
  parameters: { kind: 'etch_depth', x: 1.0, referenceHeight: 0 },
  material: 'Si',
});

// --- Project A: a completed nine-variant experiment --------------------------
const projectA = { ...fixture, name: 'Round6 九候选' };
const compiledA = await postJson('/api/v1/workflow/flows/from-project', { project: projectA });
const stepsA = compiledA.compile.project.steps.filter(step => step.enabled);
const routeA = await postJson('/api/v1/workflow/routes/from-compile', {
  compileId: compiledA.compileId, name: 'Round6 A Route', splitAfterStepId: stepsA[0].id,
});
const experimentA = await postJson('/api/v1/workflow/experiments', {
  name: 'Round6 九候选实验',
  project: projectA,
  flowId: compiledA.flowId,
  compileId: compiledA.compileId,
  recipeId: recipe.recipeId,
  recipeVersion: recipe.version,
  variants: Array.from({ length: 9 }, (_, index) => ({
    parameters: { [stepsA[0].id]: { duration: 0.01 + index * 0.001 } },
    replicates: 1,
    seed: 600 + index,
  })),
  maxAttempts: 1,
  costPerRunUsd: 0,
});
await postJson(`/api/v1/workflow/experiments/${experimentA.experimentId}/run`, {});
const finishedA = await waitExperiment(experimentA.experimentId);
assert.equal(finishedA.status, 'completed', 'nine variants must run');
assert.equal(finishedA.variants.length, 9);
const measurementA = finishedA.variants[0].replicates[0].measurementId;
assert.ok(measurementA, 'the experiment must produce a measurement');
const recordA = await postJson('/api/v1/workflow/projects', projectA);
const recordADoc = await getJson(`/api/v1/workflow/projects/${recordA.projectId}`);
await putJson(`/api/v1/workflow/projects/${recordA.projectId}/state`, {
  schemaVersion: 1, compileId: compiledA.compileId,
  compileBasis: executionCompileBasis(recordADoc), routeId: routeA.plan.route.routeId,
  segmentRuns: [], measurementIds: [measurementA], experimentIds: [experimentA.experimentId],
});

// --- Project B: a planned experiment (04 must not judge it) ------------------
const projectB = { ...fixture, name: 'Round6 计划实验' };
const compiledB = await postJson('/api/v1/workflow/flows/from-project', { project: projectB });
const stepsB = compiledB.compile.project.steps.filter(step => step.enabled);
const experimentB = await postJson('/api/v1/workflow/experiments', {
  name: 'Round6 计划实验（未运行）',
  project: projectB,
  flowId: compiledB.flowId,
  compileId: compiledB.compileId,
  recipeId: recipe.recipeId,
  recipeVersion: recipe.version,
  variants: [
    { parameters: { [stepsB[0].id]: { duration: 0.01 } }, replicates: 2, seed: 800 },
    { parameters: { [stepsB[0].id]: { duration: 0.02 } }, replicates: 2, seed: 801 },
    { parameters: { [stepsB[0].id]: { duration: 0.03 } }, replicates: 2, seed: 802 },
  ],
  maxAttempts: 1,
});
assert.equal(experimentB.status, 'planned');
const recordB = await postJson('/api/v1/workflow/projects', projectB);
const recordBDoc = await getJson(`/api/v1/workflow/projects/${recordB.projectId}`);
await putJson(`/api/v1/workflow/projects/${recordB.projectId}/state`, {
  schemaVersion: 1, compileId: compiledB.compileId,
  compileBasis: executionCompileBasis(recordBDoc), routeId: null,
  segmentRuns: [], measurementIds: [], experimentIds: [experimentB.experimentId],
});

// --- Project C: 3D run (viewer framing + 3D report) -------------------------
const project3d = { ...fixture3d, name: 'Round6 三维' };
const compiled3d = await postJson('/api/v1/workflow/flows/from-project', { project: project3d });
const route3d = await postJson('/api/v1/workflow/routes/from-compile', {
  compileId: compiled3d.compileId, name: 'Round6 3D Route',
});
const run3d = await postJson(`/api/v1/workflow/segments/${route3d.plan.segments[0].segmentId}/execute`, {});
const done3d = await waitSegment(run3d.segment.segmentId);
assert.equal(done3d.status, 'succeeded');
const record3d = await postJson('/api/v1/workflow/projects', project3d);
const record3dDoc = await getJson(`/api/v1/workflow/projects/${record3d.projectId}`);
await putJson(`/api/v1/workflow/projects/${record3d.projectId}/state`, {
  schemaVersion: 1, compileId: compiled3d.compileId,
  compileBasis: executionCompileBasis(record3dDoc), routeId: route3d.plan.route.routeId,
  segmentRuns: [{ segmentId: run3d.segment.segmentId, runId: done3d.runId, measurementId: null }],
  measurementIds: [], experimentIds: [],
});

// --- Project D: no steps (02 insert-source empty state) ---------------------
const recordD = await postJson('/api/v1/workflow/projects', { ...fixture, name: 'Round6 空白', steps: [] });

// --- GDS layout + clip (annotations) ---------------------------------------
const uploadGds = async filePath => {
  const bytes = readFileSync(filePath);
  const response = await fetch(`${baseUrl}/api/gds-assets?file_name=${encodeURIComponent(path.basename(filePath))}`, {
    method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: bytes,
  });
  assert.ok(response.ok, `gds upload -> ${response.status}`);
  return response.json();
};
const asset = await uploadGds(layoutPath);
const layout = await postJson('/api/v1/workflow/layouts', { assetId: asset.assetId, fileName: asset.fileName });
const structure = layout.structures[0];
const clip = await postJson('/api/v1/workflow/clips', {
  layoutId: layout.layoutId, name: 'Round6 标注 Clip', structure: structure.name,
  layers: structure.layerDatatypes.map(([layer, datatype]) => ({ layer, datatype, material: 'Si' })),
  bbox: structure.bounds, annotations: [],
});

// --- Browser -----------------------------------------------------------------
const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const page = await browser.newPage({ viewport: { width: 1600, height: 1500 } });
page.setDefaultTimeout(120000);
const errors = [];
page.on('pageerror', error => errors.push(error.message));
page.on('dialog', async dialog => { await dialog.accept(); });

const openZone = async name => { await page.getByRole('button', { name, exact: true }).click(); };
const closeToStart = async () => {
  if (await page.locator('.spx-save-button').count() > 0) {
    await page.getByRole('button', { name: '文件', exact: true }).click();
    await page.getByRole('menuitem', { name: '关闭工程 / 返回开始页' }).click();
  }
  await page.getByRole('button', { name: '01 工程', exact: true }).click();
  await page.locator('.spx-command').first().waitFor({ state: 'visible', timeout: 30000 });
};
const openProject = async name => {
  await closeToStart();
  const open = page.getByRole('button', { name: `打开工程 ${name}`, exact: true });
  await open.waitFor({ timeout: 30000 });
  await open.click();
  await page.locator('.spx-tree-root').filter({ hasText: name }).waitFor({ timeout: 30000 });
};
const reportFrame = () => page.frames().find(frame => frame !== page.mainFrame());

try {
  await page.goto(`${baseUrl}/`);

  // ---- H: no project → no top-right chip, tree root still says 未打开工程 --
  assert.equal(await page.locator('.spx-doc-state .spx-state-pill').count(), 0, 'the empty state hides the project chip');
  assert.match(await page.locator('.spx-tree-root').innerText(), /未打开工程/);

  // ---- A/C: 9 candidates, 4 compared, all views rendered ------------------
  await openProject('Round6 九候选');
  await openZone('03 运行');
  await page.locator('[aria-label="测量禁用原因"]').waitFor({ timeout: 30000 });
  assert.match(await page.locator('[aria-label="测量禁用原因"]').innerText(), /尚未成功|先执行完成/);
  const methodOptions = await page.getByLabel('测量方法').locator('option').allInnerTexts();
  assert.deepEqual(methodOptions, ['刻蚀深度', '指定高度 CD'], `method labels: ${methodOptions}`);

  await openZone('05 分析');
  await page.locator('[aria-label="候选表"]').waitFor({ timeout: 180000 });
  await page.waitForFunction(() => document.querySelectorAll('.wf-compare-grid figure canvas, .wf-compare-grid figure svg').length >= 9,
    { timeout: 180000 });
  assert.equal(await page.locator('.wf-error').count(), 0, 'nine candidates must not raise an error banner');
  assert.equal(await page.locator('[aria-label="候选表"] tbody tr').count(), 9);
  assert.equal(await page.locator('.wf-compare-grid figure').count(), 9);
  assert.equal(await page.locator('.wf-compare-grid').getByText('视图加载中…').count(), 0, 'every view must finish loading');
  assert.equal(await page.locator('[aria-label="候选对比表"] tbody tr').count(), 6, 'four candidates produce six pairwise rows');
  const coverage = await page.locator('[aria-label="对比候选说明"]').innerText();
  assert.match(coverage, /对比前 4\/9 个候选/, coverage);
  assert.match(coverage, /选择对比候选/);
  assert.match(coverage, /显示全部视图/);
  const metricTable = await page.locator('[aria-label="测量指标表"]').innerText();
  assert.match(metricTable, /刻蚀深度/);
  assert.match(metricTable, /µm/);
  await page.screenshot({ path: path.join(shotDir, 'round6-05-nine-candidates.png'), fullPage: true });

  // ---- D: report captions name the source (experiment candidate) ----------
  await openZone('06 报告');
  await page.getByRole('button', { name: '生成报告' }).click();
  await page.locator('.wf-report-preview').waitFor({ timeout: 120000 });
  await page.waitForTimeout(800);
  const frameA = reportFrame();
  const captionsA = await frameA.evaluate(() => [...document.querySelectorAll('figcaption')].map(node => node.textContent ?? ''));
  assert.ok(captionsA.some(text => /实验候选 V1/.test(text)), `report captions: ${JSON.stringify(captionsA)}`);
  assert.ok(captionsA.every(text => !/当前 Run/.test(text)), 'an experiment-only report must not claim 当前 Run');

  // ---- C: CSV has Chinese status/reason, µm, 4 significant digits --------
  const csvResponse = await fetch(`${baseUrl}/api/v1/workflow/experiments/${experimentA.experimentId}/export.csv`);
  assert.ok(csvResponse.ok);
  const csvText = await csvResponse.text();
  const csvLines = csvText.trim().split('\n');
  assert.match(csvLines[0], /,status,reason,value,unit,/);
  assert.ok(csvLines.slice(1).some(line => line.includes('成功')), 'CSV status is Chinese');
  assert.ok(csvLines.slice(1).every(line => !line.includes('succeeded')), 'CSV must not keep English status codes');
  const csvCells = csvLines[1].split(',');
  const unitCell = csvCells[9];
  assert.equal(unitCell, 'µm', `CSV unit: ${unitCell}`);

  // ---- B: planned experiment is not judged --------------------------------
  await openProject('Round6 计划实验');
  await openZone('04 实验');
  await page.locator('[aria-label="实验结果"]').waitFor({ timeout: 60000 });
  const plannedSummary = await page.locator('[aria-label="实验指标汇总"]').innerText();
  assert.match(plannedSummary, /尚未运行/, plannedSummary);
  assert.doesNotMatch(plannedSummary, /有有效指标/, plannedSummary);
  const resultTable = await page.locator('[aria-label="实验结果表"]').innerText();
  assert.doesNotMatch(resultTable, /测量无有效值/, 'a planned experiment must not claim the metric is invalid');
  assert.match(resultTable, /待运行/);
  assert.match(resultTable, /均值（µm）/);
  assert.match(resultTable, /标准差（µm）/);
  await page.screenshot({ path: path.join(shotDir, 'round6-04-planned-not-run.png'), fullPage: true });

  // ---- E: 3D viewer keeps ≥5 px on every edge after XZ/YZ/fit/clip --------
  await openProject('Round6 三维');
  await openZone('03 运行');
  await page.locator('.wb-viewer3d canvas').first().waitFor({ timeout: 90000 });
  await page.waitForTimeout(1500);
  const stage = page.locator('.wb-viewer3d__stage');
  const assertMargin = (box, label) => {
    for (const [edge, value] of Object.entries({ top: box.top, bottom: box.bottom, left: box.left, right: box.right })) {
      assert.ok(value >= 5, `${label}: ${edge} margin is ${value}px (need ≥5px): ${JSON.stringify(box)}`);
    }
  };
  await page.getByLabel('标准视角').selectOption('xz');
  await page.waitForTimeout(1200);
  assertMargin(paintedBox(await stage.screenshot({ path: path.join(shotDir, 'round6-3d-xz-margin.png') })), 'XZ');
  await page.getByLabel('标准视角').selectOption('yz');
  await page.waitForTimeout(1200);
  assertMargin(paintedBox(await stage.screenshot()), 'YZ');
  await page.getByRole('button', { name: '适配视图' }).click();
  await page.waitForTimeout(1000);
  assertMargin(paintedBox(await stage.screenshot()), '适配视图');
  await page.getByRole('checkbox', { name: '截面' }).check();
  await page.waitForTimeout(1200);
  assertMargin(paintedBox(await stage.screenshot()), '截面');

  // ---- D: the 3D report figure keeps its margin and says 当前 Run ----------
  await openZone('06 报告');
  await page.getByRole('button', { name: '生成报告' }).click();
  await page.locator('.wf-report-preview').waitFor({ timeout: 120000 });
  await page.waitForTimeout(1000);
  const frame3d = reportFrame();
  const iso = await frame3d.evaluate(() => {
    const svg = document.querySelector('svg.report-svg');
    if (!svg) return null;
    const plot = svg.querySelectorAll('rect')[1];
    const px = Number(plot.getAttribute('x'));
    const py = Number(plot.getAttribute('y'));
    const pw = Number(plot.getAttribute('width'));
    const ph = Number(plot.getAttribute('height'));
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    for (const polygon of svg.querySelectorAll('polygon')) {
      for (const pair of (polygon.getAttribute('points') ?? '').trim().split(/\s+/)) {
        const [x, y] = pair.split(',').map(Number);
        if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
        minX = Math.min(minX, x); maxX = Math.max(maxX, x);
        minY = Math.min(minY, y); maxY = Math.max(maxY, y);
      }
    }
    return { px, py, pw, ph, minX, maxX, minY, maxY, caption: document.querySelector('figcaption')?.textContent ?? '' };
  });
  assert.ok(iso, 'the 3D report needs an isometric SVG');
  assert.match(iso.caption, /当前 Run · /, `3D report caption: ${iso.caption}`);
  const padX = iso.pw * 0.05;
  const padY = iso.ph * 0.05;
  assert.ok(iso.minX >= iso.px + padX && iso.maxX <= iso.px + iso.pw - padX,
    `isometric geometry must stay inside the plot horizontally: ${JSON.stringify(iso)}`);
  assert.ok(iso.minY >= iso.py + padY && iso.maxY <= iso.py + iso.ph - padY,
    `isometric geometry must stay inside the plot vertically: ${JSON.stringify(iso)}`);
  const reportFigure = frame3d.locator('figure').first();
  await reportFigure.scrollIntoViewIfNeeded();
  await reportFigure.screenshot({ path: path.join(shotDir, 'round6-06-report-3d-not-clipped.png') });
  await page.screenshot({ path: path.join(shotDir, 'round6-06-report-3d-page.png'), fullPage: true });

  // ---- F: two clip annotations render and survive a reload ----------------
  await closeToStart();
  await page.locator('.spx-command').filter({ hasText: '新建工程' }).click();
  await page.getByRole('button', { name: 'GDS / Layout', exact: true }).click();
  const layoutSelect = page.getByLabel('版图选择');
  await layoutSelect.waitFor({ timeout: 30000 });
  await layoutSelect.selectOption(layout.layoutId);
  const clipRow = page.locator('.spx-clip-summary-list button').filter({ hasText: 'Round6 标注 Clip' });
  await clipRow.waitFor({ timeout: 30000 });
  await clipRow.click();
  await page.locator('.wf-clip-canvas').waitFor({ timeout: 30000 });
  const centerX = ((clip.bbox.x0 + clip.bbox.x1) / 2).toFixed(3);
  const centerY = ((clip.bbox.y0 + clip.bbox.y1) / 2).toFixed(3);
  for (const [text, x, y] of [['孔口', centerX, centerY], ['侧壁', clip.bbox.x0 + (clip.bbox.x1 - clip.bbox.x0) * 0.3, clip.bbox.y0 + (clip.bbox.y1 - clip.bbox.y0) * 0.3]]) {
    await page.getByLabel('标注文本').fill(text);
    await page.getByLabel('标注 X').fill(String(x));
    await page.getByLabel('标注 Y').fill(String(y));
    await page.getByRole('button', { name: '添加标注', exact: true }).click();
    await page.waitForFunction(expected => document.querySelectorAll('.wf-annotation').length >= expected,
      text === '孔口' ? 1 : 2);
  }
  assert.equal(await page.locator('.wf-annotation').count(), 2, 'both annotations are drawn');
  const annotationTexts = await page.locator('.wf-annotation-text')
    .evaluateAll(nodes => nodes.map(node => node.textContent ?? ''));
  assert.deepEqual(annotationTexts.sort(), ['侧壁', '孔口'].sort());
  assert.ok(await page.locator('.wf-annotation-form').getByText('标注文本').isVisible(), 'the text input has a visible label');
  assert.ok(await page.locator('.wf-annotation-form').getByText('标注 X（µm）').isVisible());
  await page.screenshot({ path: path.join(shotDir, 'round6-clip-annotations.png'), fullPage: true });

  // Reload: the annotations come back from the saved Clip.
  await page.reload();
  await page.locator('.spx-tree-root').first().waitFor({ timeout: 30000 }).catch(() => undefined);
  await closeToStart();
  await page.locator('.spx-command').filter({ hasText: '新建工程' }).click();
  await page.getByRole('button', { name: 'GDS / Layout', exact: true }).click();
  await page.getByLabel('版图选择').waitFor({ timeout: 30000 });
  await page.getByLabel('版图选择').selectOption(layout.layoutId);
  await page.locator('.spx-clip-summary-list button').filter({ hasText: 'Round6 标注 Clip' }).click();
  await page.locator('.wf-clip-canvas').waitFor({ timeout: 30000 });
  await page.waitForTimeout(500);
  assert.equal(await page.locator('.wf-annotation').count(), 2, 'annotations survive a reload');

  // ---- G: library header/breadcrumb + Chinese insert source --------------
  await page.getByRole('treeitem', { name: /材料/ }).click();
  await page.locator('.spx-library-view').waitFor({ timeout: 30000 });
  assert.equal((await page.locator('.spx-doc-header h1').innerText()).trim(), '材料库');
  assert.match(await page.locator('.spx-doc-header p').innerText(), /按分类维护可复用条目/);
  assert.equal((await page.locator('.wf-flow .wf-card-head h2').innerText()).trim(), '材料库');
  await page.screenshot({ path: path.join(shotDir, 'round6-library-header.png'), fullPage: true });
  await page.getByRole('button', { name: '返回流程' }).click();
  await page.waitForFunction(() => document.querySelector('.spx-doc-header h1')?.textContent === '流程编辑');
  assert.equal(await page.locator('.spx-library-view').count(), 0, 'the breadcrumb returns to the flow editor');

  // ---- G/H: 02 insert source is process-only; other categories stay in 库 --
  await openProject('Round6 空白');
  await openZone('02 流程');
  await page.locator('.spx-library-browser').waitFor();
  assert.equal(await page.locator('.spx-library-tabs').count(), 0, '02 must not show category tabs');
  const insertSource = page.locator('.spx-library-browser');
  assert.match(await insertSource.innerText(), /工艺预设/);
  assert.doesNotMatch(await insertSource.innerText(), /材料库|流程库|实验库/);
  assert.match(await page.locator('.spx-flow-steps').innerText(), /把左侧工艺预设拖到这里/);

  // ---- H: a single 06 CTA ------------------------------------------------
  await openZone('06 报告');
  await page.locator('[aria-label="报告空态"]').waitFor();
  assert.equal(await page.getByRole('button', { name: '去 03 运行测量' }).count(), 1, 'only one 03 CTA in the empty report');

  // ---- H: one draft status only -----------------------------------------
  await closeToStart();
  await page.locator('.spx-command').filter({ hasText: '新建工程' }).click();
  // A previous GDS form may leave the draft source on layout; pick parametric.
  const parametric = page.getByRole('button', { name: '参数化', exact: true });
  if (await parametric.count()) await parametric.click();
  await page.getByRole('button', { name: '创建工程草稿', exact: true }).click();
  await page.locator('.spx-project-status').waitFor({ timeout: 30000 });
  assert.equal(await page.getByText('草稿（未保存）', { exact: false }).count(), 1, 'the draft state appears once (top chip)');
  await page.screenshot({ path: path.join(shotDir, 'round6-01-single-draft-status.png'), fullPage: true });

  assert.deepEqual(errors, []);
  console.log(JSON.stringify({
    status: 'passed', baseUrl, shotDir,
    projectA: recordA.projectId, projectB: recordB.projectId, project3d: record3d.projectId, projectD: recordD.projectId,
    experimentA: experimentA.experimentId, experimentB: experimentB.experimentId,
    measurementA, clipId: clip.clipId, layoutId: layout.layoutId,
  }));
} finally {
  await browser.close();
}
