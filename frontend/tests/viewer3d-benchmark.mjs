/**
 * viewer3d benchmark: headless Chromium against the standalone harness.
 *
 * No backend is involved. The harness generates synthetic snapshots (>= 200k
 * triangles, 5 materials, undulating surface) and exposes
 * `window.__viewer3dHarness` for step switching, frame timing and framebuffer
 * analysis.
 *
 * Usage: node tests/viewer3d-benchmark.mjs
 * Env:   VIEWER3D_BASE_URL   harness URL (default http://127.0.0.1:8811)
 *        VIEWER3D_BENCH_JSON report path (default ../../docs/viewer3d-benchmark.json)
 *        VIEWER3D_CDP_URL    attach to an already running browser over CDP
 *                            (for example Windows Edge with the RTX GPU) instead
 *                            of launching headless Chromium + SwiftShader
 */
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from '@playwright/test';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '../..');
const baseUrl = (process.env.VIEWER3D_BASE_URL ?? 'http://127.0.0.1:8811').replace(/\/$/, '');
const jsonPath = process.env.VIEWER3D_BENCH_JSON ?? path.join(root, 'docs/viewer3d-benchmark.json');
const cdpUrl = (process.env.VIEWER3D_CDP_URL ?? '').trim();
const SOFTWARE_RENDERER = /swiftshader|llvmpipe|softpipe|software|microsoft basic render|basic render driver/i;

const FRAME_BUDGET_MS = 33;
const FIRST_SWITCH_BUDGET_MS = 800;
const CACHED_SWITCH_BUDGET_MS = 150;

const report = {
  status: 'running',
  generatedAt: new Date().toISOString(),
  baseUrl,
  browser: cdpUrl ? `chromium via CDP (${cdpUrl})` : 'chromium headless (swiftshader)',
  environment: cdpUrl ? 'hardware-or-remote' : 'software',
  webglRenderer: null,
  webglVendor: null,
  triangles: 0,
  avgFrameMs: null,
  p50FrameMs: null,
  webglBaselineMs: null,
  apiMode: null,
  p95FrameMs: null,
  switchMs: { first: null, cached: null, heavy: null },
  staticFullDetailMs: null,
  materials: [],
  checks: [],
  consoleErrors: [],
  limitations: [],
};

// Keep the separate manual hardware run (RTX 3060 Laptop) across software runs.
let previousReport = {};
try {
  previousReport = JSON.parse(await fs.readFile(jsonPath, 'utf8'));
} catch {
  previousReport = {};
}
if (previousReport.hardware) report.hardware = previousReport.hardware;

const check = (name, data = {}) => {
  report.checks.push({ name, ...data });
  console.log(`PASS ${name}${Object.keys(data).length ? ` ${JSON.stringify(data)}` : ''}`);
};

