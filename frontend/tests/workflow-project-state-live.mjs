/**
 * P0 project-state acceptance: saved 01/02/03 state survives a reload.
 *
 * Covers review items B1 (workflow state restore), B2 (global save), H8
 * (editable 01 parameters) and H7 (GDS/Clip geometry binding).
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const here = path.dirname(fileURLToPath(import.meta.url));
const baseUrl = (process.env.WORKBENCH_URL ?? 'http://127.0.0.1:8889').replace(/\/$/, '');
const projectPath = process.env.WORKFLOW_PROJECT ?? path.join(here, 'fixtures', 'workflow-2d-five.json');
const layoutPath = process.env.WORKFLOW_LAYOUT
  ?? path.resolve(here, '../../docs/stage5-evidence/two-structure-two-layer.gds');
const screenshot = process.env.WORKFLOW_SCREENSHOT ?? '/tmp/workflow-project-state.png';
const fixtureName = JSON.parse(readFileSync(projectPath, 'utf8')).name;

// Re-runs against a persistent preview data dir must not hit the 409
// "same name, different content" guard: drop older copies of this fixture.
const existingProjects = await (await fetch(`${baseUrl}/api/v1/workflow/projects`)).json();
for (const row of existingProjects.projects ?? []) {
  if (row.name === fixtureName) {
    await fetch(`${baseUrl}/api/v1/workflow/projects/${row.projectId}`, { method: 'DELETE' });
  }
}

const postJson = async (endpoint, body) => {
  const response = await fetch(`${baseUrl}${endpoint}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const text = await response.text();
  assert.ok(response.ok, `${endpoint} -> ${response.status} ${text}`);
  return JSON.parse(text);
};

// Seed one saved Clip through the API so the 01 GDS tab has an existing
// layout/clip to pick without replaying the whole P2 upload flow.
const gdsBytes = readFileSync(layoutPath);
const upload = await fetch(`${baseUrl}/api/gds-assets?file_name=${encodeURIComponent(path.basename(layoutPath))}`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/octet-stream' },
  body: gdsBytes,
});
assert.ok(upload.ok, `gds upload -> ${upload.status}`);
const asset = await upload.json();
const layout = await postJson('/api/v1/workflow/layouts', { assetId: asset.assetId, fileName: asset.fileName });
const structure = layout.structures[0];
const clip = await postJson('/api/v1/workflow/clips', {
  layoutId: layout.layoutId,
  name: 'accept clip',
  structure: structure.name,
  layers: structure.layerDatatypes.map(([layer, datatype]) => ({ layer, datatype, material: 'Si' })),
  bbox: structure.bounds,
  annotations: [],
});

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const page = await browser.newPage({ viewport: { width: 1600, height: 1500 } });
page.setDefaultTimeout(60000);
const errors = [];
page.on('pageerror', error => errors.push(error.message));

const openZone = async name => {
  await page.getByRole('button', { name, exact: true }).click();
};

try {
  await page.goto(`${baseUrl}/`);
  await page.getByText('Etch Workbench').first().waitFor();

  // --- B2: a draft is saved from the global top-bar action -----------------
  await page.locator('input[type=file]').nth(1).setInputFiles(projectPath);
  await page.locator('.spx-save-button').click();
  await page.locator('.spx-save-button').filter({ hasText: '已保存' }).waitFor();
  const projectId = await page.evaluate(() => window.localStorage.getItem('viennaps.workflow.projectId'));
  assert.ok(projectId && projectId.startsWith('proj_'), `project id must be persisted, got ${projectId}`);

  // --- A: compile → route → execute → measure, then reload ----------------
  await page.getByRole('button', { name: '去 02 定义流程' }).click();
  await page.getByRole('button', { name: '编译流程' }).click();
  await page.getByText(/编译成功/).waitFor();
  await page.getByRole('button', { name: '创建 Route' }).click();
  await page.getByRole('button', { name: '执行全部 Segment' }).click();
  await page.locator('.wf-status.succeeded').first().waitFor({ timeout: 180000 });
  await page.getByRole('button', { name: '测量', exact: true }).click();
  await page.getByText(/绑定 \(binding\)：已绑定/).first().waitFor();
  await page.locator('.wf-structure-timeline').waitFor();
  const chipsBeforeReload = await page.locator('.wf-step-chip').count();
  assert.ok(chipsBeforeReload >= 2, 'timeline needs the initial structure plus steps');

  // The 300 ms debounced state PUT must land before the reload.
  await page.waitForTimeout(1500);
  const state = await (await fetch(`${baseUrl}/api/v1/workflow/projects/${projectId}/state`)).json();
  assert.ok(state.compileId, 'state must remember the compile');
  assert.ok(state.routeId, 'state must remember the route');
  assert.ok(state.measurementIds.length >= 1, 'state must remember the measurement');
  assert.ok(state.segmentRuns.length >= 1, 'state must remember executed segments');

  // --- A2: 04 restores the experiments saved in the project state ---------
  await openZone('04 实验');
  await page.getByLabel('实验名称').fill('STATE 实验');
  await page.getByRole('button', { name: '创建实验', exact: true }).click();
  await page.getByText('实验状态：待执行').waitFor();
  await page.waitForTimeout(1500);
  const stateWithExperiment = await (await fetch(`${baseUrl}/api/v1/workflow/projects/${projectId}/state`)).json();
  assert.ok(stateWithExperiment.experimentIds.length >= 1, 'state must remember the experiment');
  await page.reload();
  await page.getByText('Etch Workbench').first().waitFor();
  await openZone('04 实验');
  await page.locator('[aria-label="实验列表"]').waitFor({ timeout: 30000 });
  assert.match(await page.locator('[aria-label="实验列表"]').innerText(), /STATE 实验/,
    '04 must list the experiment restored from the project state');

  await page.reload();
  await page.getByText('Etch Workbench').first().waitFor();
  await openZone('03 运行');
  await page.locator('.wf-summary').filter({ hasText: 'Route 已创建' }).waitFor();
  await page.locator('.wf-structure-timeline').waitFor({ timeout: 30000 });
  assert.ok(await page.locator('.wf-step-chip').count() >= chipsBeforeReload, 'reload must rebuild the structure timeline');
  assert.match(await page.locator('.wf-status-row').innerText(), /第 \d+ 段 · 成功/);
  const treeAfterReload = await page.locator('.spx-tree').first().innerText();
  assert.match(treeAfterReload, /已编译/);
  assert.match(treeAfterReload, /已创建/);
  // One status per row: the left value column must not repeat the badge.
  assert.equal((treeAfterReload.match(/未编译/g) ?? []).length, 0, 'compiled project must not say 未编译 twice');
  assert.equal((treeAfterReload.match(/已编译/g) ?? []).length, 1, 'compile status belongs to the badge only');

  await openZone('02 流程');
  await page.locator('.spx-flow-bar').getByRole('button', { name: '去 03' }).waitFor();
  assert.equal(await page.locator('.spx-flow-bar').getByRole('button', { name: '创建 Route' }).count(), 0,
    '02 must offer 去 03, not 创建 Route, when a route exists');

  // --- B: edit 02, save with Ctrl+S, reload, steps stay -------------------
  await page.getByRole('button', { name: '＋ 添加步骤' }).click();
  const stepRows = await page.locator('.spx-flow-steps tbody tr').count();
  await page.keyboard.press('Control+s');
  await page.waitForTimeout(1200);
  await page.reload();
  await page.getByText('Etch Workbench').first().waitFor();
  await openZone('02 流程');
  await page.locator('.spx-flow-steps tbody tr').first().waitFor();
  assert.equal(await page.locator('.spx-flow-steps tbody tr').count(), stepRows, 'Ctrl+S must persist the inserted step');

  // --- C: 01 parameters are editable, validated and persisted -------------
  await openZone('01 工程');
  await page.getByLabel('工程 gridDelta').fill('0.1');
  await page.getByLabel('工程引擎').selectOption('auto');
  await page.getByLabel('工程 seed').fill('123');
  await page.locator('.spx-save-button').click();
  await page.locator('.spx-save-button').filter({ hasText: '已保存' }).waitFor();
  await page.waitForTimeout(500);
  await page.reload();
  await page.getByText('Etch Workbench').first().waitFor();
  await openZone('01 工程');
  assert.equal(await page.getByLabel('工程 gridDelta').inputValue(), '0.1');
  assert.equal(await page.getByLabel('工程引擎').inputValue(), 'auto');
  assert.equal(await page.getByLabel('工程 seed').inputValue(), '123');
  await openZone('02 流程');
  await page.locator('.spx-flow-steps tbody tr').first().waitFor();
  assert.equal(await page.locator('.spx-flow-steps tbody tr').count(), stepRows, 'editing 01 must not drop steps');

  // Invalid values are reported inline and block saving.
  await openZone('01 工程');
  await page.getByLabel('工程 gridDelta').fill('0.9');
  await page.locator('.spx-field-error').filter({ hasText: 'gridDelta 必须 > 0 且 ≤ 0.5 µm' }).first().waitFor();
  assert.equal(await page.locator('.spx-sheet').filter({ hasText: '工程定义' }).getByRole('button', { name: /保存更改|保存工程/ }).isDisabled(), true);
  await page.getByLabel('工程 gridDelta').fill('0.1');

  // --- D: GDS tab blocks a save until an existing Clip is selected --------
  await page.getByRole('button', { name: 'GDS / Layout' }).click();
  await page.getByText('先选择或保存一个 Clip 作为工程几何').first().waitFor();
  assert.equal(await page.locator('.spx-sheet').filter({ hasText: '工程定义' }).getByRole('button', { name: /保存更改|保存工程/ }).isDisabled(), true,
    'saving must be blocked while the GDS tab has no clip');
  const clipButton = page.locator('[aria-label="已有 Clip"] button').filter({ hasText: clip.name }).first();
  await clipButton.waitFor({ timeout: 30000 });
  await clipButton.click();
  await page.locator('.spx-sheet').filter({ hasText: '工程定义' }).getByRole('button', { name: /保存更改|保存工程/ }).click();
  await page.locator('.spx-save-button').filter({ hasText: '已保存' }).waitFor();
  await page.waitForTimeout(500);

  await page.reload();
  await page.getByText('Etch Workbench').first().waitFor();
  await openZone('01 工程');
  await page.getByText(`Clip · ${clip.name}`).first().waitFor({ timeout: 30000 });
  const savedProject = await (await fetch(`${baseUrl}/api/v1/workflow/projects/${projectId}`)).json();
  assert.equal(savedProject.geometry.kind, 'gds', 'saving the GDS tab must write the Clip geometry binding');
  assert.equal(savedProject.geometry.assetId, clip.gdsAssetId);

  // --- E: recent projects offer an explicit 打开 action -------------------
  await page.locator('.spx-save-button').click();
  await page.locator('.spx-save-button').filter({ hasText: '已保存' }).waitFor();
  await page.getByRole('button', { name: '关闭工程' }).click();
  await page.locator('.spx-recent-projects').waitFor();
  const recentRow = page.locator('.spx-project-row').filter({ hasText: 'P1 2D five-step fixture' }).first();
  await recentRow.waitFor();
  await recentRow.getByRole('button', { name: /^打开/ }).waitFor();
  const emptyTree = await page.locator('.spx-tree').first().innerText();
  assert.equal((emptyTree.match(/未编译/g) ?? []).length, 1, 'an empty compile row shows the status once');

  await page.screenshot({ path: screenshot, fullPage: true });
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ status: 'passed', baseUrl, projectId, clipId: clip.clipId, screenshot, errors }));
} finally {
  await browser.close();
}
