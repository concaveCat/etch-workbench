/**
 * 3D GDS / Layout acceptance: a 3D project can use a layout Clip as geometry.
 *
 * Flow:
 *  - import a 3D project, switch 01 to GDS / Layout;
 *  - upload/register the two-structure GDS, fill per-layer baseHeight/height,
 *    save the Clip;
 *  - use it as the 3D project geometry, compile through /clips/{id}/compile;
 *  - create a Route, execute it, and check the 3D viewer renders a non-empty
 *    structure (triangles > 0, painted canvas);
 *  - 2D regression: the same GDS still saves a height-less 2D Clip and compiles.
 *
 * Env: WORKBENCH_URL (default http://127.0.0.1:8820), WORKFLOW_LAYOUT,
 * WORKFLOW_SCREENSHOT_DIR (default /tmp).
 */
import assert from 'node:assert/strict';
import path from 'node:path';
import zlib from 'node:zlib';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const here = path.dirname(fileURLToPath(import.meta.url));
const baseUrl = (process.env.WORKBENCH_URL ?? 'http://127.0.0.1:8820').replace(/\/$/, '');
const layoutPath = process.env.WORKFLOW_LAYOUT
  ?? path.resolve(here, '../../docs/stage5-evidence/two-structure-two-layer.gds');
const project3dPath = process.env.WORKFLOW_PROJECT_3D ?? path.join(here, 'fixtures', 'workflow-3d-hole.json');
const project2dPath = process.env.WORKFLOW_PROJECT_2D ?? path.join(here, 'fixtures', 'workflow-2d-five.json');
const screenshotDir = process.env.WORKFLOW_SCREENSHOT_DIR ?? '/tmp';
const shot = name => path.join(screenshotDir, name);

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
      colorType = data[9];
    } else if (type === 'IDAT') {
      idat.push(data);
    } else if (type === 'IEND') {
      break;
    }
    offset += 12 + length;
  }
  const channels = colorType === 6 ? 4 : colorType === 2 ? 3 : 0;
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

