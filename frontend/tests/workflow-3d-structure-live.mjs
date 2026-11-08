/**
 * 3D structure timeline acceptance (03 run).
 *
 * Covers the integration of the fast viewer3d into 03:
 *  - importing a 3D project, compiling, routing and executing it produces a
 *    visible "initial structure → step" timeline;
 *  - the 3D viewer renders the raw run snapshot (vtk.js canvas) with no console
 *    errors, and the canvas is not a flat single colour;
 *  - switching to the initial structure keeps the same viewer mounted;
 *  - the 02 step table's "结构" button jumps to 03 and selects that step;
 *  - the 03 segment row's "查看结构" button selects the segment's last step;
 *  - standard views / the clip section / "适配视图" fit the visible geometry
 *    instead of shrinking it to a dot (N6/N16);
 *  - the clip slider names its axis and unit in µm;
 *  - a measurement draws a red line inside the 3D scene (N23).
 */
import assert from 'node:assert/strict';
import path from 'node:path';
import zlib from 'node:zlib';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const here = path.dirname(fileURLToPath(import.meta.url));
const baseUrl = (process.env.WORKBENCH_URL ?? 'http://127.0.0.1:8861').replace(/\/$/, '');
// A hole has a real Z extent: a flat plane would be edge-on in XZ and could
// never show whether the standard views fit the geometry.
const projectPath = process.env.WORKFLOW_PROJECT ?? path.join(here, 'fixtures', 'workflow-3d-hole.json');
const screenshot = process.env.WORKFLOW_SCREENSHOT ?? '/tmp/workflow-3d-structure.png';

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
    const filter = raw[position];
    position += 1;
    const line = raw.subarray(position, position + stride);
    position += stride;
    const previous = y > 0 ? pixels.subarray((y - 1) * stride, y * stride) : null;
    const current = pixels.subarray(y * stride, (y + 1) * stride);
    for (let x = 0; x < stride; x += 1) {
      const left = x >= channels ? current[x - channels] : 0;
      const up = previous ? previous[x] : 0;
      const upLeft = previous && x >= channels ? previous[x - channels] : 0;
      let value = line[x];
      if (filter === 1) value += left;
      else if (filter === 2) value += up;
      else if (filter === 3) value += Math.floor((left + up) / 2);
      else if (filter === 4) {
        const estimate = left + up - upLeft;
        const pa = Math.abs(estimate - left);
        const pb = Math.abs(estimate - up);
        const pc = Math.abs(estimate - upLeft);
        value += pa <= pb && pa <= pc ? left : pb <= pc ? up : upLeft;
      }
      current[x] = value & 0xff;
    }
  }
  return { width, height, channels, pixels };
}

function analyzePng(buffer) {
  const { width, height, channels, pixels } = decodePng(buffer);
  const counts = new Map();
  let background = 0;
  let backgroundKey = '';
  for (let i = 0; i < width * height; i += 1) {
    const r = pixels[i * channels];
    const g = pixels[i * channels + 1];
    const b = pixels[i * channels + 2];
    const key = `${r},${g},${b}`;
    const count = (counts.get(key) ?? 0) + 1;
    counts.set(key, count);
    if (count > background) {
      background = count;
      backgroundKey = key;
    }
  }
  const [br, bg, bb] = backgroundKey.split(',').map(Number);
  let painted = 0;
  const quantized = new Set();
  // Group painted pixels by hue so a single material's brightness spread can
  // be measured. Real lighting makes one material brighter where its normal
  // faces the light and darker elsewhere; a flat fill has zero spread.
  const hueBuckets = new Map();
  for (let i = 0; i < width * height; i += 1) {
    const r = pixels[i * channels];
    const g = pixels[i * channels + 1];
    const b = pixels[i * channels + 2];
    if (Math.abs(r - br) + Math.abs(g - bg) + Math.abs(b - bb) > 18) painted += 1;
    quantized.add(`${r >> 4},${g >> 4},${b >> 4}`);
    if (Math.abs(r - br) + Math.abs(g - bg) + Math.abs(b - bb) <= 18) continue;
    const max = Math.max(r, g, b);
    const min = Math.min(r, g, b);
    const saturation = max === 0 ? 0 : (max - min) / max;
    if (saturation < 0.18 || max < 30) continue;
    let hue;
    const delta = max - min;
    if (max === r) hue = ((g - b) / delta) % 6;
    else if (max === g) hue = (b - r) / delta + 2;
    else hue = (r - g) / delta + 4;
    hue = (hue * 60 + 360) % 360;
    const bucket = Math.round(hue / 12);
    const luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b;
    if (!hueBuckets.has(bucket)) hueBuckets.set(bucket, []);
    hueBuckets.get(bucket).push(luminance);
  }
  let lighting = null;
  for (const [bucket, values] of hueBuckets) {
    if (!lighting || values.length > lighting.count) {
      const mean = values.reduce((total, value) => total + value, 0) / values.length;
      const variance = values.reduce((total, value) => total + (value - mean) ** 2, 0) / values.length;
      lighting = {
        hueBucket: bucket,
        count: values.length,
        mean: Number(mean.toFixed(2)),
        std: Number(Math.sqrt(variance).toFixed(2)),
        spread: Number((Math.max(...values) - Math.min(...values)).toFixed(2)),
      };
    }
  }
  return { width, height, painted, uniqueColors: quantized.size, backgroundKey, lighting };
}