let browser;
let launchedHere = false;
try {
  if (cdpUrl) {
    // A real desktop browser (Windows Edge on the RTX GPU) already exists; do
    // not launch a second, software-only browser. Reuse its default context so
    // the benchmark measures the same GPU the user sees.
    browser = await chromium.connectOverCDP(cdpUrl);
    report.browser = `chromium via CDP (${cdpUrl})`;
  } else {
    browser = await chromium.launch({
      headless: true,
      args: ['--no-sandbox', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'],
    });
    launchedHere = true;
  }
  const context = cdpUrl
    ? (browser.contexts()[0] ?? await browser.newContext({ viewport: { width: 1280, height: 800 } }))
    : await browser.newContext({ viewport: { width: 1280, height: 800 } });
  const page = await context.newPage();
  if (cdpUrl) await page.setViewportSize({ width: 1280, height: 800 });
  page.on('console', (message) => {
    if (message.type() === 'error') report.consoleErrors.push(message.text());
  });
  page.on('pageerror', (error) => report.consoleErrors.push(String(error)));

  await page.goto(`${baseUrl}/viewer3d-harness.html`, { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => window.__viewer3dHarness?.getState().ready === true, null, { timeout: 60000 });
  const initial = await page.evaluate(() => window.__viewer3dHarness.getState());
  report.triangles = initial.stats.triangles;
  // Read the real backend from the vtk canvas (no extra context is created).
  const rendererInfo = await page.evaluate(() => {
    const canvas = document.querySelector('.wb-viewer3d__canvas canvas') ?? document.createElement('canvas');
    const gl = canvas.getContext('webgl2') ?? canvas.getContext('webgl');
    if (!gl) return null;
    const debug = gl.getExtension('WEBGL_debug_renderer_info');
    return {
      renderer: String(debug ? gl.getParameter(debug.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER) ?? ''),
      vendor: String(debug ? gl.getParameter(debug.UNMASKED_VENDOR_WEBGL) : gl.getParameter(gl.VENDOR) ?? ''),
    };
  });
  if (rendererInfo) {
    report.webglRenderer = rendererInfo.renderer;
    report.webglVendor = rendererInfo.vendor;
    report.environment = SOFTWARE_RENDERER.test(`${rendererInfo.vendor} ${rendererInfo.renderer}`) ? 'software' : 'hardware';
    console.log(`WEBGL_RENDERER ${rendererInfo.renderer} (${report.environment})`);
  }
  assert.ok(report.triangles >= 200_000, `harness must render at least 200k triangles, got ${report.triangles}`);
  check('harness ready', { triangles: report.triangles, parseMs: initial.stats.parseMs });

  // --- pixels + material colours -------------------------------------------
  const pixels = await page.evaluate(() => window.__viewer3dHarness.analyze());
  assert.ok(pixels, 'framebuffer readback failed');
  assert.ok(pixels.nonBackground > 5000, `canvas looks empty (${pixels.nonBackground} painted pixels)`);
  assert.ok(pixels.uniqueColors > 20, `canvas is flat (${pixels.uniqueColors} quantised colours)`);
  const presentMaterials = pixels.materials.filter((entry) => entry.pixels > 200);
  assert.equal(presentMaterials.length, 5, `expected 5 material colours, got ${JSON.stringify(pixels.materials)}`);
  report.materials = pixels.materials;
  check('canvas has pixels and all 5 material colours', {
    painted: pixels.nonBackground,
    uniqueColors: pixels.uniqueColors,
    size: `${pixels.width}x${pixels.height}`,
  });

  // --- interaction frames (camera drag, interaction LOD active) -------------
  const spin = await page.evaluate(() => window.__viewer3dHarness.spin(120));
  report.avgFrameMs = Number(spin.avg.toFixed(2));
  report.p50FrameMs = Number(spin.p50.toFixed(2));
  report.p95FrameMs = Number(spin.p95.toFixed(2));
  check('120 rotating frames measured', {
    avgMs: report.avgFrameMs,
    p50Ms: report.p50FrameMs,
    p95Ms: report.p95FrameMs,
    maxMs: Number(spin.max.toFixed(2)),
    fps: Number(spin.fps.toFixed(1)),
  });

  // --- full-detail static frames (LOD released) -----------------------------
  const statics = await page.evaluate(() => window.__viewer3dHarness.staticFrames(20));
  report.staticFullDetailMs = { avg: Number(statics.avg.toFixed(2)), p95: Number(statics.p95.toFixed(2)) };
  check('full-detail static frames', report.staticFullDetailMs);

  // --- step switching -------------------------------------------------------
  const firstSwitch = await page.evaluate(() => window.__viewer3dHarness.switchStep('step-2'));
  report.switchMs.first = Number(firstSwitch.ms.toFixed(1));
  assert.ok(report.switchMs.first < FIRST_SWITCH_BUDGET_MS, `first step switch took ${report.switchMs.first} ms`);
  check('first switch (cold parse)', { ms: report.switchMs.first, parseMs: firstSwitch.stats.parseMs });

  const cachedSwitch = await page.evaluate(() => window.__viewer3dHarness.switchStep('step-1'));
  report.switchMs.cached = Number(cachedSwitch.ms.toFixed(1));
  assert.ok(report.switchMs.cached < CACHED_SWITCH_BUDGET_MS, `cached step switch took ${report.switchMs.cached} ms`);
  check('cached switch (LRU hit)', { ms: report.switchMs.cached, parseMs: cachedSwitch.stats.parseMs });

  const afterSwitch = await page.evaluate(() => window.__viewer3dHarness.analyze());
  assert.ok(afterSwitch.nonBackground > 5000, 'canvas is empty after switching back');
  check('canvas still painted after switching', { painted: afterSwitch.nonBackground });

  // --- heavy step: vertex-clustering simplification -------------------------
  const heavy = await page.evaluate(() => window.__viewer3dHarness.switchStep('step-3'));
  report.switchMs.heavy = Number(heavy.ms.toFixed(1));
  report.simplification = {
    from: heavy.stats.sourceTriangles,
    to: heavy.stats.triangles,
    ms: report.switchMs.heavy,
  };
  assert.equal(heavy.stats.simplified, true, 'the 700k triangle step must be simplified');
  assert.ok(heavy.stats.triangles <= 300_000, `simplified mesh still has ${heavy.stats.triangles} triangles`);
  assert.ok(heavy.stats.sourceTriangles >= 700_000, `unexpected source triangle count ${heavy.stats.sourceTriangles}`);
  check('vertex clustering keeps the heavy step under budget', {
    ms: report.switchMs.heavy,
    from: heavy.stats.sourceTriangles,
    to: heavy.stats.triangles,
  });
  const heavyPixels = await page.evaluate(() => window.__viewer3dHarness.analyze());
  assert.ok(heavyPixels.nonBackground > 5000, 'simplified mesh did not render');

  // --- clipping plane is real ----------------------------------------------
  // The viewer re-fits the camera to the visible half, so the painted pixel
  // count no longer has to shrink; the visible *bounds* prove the cut.
  await page.evaluate(() => window.__viewer3dHarness.switchStep('step-1'));
  const beforeClip = await page.evaluate(() => window.__viewer3dHarness.analyze().nonBackground);
  const fullBounds = await page.evaluate(() => window.__viewer3dHarness.visibleBounds());
  await page.getByLabel('截面', { exact: true }).check();
  await page.waitForTimeout(400);
  const afterClip = await page.evaluate(() => window.__viewer3dHarness.analyze().nonBackground);
  const clippedBounds = await page.evaluate(() => window.__viewer3dHarness.visibleBounds());
  await page.getByLabel('截面', { exact: true }).uncheck();
  assert.ok(fullBounds && clippedBounds, 'visible bounds must be available for the clip check');
  const fullSpanX = fullBounds[3] - fullBounds[0];
  const clippedSpanX = clippedBounds[3] - clippedBounds[0];
  assert.ok(clippedSpanX < fullSpanX * 0.75,
    `clipping did not remove geometry (x span ${fullSpanX} -> ${clippedSpanX})`);
  assert.ok(afterClip > 5000, `the clipped half must stay visible (${beforeClip} -> ${afterClip} painted pixels)`);
  check('X/Y/Z clipping slider removes geometry', { before: beforeClip, after: afterClip });

  // --- wireframe toggle is real --------------------------------------------
  const surfaceShot = await page.evaluate(() => window.__viewer3dHarness.analyze());
  await page.getByLabel('显示模式').selectOption('wireframe');
  await page.waitForTimeout(300);
  const wireframeShot = await page.evaluate(() => window.__viewer3dHarness.analyze());
  await page.getByLabel('显示模式').selectOption('surface');
  await page.waitForTimeout(300);
  assert.notEqual(wireframeShot.signature, surfaceShot.signature, 'wireframe/surface toggle had no visible effect');
  check('wireframe/surface toggle changes the image', {
    surfacePixels: surfaceShot.nonBackground,
    wireframePixels: wireframeShot.nonBackground,
  });

  // --- material visibility toggle is real ----------------------------------
  await page.getByLabel('显示 Si', { exact: true }).uncheck();
  await page.waitForTimeout(250);
  const hidden = await page.evaluate(() => window.__viewer3dHarness.analyze());
  await page.getByLabel('显示 Si', { exact: true }).check();
  const siBefore = pixels.materials.find((entry) => entry.color === '#f59e0b')?.pixels ?? 0;
  const siAfter = hidden.materials.find((entry) => entry.color === '#f59e0b')?.pixels ?? 0;
  assert.ok(siAfter < siBefore * 0.2, `hiding Si left ${siAfter} amber pixels (was ${siBefore})`);
  check('material visibility toggle hides only that material', { before: siBefore, after: siAfter });

  // --- 2D snapshots use the canvas renderer, not the 3D engine -------------
  const page2d = await context.newPage();
  page2d.on('console', (message) => {
    if (message.type() === 'error') report.consoleErrors.push(message.text());
  });
  page2d.on('pageerror', (error) => report.consoleErrors.push(String(error)));
  await page2d.goto(`${baseUrl}/viewer3d-harness.html?dimension=2`, { waitUntil: 'domcontentloaded' });
  await page2d.waitForFunction(() => window.__viewer3dHarness?.getState().ready === true, null, { timeout: 60000 });
  await page2d.waitForSelector('.wb-viewer3d__canvas2d');
  const pixels2d = await page2d.evaluate(() => window.__viewer3dHarness.analyze2d());
  assert.ok(pixels2d, '2D canvas readback failed');
  assert.ok(pixels2d.nonBackground > 500, `2D canvas looks empty (${pixels2d.nonBackground} painted pixels)`);
  assert.ok(pixels2d.materials.filter((entry) => entry.pixels > 100).length >= 2, '2D canvas lost its material colours');
  await page2d.close();
  check('2D snapshots render on a canvas with material colours', {
    painted: pixels2d.nonBackground,
    materials: pixels2d.materials.filter((entry) => entry.pixels > 100).length,
  });

  // --- production path: worker fetches the snapshot, prefetch warms the next
  const apiSnapshot = (phase) => {
    const grid = 122;                       // ~30k triangles, JSON stays small
    const side = grid + 1;
    const points = [];
    const triangles = [];
    const materialIds = [];
    for (let j = 0; j < side; j++) {
      for (let i = 0; i < side; i++) {
        const x = -4 + (8 * i) / grid;
        const y = -4 + (8 * j) / grid;
        points.push(x, y, 0.4 * Math.sin(x * 1.3 + phase) * Math.cos(y * 1.1));
      }
    }
    for (let j = 0; j < grid; j++) {
      for (let i = 0; i < grid; i++) {
        const a = j * side + i;
        const b = a + 1;
        const c = a + side;
        const d = c + 1;
        triangles.push(a, c, b, b, c, d);
        materialIds.push((a % 3) * 10, (a % 3) * 10);
      }
    }
    return {
      dimension: 3,
      points,
      triangles,
      cellData: { materialIds },
      materials: { '0': { name: 'Mask', color: '#3b82f6' }, '10': { name: 'Si', color: '#f59e0b' }, '20': { name: 'SiO2', color: '#22c55e' } },
      coordinateUnit: 'um',
      stepName: `API 步骤 ${phase}`,
    };
  };
  const apiPage = await context.newPage();
  apiPage.on('console', (message) => {
    if (message.type() === 'error') report.consoleErrors.push(message.text());
  });
  apiPage.on('pageerror', (error) => report.consoleErrors.push(String(error)));
  const manifest = { snapshots: [{ stepId: 'api-1', name: 'API 1', stepIndex: 0 }, { stepId: 'api-2', name: 'API 2', stepIndex: 1 }] };
  await apiPage.route('**/api/runs/**', async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith('/snapshots/api-1')) return route.fulfill({ json: apiSnapshot(0.4) });
    if (url.pathname.endsWith('/snapshots/api-2')) return route.fulfill({ json: apiSnapshot(1.9) });
    if (url.pathname.endsWith('/api/runs/benchmark-run')) return route.fulfill({ json: manifest });
    return route.fulfill({ status: 404, json: { detail: 'not found' } });
  });
  await apiPage.goto(`${baseUrl}/viewer3d-harness.html?source=api`, { waitUntil: 'domcontentloaded' });
  await apiPage.waitForFunction(() => window.__viewer3dHarness?.getState().ready === true, null, { timeout: 60000 });
  const apiState = await apiPage.evaluate(() => window.__viewer3dHarness.getState());
  assert.ok(apiState.stats.triangles > 20_000, `API snapshot did not render (${apiState.stats.triangles} triangles)`);
  // Idle prefetch should have warmed api-2, so switching must be a cache hit.
  await apiPage.waitForTimeout(1500);
  const apiSwitch = await apiPage.evaluate(() => window.__viewer3dHarness.switchStep('api-2'));
  assert.equal(apiSwitch.stats.parseMs, 0, 'neighbouring step was not prefetched');
  assert.ok(apiSwitch.ms < CACHED_SWITCH_BUDGET_MS, `prefetched switch took ${apiSwitch.ms} ms`);
  report.apiMode = { triangles: apiState.stats.triangles, prefetchedSwitchMs: Number(apiSwitch.ms.toFixed(1)) };
  await apiPage.close();
  check('worker fetch path + idle prefetch of the next step', report.apiMode);

  // --- renderer line is opt-in (性能) and the software hint is honest ------
  assert.equal(await page.locator('.wb-viewer3d__renderer').count(), 0, 'the renderer string is hidden by default');
  await page.locator('.wb-viewer3d__toggle--end input').check();
  const rendererLine = page.locator('.wb-viewer3d__renderer');
  await rendererLine.waitFor();
  const uiRenderer = await rendererLine.innerText();
  assert.match(uiRenderer, /WebGL：/);
  assert.ok(uiRenderer.includes(String(report.webglRenderer ?? '').slice(0, 12)),
    `panel renderer ${uiRenderer} does not match ${report.webglRenderer}`);
  await page.locator('.wb-viewer3d__toggle--end input').uncheck();
  assert.equal(await page.locator('.wb-viewer3d__renderer').count(), 0, 'closing 性能 hides the renderer string');
  if (report.environment === 'software') {
    const warning = page.locator('[aria-label="软件渲染提示"]');
    assert.equal(await warning.count(), 1, 'software rendering must be announced in the panel');
    assert.match(await warning.innerText(), /软件渲染/);
  } else {
    assert.equal(await page.locator('[aria-label="软件渲染提示"]').count(), 0, 'a hardware GPU must not warn');
  }
  check('renderer string is opt-in and software rendering is announced', { environment: report.environment });

  const debugPage = await context.newPage();
  await debugPage.goto(`${baseUrl}/viewer3d-harness.html?debug=1`, { waitUntil: 'domcontentloaded' });
  await debugPage.waitForFunction(() => window.__viewer3dHarness?.getState().ready === true, null, { timeout: 60000 });
  await debugPage.locator('.wb-viewer3d__renderer').waitFor({ timeout: 30000 });
  assert.match(await debugPage.locator('.wb-viewer3d__renderer').innerText(), /WebGL：/);
  await debugPage.close();
  check('?debug=1 shows the renderer string without opening 性能');

  // --- raw WebGL2 floor on the same canvas ---------------------------------
  const baseline = await page.evaluate(
    ([width, height, triangles]) => window.__viewer3dHarness.webglBaseline(width, height, triangles),
    [pixels.width, pixels.height, report.triangles],
  );
  report.webglBaselineMs = Number(baseline.avgMs.toFixed(2));
  check('raw WebGL2 baseline (no vtk)', { ms: report.webglBaselineMs, triangles: baseline.triangles });

  // --- frame budget ---------------------------------------------------------
  if (report.avgFrameMs > FRAME_BUDGET_MS) {
    report.limitations.push(
      `平均帧时间 ${report.avgFrameMs} ms 超过 ${FRAME_BUDGET_MS} ms（渲染后端 ${report.webglRenderer ?? 'unknown'}）。` +
      `同机同分辨率的裸 WebGL2 基线（单 draw call、最简着色器、同样 ${report.triangles} 个三角形）为 ${report.webglBaselineMs} ms/帧，` +
      `查看器相对基线的开销约为 ${(report.avgFrameMs / Math.max(report.webglBaselineMs, 0.01)).toFixed(1)}x。` +
      (report.environment === 'software'
        ? '无头 Chromium 只能用 SwiftShader 软件光栅化，这个数字不代表真机 GPU；请用 VIEWER3D_CDP_URL 连到硬件浏览器复测。'
        : '这是硬件 GPU 上的结果，瓶颈需要结合基线判断。'),
    );
  } else {
    check('interaction frame time within budget', { avgMs: report.avgFrameMs, budgetMs: FRAME_BUDGET_MS });
  }

  assert.deepEqual(report.consoleErrors, [], `console errors: ${report.consoleErrors.join(' | ')}`);
  check('no console errors');

  report.status = 'passed';
} catch (error) {
  report.status = 'failed';
  report.error = error instanceof Error ? error.message : String(error);
  console.error(`FAIL ${report.error}`);
  process.exitCode = 1;
} finally {
  if (launchedHere) {
    await browser?.close();
  } else if (browser) {
    // CDP: closing the connection must not close the user's Edge window.
    for (const context of browser.contexts()) {
      for (const page of context.pages()) {
        if (page.url().startsWith(baseUrl)) await page.close().catch(() => undefined);
      }
    }
    await browser.close().catch(() => undefined);
  }
  if (report.environment === 'hardware' && report.webglRenderer) {
    // A CDP run against a real GPU refreshes the hardware section itself.
    report.hardware = {
      measuredAt: report.generatedAt,
      source: `CDP ${cdpUrl}`,
      browser: report.browser,
      environment: 'hardware',
      webglRenderer: report.webglRenderer,
      webglVendor: report.webglVendor,
      triangles: report.triangles,
      avgFrameMs: report.avgFrameMs,
      p95FrameMs: report.p95FrameMs,
      staticFullDetailMs: report.staticFullDetailMs,
      switchMs: report.switchMs,
      simplification: report.simplification,
    };
  }
  await fs.mkdir(path.dirname(jsonPath), { recursive: true });
  await fs.writeFile(jsonPath, `${JSON.stringify(report, null, 2)}\n`);
  console.log(JSON.stringify(report, null, 2));
}