/** Count pixels that differ from the most common (background) colour. */
function paintedPixels(buffer) {
  const { width, height, channels, pixels } = decodePng(buffer);
  const counts = new Map();
  for (let i = 0; i < width * height; i += 1) {
    const key = `${pixels[i * channels]},${pixels[i * channels + 1]},${pixels[i * channels + 2]}`;
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  let backgroundKey = '';
  let background = 0;
  for (const [key, count] of counts) {
    if (count > background) { background = count; backgroundKey = key; }
  }
  const [br, bg, bb] = backgroundKey.split(',').map(Number);
  let painted = 0;
  for (let i = 0; i < width * height; i += 1) {
    if (Math.abs(pixels[i * channels] - br)
      + Math.abs(pixels[i * channels + 1] - bg)
      + Math.abs(pixels[i * channels + 2] - bb) > 18) painted += 1;
  }
  return { painted, uniqueColors: counts.size };
}

const browser = await chromium.launch({
  headless: true,
  args: ['--no-sandbox', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'],
});
const page = await browser.newPage({ viewport: { width: 1600, height: 1400 } });
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

  // ---- 3D project -------------------------------------------------------
  await page.locator('input[type=file]').nth(1).setInputFiles(project3dPath);
  await page.getByText('3D hole etch fixture').first().waitFor();

  // The GDS source tab is selectable for a 3D project.
  const gdsTab = page.getByRole('button', { name: 'GDS / Layout' });
  assert.equal(await gdsTab.isDisabled(), false, '3D projects must offer the GDS / Layout tab');
  await gdsTab.click();

  await page.locator('input[type=file]').nth(0).setInputFiles(layoutPath);
  await page.locator('.spx-file-name').filter({ hasText: 'two-structure-two-layer.gds' }).first().waitFor();
  await page.getByLabel('版图结构').waitFor();

  // Per-layer z placement is visible in 3D only.
  const baseHeightInput = page.getByLabel('图层 1 基底高度');
  const heightInput = page.getByLabel('图层 1 厚度');
  await heightInput.waitFor();
  assert.equal(await baseHeightInput.inputValue(), '0', 'baseHeight defaults to 0');
  assert.equal(await heightInput.inputValue(), '0.2', 'height defaults to 0.2');
  await baseHeightInput.fill('0');
  await heightInput.fill('0.6');
  await page.getByLabel('Clip 名称').fill('3d gds clip');

  // An illegal thickness is rejected in place, before the request.
  await heightInput.fill('0.1');
  await page.getByRole('button', { name: '保存 Clip' }).click();
  await page.getByText(/小于两个网格步长/).first().waitFor();
  await heightInput.fill('0.6');

  await page.getByRole('button', { name: '预览' }).click();
  await page.locator('.wf-clip-canvas polygon').first().waitFor();
  await page.getByRole('button', { name: '保存 Clip' }).click();
  await page.locator('.spx-clip-summary-list button').filter({ hasText: '3d gds clip' }).first().waitFor();
  await page.screenshot({ path: shot('workflow-3d-gds-layers.png'), fullPage: true });

  const clips = (await (await fetch(`${baseUrl}/api/v1/workflow/clips`)).json()).clips;
  const clip3d = clips.find(item => item.name === '3d gds clip');
  assert.ok(clip3d, 'the 3D clip must be saved');
  assert.equal(clip3d.layers[0].baseHeight, 0);
  assert.equal(clip3d.layers[0].height, 0.6);

  // ---- use it as the 3D project geometry and compile --------------------
  const [compileResponse] = await Promise.all([
    page.waitForResponse(item => item.url().includes('/compile') && item.request().method() === 'POST'),
    page.getByRole('button', { name: '用此 Clip 编译' }).click(),
  ]);
  assert.equal(compileResponse.status(), 201, await compileResponse.text());
  const compiled = await compileResponse.json();
  assert.equal(compiled.dimension, 3, 'the clip compile must stay 3D');
  assert.equal(compiled.binding.dimension, 3);
  assert.equal(compiled.binding.geometrySource.clipId, clip3d.clipId);
  assert.equal(compiled.compile.project.geometry.kind, 'gds');
  assert.deepEqual(
    compiled.compile.project.geometry.layers.map(row => [row.layer, row.baseHeight, row.height]),
    [[1, 0, 0.6]],
  );
  assert.ok(compiled.compile.project.domain.yExtent > 0, '3D GDS compile needs a y extent');
  await page.getByText(/流程已编译/).first().waitFor();
  await page.screenshot({ path: shot('workflow-3d-gds-compile.png'), fullPage: true });

  // ---- Route + run + non-empty 3D structure -----------------------------
  await page.getByRole('button', { name: '02 流程' }).click();
  await page.getByRole('button', { name: /^(创建 Route|去 03)$/ }).click();
  await page.getByRole('button', { name: '执行全部 Segment' }).click();
  await page.locator('.wf-status.succeeded').first().waitFor({ timeout: 120000 });

  await page.locator('.wf-structure-timeline').waitFor();
  await page.locator('.wf-step-chip[data-step-id="initial"]').first().waitFor();
  await page.locator('.wb-viewer3d canvas').first().waitFor({ timeout: 120000 });
  await page.waitForFunction(() => {
    const element = document.querySelector('.wf-structure-3d-stats');
    return !!element && /三角形/.test(element.textContent ?? '');
  });
  const statsText = await page.locator('.wf-structure-3d-stats').innerText();
  const triangles = Number((statsText.match(/三角形\s*([\d,]+)/)?.[1] ?? '0').replace(/,/g, ''));
  assert.ok(triangles > 0, `3D GDS structure must be non-empty, got ${triangles} triangles`);
  const canvas = paintedPixels(await page.locator('.wb-viewer3d__stage').screenshot());
  assert.ok(canvas.painted > 500, `3D GDS canvas looks blank (${canvas.painted} painted pixels)`);
  await page.locator('.wb-viewer3d__stage').screenshot({ path: shot('workflow-3d-gds-structure.png') });
  await page.screenshot({ path: shot('workflow-3d-gds-route.png'), fullPage: true });

  // ---- saved project reopens on the 3D GDS tab with its layer heights ---
  await page.getByRole('button', { name: '01 工程' }).click();
  await page.getByRole('button', { name: '保存工程', exact: true }).click();
  await page.locator('.spx-project-status').filter({ hasText: '已写入服务端' }).waitFor();
  await page.reload();
  await page.locator('.spx-project-status').filter({ hasText: '已写入服务端' }).waitFor();
  await page.getByRole('button', { name: '01 工程' }).click();
  await page.getByRole('button', { name: 'GDS / Layout' }).waitFor();
  assert.match(await page.getByRole('button', { name: 'GDS / Layout' }).getAttribute('class') ?? '', /active/,
    'a reopened 3D GDS project must restore the layout source tab');
  assert.equal(await page.getByLabel('图层 1 基底高度').inputValue(), '0');
  assert.equal(await page.getByLabel('图层 1 厚度').inputValue(), '0.6');
  await page.getByLabel('图层 1 厚度').scrollIntoViewIfNeeded();
  await page.screenshot({ path: shot('workflow-3d-gds-reopen.png'), fullPage: true });

  // ---- 2D GDS regression ------------------------------------------------
  await page.getByRole('button', { name: '01 工程' }).click();
  await page.locator('input[type=file]').nth(1).setInputFiles(project2dPath);
  await page.getByText('P1 2D five-step fixture').first().waitFor();
  await page.getByRole('button', { name: 'GDS / Layout' }).click();
  await page.getByLabel('版图选择').waitFor();
  const layoutValue = await page.getByLabel('版图选择').inputValue();
  if (!layoutValue) {
    const option = await page.getByLabel('版图选择').locator('option').nth(1).getAttribute('value');
    await page.getByLabel('版图选择').selectOption(option);
  }
  await page.getByLabel('版图结构').waitFor();
  assert.equal(await page.getByLabel('图层 1 厚度').count(), 0, '2D clips must not ask for layer heights');
  await page.getByLabel('Clip 名称').fill('2d regression clip');
  await page.getByRole('button', { name: '保存 Clip' }).click();
  await page.locator('.spx-clip-summary-list button').filter({ hasText: '2d regression clip' }).first().waitFor();
  const [twoDResponse] = await Promise.all([
    page.waitForResponse(item => item.url().includes('/compile') && item.request().method() === 'POST'),
    page.getByRole('button', { name: '用此 Clip 编译' }).click(),
  ]);
  assert.equal(twoDResponse.status(), 201, await twoDResponse.text());
  const twoD = await twoDResponse.json();
  assert.equal(twoD.dimension, 2);
  assert.ok(twoD.compile.project.geometry.layers.every(row => row.baseHeight == null && row.height == null),
    '2D GDS layers must stay height-free');
  await page.getByText(/流程已编译/).first().waitFor();
  await page.screenshot({ path: shot('workflow-2d-gds-regression.png'), fullPage: true });

  assert.deepEqual(pageErrors, [], `page errors: ${pageErrors.join(' | ')}`);
  assert.deepEqual(consoleErrors, [], `console errors: ${consoleErrors.join(' | ')}`);
  console.log(JSON.stringify({
    ok: true,
    clip3d: { clipId: clip3d.clipId, clipHash: clip3d.clipHash, layers: clip3d.layers },
    compiled: {
      dimension: compiled.dimension,
      compileId: compiled.compileId,
      domain: compiled.compile.project.domain,
      triangles,
      painted: canvas.painted,
    },
    twoD: { dimension: twoD.dimension, layers: twoD.compile.project.geometry.layers },
  }, null, 2));
} finally {
  await browser.close();
}