/**
 * Share of the canvas covered by the rendered structure (largest side of the
 * painted bounding box). The bottom-left corner holds the orientation marker
 * and the scale bar, so it is excluded.
 */
function paintedExtent(buffer) {
  const { width, height, channels, pixels } = decodePng(buffer);
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      if (x < 190 && y > height - 130) continue;
      const i = (y * width + x) * channels;
      const r = pixels[i], g = pixels[i + 1], b = pixels[i + 2];
      if (Math.abs(r - 247) + Math.abs(g - 249) + Math.abs(b - 251) <= 24) continue;
      if (x < minX) minX = x;
      if (x > maxX) maxX = x;
      if (y < minY) minY = y;
      if (y > maxY) maxY = y;
    }
  }
  if (!Number.isFinite(minX)) return { width: 0, height: 0, ratio: 0 };
  return {
    width: (maxX - minX) / width,
    height: (maxY - minY) / height,
    ratio: Math.max((maxX - minX) / width, (maxY - minY) / height),
  };
}

/** Reddish pixels: the measurement line is drawn in #b42318. */
function countRedPixels(buffer) {
  const { width, height, channels, pixels } = decodePng(buffer);
  let red = 0;
  for (let i = 0; i < width * height; i += 1) {
    const r = pixels[i * channels], g = pixels[i * channels + 1], b = pixels[i * channels + 2];
    if (r > 110 && r - g > 55 && r - b > 55) red += 1;
  }
  return red;
}

