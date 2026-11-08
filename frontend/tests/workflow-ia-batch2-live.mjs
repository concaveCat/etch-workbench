/** IA batch 2 Chromium acceptance.
 *
 * Covers the review items implemented in this batch: 03 structure view after a
 * run, readable primary buttons, route reuse instead of silent duplicate
 * creation, labelled measurement fields, text status badges in the left tree,
 * the GDS/Layout entry from the tree, project persistence (save/reload/export/
 * overwrite/delete) and the seeded library presets.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const here = path.dirname(fileURLToPath(import.meta.url));
const baseUrl = (process.env.WORKBENCH_URL ?? 'http://127.0.0.1:8818').replace(/\/$/, '');
const projectPath = process.env.WORKFLOW_PROJECT ?? path.join(here, 'fixtures', 'workflow-2d-five.json');
const layoutPath = process.env.WORKFLOW_LAYOUT ?? path.resolve(here, '../../docs/stage5-evidence/two-structure-two-layer.gds');
const screenshot = process.env.WORKFLOW_SCREENSHOT ?? '/tmp/workflow-ia-batch2.png';

const getJson = async endpoint => {
  const response = await fetch(`${baseUrl}${endpoint}`);
  assert.ok(response.ok, `${endpoint} -> ${response.status}`);
  return response.json();
};

const contrast = (foreground, background) => {
  const parse = value => {
    const match = /rgba?\(([\d.]+),\s*([\d.]+),\s*([\d.]+)/.exec(value);
    assert.ok(match, `unparsable color ${value}`);
    return match.slice(1, 4).map(Number);
  };
  const luminance = rgb => {
    const [r, g, b] = rgb.map(channel => {
      const value = channel / 255;
      return value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const first = luminance(parse(foreground));
  const second = luminance(parse(background));
  const [light, dark] = first > second ? [first, second] : [second, first];
  return (light + 0.05) / (dark + 0.05);
};

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const page = await browser.newPage({ viewport: { width: 1600, height: 1100 } });
page.setDefaultTimeout(60000);
const errors = [];
page.on('pageerror', error => errors.push(error.message));

try {
  await page.goto(`${baseUrl}/`);
  await page.getByText('Etch Workbench').first().waitFor();

  // ---- 01: recent projects empty state, import a draft, save it for real.
  await page.locator('[aria-label="最近工程列表"]').waitFor();
  assert.match(await page.locator('[aria-label="最近工程列表"]').innerText(), /还没有已保存工程/);
  await page.locator('input[type=file]').nth(1).setInputFiles(projectPath);
  await page.locator('.spx-save-button').filter({ hasText: '草稿（未保存）' }).waitFor();
  await page.getByRole('button', { name: '保存工程', exact: true }).click();
  await page.locator('.spx-project-status').filter({ hasText: '已写入服务端' }).waitFor();
  const saved = await getJson('/api/v1/workflow/projects');
  assert.equal(saved.projects.length, 1, JSON.stringify(saved));
  assert.equal(saved.projects[0].stepCount, 5);
  const savedName = saved.projects[0].name;

  // ---- Reload restores the saved project (steps survive, no compile/route).
  await page.reload();
  await page.locator('.spx-project-status').filter({ hasText: '已写入服务端' }).waitFor();
  assert.equal(await page.locator('.spx-summary-grid').filter({ hasText: '步骤' }).innerText().then(text => text.includes('5')), true);
  assert.equal((await getJson('/api/v1/workflow/compiles')).compiles.length, 0, 'opening a project must not compile');

  // ---- Export JSON downloads the current ProjectV7.
  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('button', { name: '导出工程 JSON' }).click();
  const download = await downloadPromise;
  assert.equal(download.suggestedFilename(), `${savedName}.json`);
  const exported = JSON.parse(readFileSync(await download.path(), 'utf8'));
  assert.equal(exported.steps.length, 5);
  assert.equal(exported.name, savedName);

  // ---- Same-name conflict needs an explicit overwrite confirmation.
  // Entry cards are hidden while a project is open: close it first.
  await page.getByRole('button', { name: '关闭工程', exact: true }).click();
  await page.locator('.spx-command').filter({ hasText: '新建工程' }).click();
  await page.getByLabel('新工程名称').fill(savedName);
  await page.getByRole('button', { name: '3D', exact: true }).click();
  // 3D projects now accept GDS / Layout geometry (per-layer baseHeight/height).
  assert.equal(await page.getByRole('button', { name: /GDS \/ Layout/ }).isDisabled(), false);
  assert.equal(await page.locator('.spx-lock-note').filter({ hasText: '3D 工程不支持 GDS 版图几何' }).count(), 0);
  await page.getByRole('button', { name: '2D', exact: true }).click();
  await page.getByLabel('新工程基底材料').selectOption('SiO2');
  await page.getByRole('button', { name: '创建工程草稿' }).click();
  await page.getByRole('button', { name: '去 02 定义流程' }).click();
  await page.getByRole('button', { name: '＋ 添加步骤' }).click();
  await page.getByRole('button', { name: '01 工程' }).click();
  await page.getByRole('button', { name: '保存工程', exact: true }).click();
  await page.getByRole('button', { name: '确认覆盖' }).waitFor();
  assert.equal((await getJson('/api/v1/workflow/projects')).projects.length, 1, 'a 409 must not create a second row');
  await page.getByRole('button', { name: '确认覆盖' }).click();
  await page.locator('.spx-project-status').filter({ hasText: '已写入服务端' }).waitFor();
  const overwritten = await getJson('/api/v1/workflow/projects');
  assert.equal(overwritten.projects.length, 1);
  assert.equal(overwritten.projects[0].stepCount, 1, 'overwrite must replace the steps with the edited ones');
  const overwrittenDoc = await getJson(`/api/v1/workflow/projects/${overwritten.projects[0].projectId}`);
  assert.equal(overwrittenDoc.geometry.substrateMaterial, 'SiO2', 'substrate material select must be persisted');

  // ---- Close and delete: the row disappears from the recent list.
  await page.getByRole('button', { name: '关闭工程' }).click();
  await page.locator('[aria-label="最近工程列表"]').waitFor();
  await page.getByRole('button', { name: '删除', exact: true }).click();
  await page.getByText(/确认删除工程/).waitFor();
  await page.getByRole('button', { name: '确认删除' }).click();
  await page.getByText(/还没有已保存工程/).waitFor();
  assert.equal((await getJson('/api/v1/workflow/projects')).projects.length, 0);

  // ---- 02: compile once, create one route, then only navigate with 去 03.
  await page.locator('input[type=file]').nth(1).setInputFiles(projectPath);
  await page.getByRole('button', { name: '去 02 定义流程' }).click();
  await page.getByRole('button', { name: '编译流程' }).click();
  await page.getByText(/编译成功/).waitFor();
  await page.getByRole('button', { name: '创建 Route', exact: true }).click();
  await page.locator('.wf-status.planned').first().waitFor();
  assert.equal((await getJson('/api/v1/workflow/routes')).routes.length, 1);
  await page.getByRole('button', { name: '02 流程' }).click();
  await page.getByRole('button', { name: '去 03', exact: true }).click();
  await page.getByRole('button', { name: '02 流程' }).click();
  await page.getByRole('button', { name: '去 03', exact: true }).click();
  assert.equal((await getJson('/api/v1/workflow/routes')).routes.length, 1, '去 03 must never create a second route');
  await page.getByRole('button', { name: '02 流程' }).click();
  assert.equal(await page.getByRole('button', { name: '重建 Route…' }).count(), 1);
  assert.equal(await page.getByText('查看编译详情', { exact: true }).count(), 1);
  await page.getByRole('button', { name: '去 03', exact: true }).click();

  // ---- 03: the run result is a structure view, not just a run id.
  await page.getByRole('button', { name: '执行全部 Segment' }).click();
  await page.locator('.wf-structure-canvas').waitFor({ timeout: 120000 });
  await page.locator('.wf-structure-timeline').waitFor();
  assert.ok((await page.locator('.wf-step-chip').count()) >= 2, 'timeline must show the initial structure and every executed step');
  await page.locator('.wf-step-chip[data-step-id="initial"]').first().waitFor();
  assert.ok((await page.locator('.wf-structure-canvas > line').count()) > 0, '2D structure must draw line segments');
  const structureText = await page.locator('.wf-structure-panel').innerText();
  assert.match(structureText, /当前：/);
  assert.match(structureText, /µm/);
  assert.ok((await page.locator('.wf-axes text').count()) > 0, 'structure view must draw axes or a scale bar');
  assert.match(structureText, /材料|Si/);

  // ---- Measurement fields carry visible labels and units.
  await page.getByText('X 位置（µm）', { exact: true }).waitFor();
  await page.getByText('参考高度（µm）', { exact: true }).waitFor();
  assert.equal(await page.getByLabel('测量材料').evaluate(node => node.tagName), 'SELECT');
  await page.getByRole('button', { name: '测量', exact: true }).click();
  await page.getByText(/测量 · 已测量/).waitFor({ timeout: 60000 });
  await page.locator('.wf-marker').waitFor({ state: 'attached', timeout: 30000 });
  assert.ok((await page.locator('.wf-marker line').count()) >= 1);

  // ---- Primary button hover stays readable (white on dark blue).
  await page.getByRole('button', { name: '02 流程' }).click();
  const primary = page.getByRole('button', { name: '重新编译', exact: true });
  await primary.hover();
  const colors = await primary.evaluate(node => {
    const style = window.getComputedStyle(node);
    return { color: style.color, background: style.backgroundColor };
  });
  assert.equal(colors.color, 'rgb(255, 255, 255)', JSON.stringify(colors));
  assert.ok(contrast(colors.color, colors.background) >= 4.5, `hover contrast too low: ${JSON.stringify(colors)}`);
  // Disabled primary must stay readable too (compile button with an empty flow).
  await page.getByRole('button', { name: '03 运行' }).click();
  const disabledPrimary = page.getByRole('button', { name: '执行当前 Segment', exact: true });
  assert.equal(await disabledPrimary.isDisabled(), true);
  const disabledColors = await disabledPrimary.evaluate(node => {
    const style = window.getComputedStyle(node);
    return { color: style.color, background: style.backgroundColor };
  });
  assert.ok(contrast(disabledColors.color, disabledColors.background) >= 3, `disabled contrast too low: ${JSON.stringify(disabledColors)}`);

  // ---- Left tree: text badges, no fake checkbox, no UUIDs.
  assert.equal(await page.locator('.spx-left .spx-tree-dot').count(), 0, 'left tree must not render checkbox-like squares');
  assert.equal(await page.locator('.spx-left input[type=checkbox]').count(), 0);
  const treeText = await page.locator('.spx-left').innerText();
  assert.doesNotMatch(treeText, /[0-9a-f]{12,}/i, 'left tree must not show ids');
  const runRow = page.locator('.spx-left .spx-tree-item').filter({ hasText: 'Run' }).first();
  const runText = await runRow.innerText();
  assert.match(runText, /(成功|待执行|运行中|失败)/);
  const runStatus = await runRow.locator('.spx-tree-status').innerText();
  assert.equal(runText.split(runStatus).length - 1, 1, 'the run row shows its status once, in the badge');
  const compileRow = page.locator('.spx-left .spx-tree-item').filter({ hasText: '编译' }).first();
  assert.equal(((await compileRow.innerText()).match(/已编译|未编译|需重新编译/g) ?? []).length, 1,
    'the compile row shows its status once, in the badge');
  assert.ok((await page.locator('.spx-left .spx-tree-status').count()) >= 3);
  assert.match(treeText, /状态图例|待执行\/未创建/);

  // ---- Left tree 版图 opens the GDS/Layout area, never the new-project form.
  await page.locator('.spx-tree-item').filter({ hasText: '版图' }).first().click();
  await page.locator('.spx-sheet-title').filter({ hasText: '版图 / Clip' }).waitFor();
  assert.equal(await page.getByRole('button', { name: '创建工程草稿' }).count(), 0, '版图 must not open the new-project form');

  // ---- GDS layer -> material mapping, coloured preview, bounded height.
  await page.locator('input[type=file]').nth(0).setInputFiles(layoutPath);
  await page.locator('.spx-file-name').filter({ hasText: 'two-structure-two-layer.gds' }).first().waitFor();
  await page.getByRole('button', { name: '注册为 Layout' }).click();
  await page.getByLabel('版图结构').waitFor();
  const layerMaterial = page.getByLabel(/^图层 \d+ 材料$/).first();
  await layerMaterial.waitFor();
  await layerMaterial.selectOption('SiO2');
  await page.getByRole('button', { name: '预览', exact: true }).click();
  await page.locator('.wf-clip-canvas polygon').first().waitFor();
  // ---- N7: clip canvas titles, ticks and scale bar stay apart -----------
  const clipLayout = await page.locator('.wf-clip-canvas').evaluate(svg => {
    const rect = selector => {
      const node = svg.querySelector(selector);
      if (!node) return null;
      const box = node.getBoundingClientRect();
      return { left: box.left, top: box.top, right: box.right, bottom: box.bottom, text: node.textContent ?? '' };
    };
    const overlap = (a, b) => !!a && !!b
      && Math.min(a.right, b.right) - Math.max(a.left, b.left) > 0.5
      && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 0.5;
    const xTitle = rect('.wf-axis-title--x');
    const yTitle = rect('.wf-axis-title--v');
    const scale = rect('.wf-scale-label');
    const vTicks = Array.from(svg.querySelectorAll('.wf-tick-label--v')).map(node => {
      const box = node.getBoundingClientRect();
      return { left: box.left, top: box.top, right: box.right, bottom: box.bottom };
    });
    const zeros = Array.from(svg.querySelectorAll('.wf-tick-label')).filter(node => (node.textContent ?? '').trim() === '0');
    return {
      xTitle, yTitle, scale, zeros: zeros.length,
      xTitleHitsScale: overlap(xTitle, scale),
      yTitleHitsTicks: vTicks.some(tick => overlap(yTitle, tick)),
      tickColor: getComputedStyle(svg.querySelector('.wf-tick-label')).fill,
      viewBox: svg.getAttribute('viewBox'),
    };
  });
  assert.ok(clipLayout.xTitle && clipLayout.xTitle.text.includes('X (µm)'), 'clip canvas must title the X axis in µm');
  assert.ok(clipLayout.yTitle && clipLayout.yTitle.text.includes('Y (µm)'), 'clip canvas must title the Y axis in µm');
  assert.ok(clipLayout.scale, 'clip canvas must draw a scale bar');
  assert.equal(clipLayout.xTitleHitsScale, false, `clip X title must not overlap the scale bar (${JSON.stringify(clipLayout)})`);
  assert.equal(clipLayout.yTitleHitsTicks, false, `clip Y title must not overlap a tick label (${JSON.stringify(clipLayout)})`);
  assert.equal(clipLayout.zeros, 1, `clip origin must be labelled once, got ${clipLayout.zeros}`);
  assert.equal(clipLayout.tickColor, 'rgb(38, 49, 58)', `clip tick text must stay dark, got ${clipLayout.tickColor}`);
  const legendText = await page.locator('.spx-layer-legend').innerText();
  assert.match(legendText, /SiO2/);
  const canvasBox = await page.locator('.wf-clip-canvas').boundingBox();
  assert.ok(canvasBox && canvasBox.height <= 260, `clip canvas must stay bounded, got ${JSON.stringify(canvasBox)}`);
  await page.getByLabel('Clip 名称').fill('IA clip');
  await page.getByRole('button', { name: '保存 Clip' }).click();
  await page.locator('.spx-clip-summary-list button').filter({ hasText: 'IA clip' }).waitFor();
  assert.match(await page.locator('.spx-clip-summary-list').innerText(), /层 · .*多边形/);

  // ---- Library presets are seeded and readable (swatch + catalog/material).
  const materials = (await getJson('/api/v1/workflow/library/items?category=material')).items;
  const processes = (await getJson('/api/v1/workflow/library/items?category=process')).items;
  assert.ok(materials.length >= 13, `expected seeded material presets, got ${materials.length}`);
  assert.ok(processes.length >= 3, `expected seeded process presets, got ${processes.length}`);
  await page.locator('[aria-label="库分类"] .spx-tree-item').filter({ hasText: '材料' }).first().click();
  await page.locator('.spx-library-category-head strong').filter({ hasText: '材料库' }).waitFor();
  await page.locator('.spx-library-manager-list button.spx-library-row').first().waitFor();
  assert.ok((await page.locator('.spx-library-manager-list .spx-library-row i').count()) >= 13);
  const firstRow = await page.locator('.spx-library-manager-list button.spx-library-row').first().innerText();
  assert.match(firstRow, /·/, 'material rows must show catalog · material');
  const roleSelect = page.getByLabel('库材料角色');
  await roleSelect.waitFor();
  const roleOptions = await roleSelect.locator('option').evaluateAll(nodes => nodes.map(node => node.value));
  assert.deepEqual(roleOptions, ['substrate', 'film', 'mask', 'resist']);
  await page.locator('[aria-label="库分类"] .spx-tree-item').filter({ hasText: '工艺' }).first().click();
  await page.locator('.spx-library-category-head strong').filter({ hasText: '工艺库' }).waitFor();
  await page.locator('.spx-library-manager-list button.spx-library-row').first().waitFor();
  assert.match(await page.locator('.spx-library-manager-list').innerText(), /(刻蚀|沉积).*µm\/s.*s/);
  await page.locator('[aria-label="库分类"] .spx-tree-item').filter({ hasText: '流程' }).first().click();
  await page.locator('.spx-library-category-head strong').filter({ hasText: '流程库' }).waitFor();
  await page.locator('.spx-library-reason').filter({ hasText: '先在 02 保存流程模板' }).waitFor();
  assert.equal(await page.getByRole('button', { name: '保存', exact: true }).isDisabled(), true);

  await page.screenshot({ path: screenshot, fullPage: true });
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({
    status: 'passed', baseUrl, screenshot, errors,
    seeded: { materials: materials.length, processes: processes.length },
  }));
} finally {
  await browser.close();
}