const browser = await chromium.launch({
  headless: true,
  args: ['--no-sandbox', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'],
});
const page = await browser.newPage({ viewport: { width: 1600, height: 1300 } });
page.setDefaultTimeout(90000);
const pageErrors = [];
const consoleErrors = [];
page.on('pageerror', error => pageErrors.push(error.message));
page.on('console', message => {
  if (message.type() === 'error') consoleErrors.push(message.text());
});

try {
  await page.goto(`${baseUrl}/`);
  await page.getByText('Etch Workbench').first().waitFor();
  await page.locator('input[type=file]').nth(1).setInputFiles(projectPath);
  await page.getByRole('button', { name: '去 02 定义流程' }).click();
  await page.getByRole('button', { name: '编译流程' }).click();
  await page.getByText(/编译成功/).waitFor();
  await page.getByRole('button', { name: /^(创建 Route|去 03)$/ }).click();
  await page.getByRole('button', { name: '执行全部 Segment' }).click();
  await page.locator('.wf-status.succeeded').first().waitFor({ timeout: 90000 });

  // ---- 03 timeline + 3D canvas -------------------------------------------
  await page.locator('.wf-structure-timeline').waitFor();
  const chips = page.locator('.wf-step-chip');
  assert.ok(await chips.count() >= 2, 'timeline must contain the initial structure and the executed step');
  await page.locator('.wf-step-chip[data-step-id="initial"]').first().waitFor();
  await page.locator('.wb-viewer3d canvas').first().waitFor({ timeout: 90000 });
  await page.waitForFunction(() => {
    const el = document.querySelector('.wf-structure-3d-stats');
    return !!el && /三角形/.test(el.textContent ?? '');
  });
  const statsText = await page.locator('.wf-structure-3d-stats').innerText();
  const triangles = Number((statsText.match(/三角形\s*([\d,]+)/)?.[1] ?? '0').replace(/,/g, ''));
  assert.ok(triangles > 0, `3D viewer reported ${triangles} triangles`);
  assert.equal(await page.locator('.wf-structure-3d-stats').getByText('简化').count(), 0, 'a small plane must not be simplified');

  const stepChipId = await page.locator('.wf-step-chip.active').first().getAttribute('data-step-id');
  assert.ok(stepChipId && stepChipId !== 'initial', `default selection should be the executed step, got ${stepChipId}`);
  assert.equal(await page.locator('.wf-structure-info code').count(), 0, 'the current step shows its name, not the raw s_... id');
  assert.match(await page.locator('.wf-structure-side').innerText(), /第 \d+ 段 · Run 已记录/);

  // The scale bar must follow wheel zoom; a static label would misreport the
  // world length of the 72 px bar.
  const scaleLabel = page.locator('.wb-viewer3d__scalebar-label');
  await scaleLabel.waitFor({ timeout: 30000 });
  const scaleBefore = (await scaleLabel.innerText()).trim();
  await page.getByLabel('标准视角').selectOption('xz');
  await page.waitForTimeout(1200);
  const scaleAfter = (await scaleLabel.innerText()).trim();
  assert.notEqual(scaleAfter, scaleBefore, `scale bar did not follow the camera (${scaleBefore} -> ${scaleAfter})`);

  await page.locator('.wb-viewer3d__stage').screenshot({ path: screenshot.replace(/\.png$/, '-canvas.png') });
  const canvasPng = await page.locator('.wb-viewer3d__stage').screenshot();
  const canvasPixels = analyzePng(canvasPng);
  assert.ok(canvasPixels.painted > 500, `3D canvas looks blank (${canvasPixels.painted} painted pixels)`);
  assert.ok(canvasPixels.uniqueColors > 8, `3D canvas is flat (${canvasPixels.uniqueColors} quantized colours)`);
  // Lighting: the dominant material must not be one flat fill. Its pixels span
  // different orientations, so brightness has to vary measurably.
  assert.ok(canvasPixels.lighting && canvasPixels.lighting.count > 500,
    `lighting sample missing: ${JSON.stringify(canvasPixels.lighting)}`);
  assert.ok(canvasPixels.lighting.spread > 15,
    `same-material pixels must differ in brightness (${JSON.stringify(canvasPixels.lighting)})`);
  assert.ok(canvasPixels.lighting.std > 3,
    `same-material brightness must vary with orientation (${JSON.stringify(canvasPixels.lighting)})`);

  // ---- N6/N16: standard views fit the visible geometry --------------------
  await page.getByLabel('标准视角').selectOption('xz');
  await page.waitForTimeout(1200);
  const xzExtent = paintedExtent(await page.locator('.wb-viewer3d__stage').screenshot());
  assert.ok(xzExtent.ratio >= 0.3, `XZ view must fill the canvas (${JSON.stringify(xzExtent)})`);
  await page.locator('.wb-viewer3d__stage').screenshot({ path: screenshot.replace(/\.png$/, '-xz.png') });

  await page.getByRole('button', { name: '适配视图' }).click();
  await page.waitForTimeout(900);
  const fittedExtent = paintedExtent(await page.locator('.wb-viewer3d__stage').screenshot());
  assert.ok(fittedExtent.ratio >= 0.3, `适配视图 must keep the geometry readable (${JSON.stringify(fittedExtent)})`);
  await page.locator('.wb-viewer3d__stage').screenshot({ path: screenshot.replace(/\.png$/, '-fit.png') });

  // ---- N16: the clip slider names its axis and unit, and stays readable ---
  await page.getByRole('checkbox', { name: '截面' }).check();
  await page.waitForTimeout(1200);
  const clipLabel = await page.locator('.wb-viewer3d__clip-value').innerText();
  assert.match(clipLabel, /^X = -?\d+\.\d{3} µm$/, `clip label must carry the axis and µm: ${clipLabel}`);
  const clipRange = await page.locator('.wb-viewer3d__clip-range').innerText();
  assert.match(clipRange, /-?\d+\.\d{3} … -?\d+\.\d{3} µm/, `clip range must show min … max µm: ${clipRange}`);
  const sectionExtent = paintedExtent(await page.locator('.wb-viewer3d__stage').screenshot());
  assert.ok(sectionExtent.ratio >= 0.3, `section must stay readable (${JSON.stringify(sectionExtent)})`);
  await page.locator('.wb-viewer3d__stage').screenshot({ path: screenshot.replace(/\.png$/, '-clip.png') });

  await page.getByLabel('截面位置（µm）').evaluate(node => {
    const target = Number(node.min) + (Number(node.max) - Number(node.min)) * 0.25;
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
    setter.call(node, String(target));
    node.dispatchEvent(new Event('input', { bubbles: true }));
  });
  await page.waitForTimeout(500);
  assert.notEqual(await page.locator('.wb-viewer3d__clip-value').innerText(), clipLabel,
    'dragging the slider must update the label in real time');
  await page.getByRole('checkbox', { name: '截面' }).uncheck();
  await page.waitForTimeout(800);

  // ---- N23: the measurement draws a red line inside the 3D scene ---------
  await page.getByRole('button', { name: '测量', exact: true }).click();
  await page.getByText(/测量 · 已测量/).waitFor({ timeout: 90000 });
  await page.waitForTimeout(1500);
  const markerPixels = countRedPixels(await page.locator('.wb-viewer3d__stage').screenshot());
  assert.ok(markerPixels > 50, `3D marker line must be visible (${markerPixels} red pixels)`);
  assert.match(await page.locator('.wb-viewer3d__status').innerText(), /测量位置：X = -?[\d.]+ µm/);
  const structureSide = await page.locator('.wf-structure-side').innerText();
  assert.match(structureSide, /测量位置：X = -?[\d.]+ µm/, `3D wording must name the position: ${structureSide}`);
  assert.doesNotMatch(structureSide, /2D 视图/, 'the 3D panel must not point at a 2D view');
  await page.locator('.wb-viewer3d__stage').screenshot({ path: screenshot.replace(/\.png$/, '-marker.png') });

  // ---- initial structure keeps the viewer mounted -------------------------
  const initialChip = page.locator('.wf-step-chip[data-step-id="initial"]').first();
  await initialChip.click();
  await page.waitForFunction(() => document.querySelector('.wf-step-chip[data-step-id="initial"]')?.getAttribute('aria-selected') === 'true');
  await page.locator('.wb-viewer3d canvas').first().waitFor();
  await page.waitForFunction(() => !document.querySelector('.wb-viewer3d__overlay'));
  assert.match(await page.locator('.wf-structure-3d-stats').innerText(), /三角形/);
  assert.equal(await page.locator('.wf-structure-3d').count(), 1, 'the viewer must not be remounted when switching steps');

  // ---- 02 step row "结构" jumps back to the same step ---------------------
  await page.getByRole('button', { name: '02 流程', exact: true }).click();
  const stepRow = page.locator('table[aria-label="流程步骤"] tbody tr').first();
  await stepRow.click();
  const structureButton = page.locator('.spx-step-inspector').getByRole('button', { name: '查看结构' });
  await structureButton.waitFor();
  assert.equal(await structureButton.isDisabled(), false, 'structure button must be enabled for an executed step');
  await page.screenshot({ path: screenshot.replace(/\.png$/, '-02.png'), fullPage: true });
  await structureButton.click();
  await page.locator('.wf-structure-timeline').waitFor();
  await page.waitForFunction(id => document.querySelector('.wf-step-chip.active')?.getAttribute('data-step-id') === id, stepChipId);

  // ---- 03 segment row "查看结构" selects the segment's through step -------
  await page.getByRole('button', { name: '02 流程', exact: true }).click();
  await page.getByRole('button', { name: '03 运行', exact: true }).click();
  await initialChip.click();
  const segmentButton = page.locator('.wf-segment-structure').first();
  await segmentButton.waitFor();
  assert.equal(await segmentButton.isDisabled(), false, 'segment structure button must be enabled after execution');
  await segmentButton.click();
  await page.waitForFunction(id => document.querySelector('.wf-step-chip.active')?.getAttribute('data-step-id') === id, stepChipId);
  assert.match(await page.locator('.wf-summary.success').filter({ hasText: 'Route 已创建' }).innerText(), /结构：/);

  // ---- software renderer hint (03 hides the viewer's 性能 toggle) --------
  // Headless CI is SwiftShader: the panel must say so, stay usable, and the
  // warning must be dismissible. On a hardware browser the warning is absent.
  const rendererWarning = page.locator('[aria-label="软件渲染提示"]');
  assert.equal(await page.locator('.wb-viewer3d__renderer').count(), 0, 'the renderer string is hidden by default');
  if (await rendererWarning.count() > 0) {
    assert.match(await rendererWarning.innerText(), /软件渲染/);
    await rendererWarning.getByRole('button', { name: '关闭软件渲染提示' }).click();
    await rendererWarning.waitFor({ state: 'detached' });
    // 3D keeps working after dismissing the hint.
    assert.equal(await page.locator('.wb-viewer3d canvas').count(), 1);
  }

  await page.screenshot({ path: screenshot, fullPage: true });
  const benign = /favicon|Download the React DevTools/i;
  const consoleProblems = consoleErrors.filter(message => !benign.test(message));
  assert.deepEqual(pageErrors, []);
  assert.deepEqual(consoleProblems, []);
  console.log(JSON.stringify({
    status: 'passed',
    baseUrl,
    screenshot,
    chips: await chips.count(),
    stepChipId,
    triangles,
    statsText,
    canvasPixels,
    consoleErrors: consoleProblems,
  }));
} finally {
  await browser.close();
}
