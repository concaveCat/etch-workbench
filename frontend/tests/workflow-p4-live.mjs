/** P4 Chromium acceptance: modules/expressions, measurement decisions and shared library. */
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const here = path.dirname(fileURLToPath(import.meta.url));
const baseUrl = (process.env.WORKBENCH_URL ?? 'http://127.0.0.1:8817').replace(/\/$/, '');
const projectPath = process.env.WORKFLOW_PROJECT ?? path.join(here, 'fixtures', 'workflow-2d-five.json');
const screenshot = process.env.WORKFLOW_SCREENSHOT ?? '/tmp/workflow-p4.png';

const moduleJson = JSON.stringify({
  schemaVersion: 1,
  moduleId: 'mod_p4_ui',
  name: 'P4 UI etch module',
  description: '',
  parameters: [{ name: 'duration', default: 0.02, unit: 's' }],
  steps: [{
    id: 'step_mod_etch_01', name: 'module etch', enabled: true,
    operation: 'etch', model: 'isotropic', rateMagnitude: 0, duration: '${duration}', rateMode: 'uniform',
  }],
}, null, 2);

const definitionJson = JSON.stringify({
  schemaVersion: 2,
  definitionId: 'def_p4_ui',
  flowId: 'flow_p4_ui',
  revision: 1,
  name: 'P4 UI modular flow',
  description: '',
  variables: [],
  nodes: [
    { kind: 'module-call', instanceId: 'call_one', moduleId: 'mod_p4_ui', parameters: { duration: 0.02 } },
    { kind: 'module-call', instanceId: 'call_two', moduleId: 'mod_p4_ui', parameters: { duration: 0.04 } },
    {
      kind: 'repeat', repeatId: 'loop_tail', loopVariable: 'i', count: 2,
      body: [{
        kind: 'step',
        step: {
          id: 'step_ui_tail_01', name: 'loop tail', enabled: true,
          operation: 'etch', model: 'isotropic', rateMagnitude: 0,
          duration: '${0.01 + i * 0.01}', rateMode: 'uniform',
        },
      }],
    },
  ],
  solver: {
    timeStepRatio: 0.25, raysPerPoint: 16, cpuThreads: 1,
    random: { mode: 'fixed', seed: 7 }, enginePreference: 'cpu', timeoutSeconds: 30,
  },
}, null, 2);

const getJson = async endpoint => {
  const response = await fetch(`${baseUrl}${endpoint}`);
  assert.ok(response.ok, `${endpoint} -> ${response.status}`);
  return response.json();
};

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const page = await browser.newPage({ viewport: { width: 1600, height: 1500 } });
page.setDefaultTimeout(60000);
const errors = [];
page.on('pageerror', error => errors.push(error.message));

try {
  await page.goto(`${baseUrl}/`);
  await page.getByText('Etch Workbench').first().waitFor();
  await page.locator('input[type=file]').nth(1).setInputFiles(projectPath);
  await page.getByRole('button', { name: '去 02 定义流程' }).click();
  await page.getByRole('button', { name: '编译流程' }).click();
  await page.getByText(/编译成功/).waitFor();

  // P4-1: module + definition + loop expansion + compile.
  await page.getByText('高级：模块化流程定义（JSON）').click();
  await page.getByLabel('模块 JSON').fill(moduleJson);
  await page.getByRole('button', { name: '保存模块' }).click();
  await page.getByLabel('流程定义 JSON').fill(definitionJson);
  await page.getByRole('button', { name: '保存定义' }).click();
  await page.getByRole('button', { name: '展开预览' }).click();
  await page.getByText(/4 步 · 深度 \d+/).first().waitFor();
  assert.match(await page.locator('.wf-summary').filter({ hasText: '展开预览' }).innerText(), /2 模块实例 · 2 循环迭代/);
  await page.getByRole('button', { name: '编译模块化流程' }).click();
  await page.getByText(/模块化流程已编译 · 4 步/).waitFor();

  // P4-2: split route, execute prefix, measure, decide, execute branch, replay.
  await page.getByRole('button', { name: /^(创建 Route|去 03)$/ }).click();
  await page.getByRole('button', { name: '执行当前 Segment' }).click();
  await page.locator('.wf-status.succeeded').first().waitFor({ timeout: 60000 });
  await page.getByRole('button', { name: '测量', exact: true }).click();
  await page.getByText(/绑定 \(binding\)：已绑定/).first().waitFor();
  await page.getByText('高级：分支与测量决策').click();
  await page.getByLabel('决策阈值').fill('0.5');
  await page.getByRole('button', { name: '测量并生成分支' }).click();
  await page.locator('table[aria-label="测量决策列表"] tbody tr').first().waitFor();
  const decisionRow = page.locator('table[aria-label="测量决策列表"] tbody tr').first();
  assert.match(await decisionRow.innerText(), /wfr_/);
  assert.match(await decisionRow.innerText(), /false/);
  await page.getByRole('button', { name: '执行当前 Segment' }).click();
  await page.locator('.wf-status.succeeded').first().waitFor({ timeout: 60000 });
  await page.getByRole('button', { name: /重放决策/ }).first().click();
  await page.locator('table[aria-label="测量决策列表"] tbody tr').nth(1).waitFor();
  assert.match(await page.locator('table[aria-label="测量决策列表"] tbody tr').nth(1).innerText(), /replay of/);

  // P4-3: export a bundle, then clone-import it.
  await page.getByRole('button', { name: '02 流程' }).click();
  await page.getByText('高级：共享流程库').click();
  await page.getByLabel('共享库名称').fill('P4 UI bundle');
  await page.getByRole('button', { name: '导出共享库' }).click();
  await page.locator('table[aria-label="共享库列表"] tbody tr').first().waitFor();
  const bundleRow = page.locator('table[aria-label="共享库列表"] tbody tr').first();
  assert.match(await bundleRow.innerText(), /1 模块修订 · 1 定义/);
  await page.getByLabel('共享库 bundle 选择').selectOption({ index: 1 });
  await page.getByLabel('共享库导入策略').selectOption('clone');
  await page.getByRole('button', { name: '导入共享库', exact: true }).click();
  await page.getByLabel('共享库导入结果').waitFor();
  assert.match(await page.getByLabel('共享库导入结果').innerText(), /定义: def_p4_ui_/);

  // P4-4: the library manager occupies the main workspace (no modal) and
  // stores material presets by catalog + material from /api/materials.
  // It is entered from the left tree "库" group only — 02 has no manager tab.
  const libraryTree = page.locator('[aria-label="库分类"]');
  await libraryTree.locator('.spx-tree-item').filter({ hasText: '材料' }).first().click();
  await page.getByLabel('库管理').waitFor();
  assert.equal(await page.locator('.spx-library-manager-body').count(), 1);
  assert.equal(await page.locator('.spx-library-manager-list').count(), 1);
  assert.equal(await page.locator('.spx-library-manager-actions').count(), 1);
  assert.equal(await page.locator('.spx-modal-backdrop').count(), 0, 'library manager must live in the main workspace');
  assert.equal(await page.locator('.spx-flow-viewswitch').count(), 0, '02 must not have a library-manager tab');
  assert.equal(await page.getByRole('button', { name: '返回流程编辑' }).count(), 0, 'the manager header must be gone');
  assert.equal(await page.locator('.spx-library-manager-tabs').count(), 0, 'category tabs must live in the left tree only');

  // Switching categories from the left tree must replace the main-workspace
  // heading and form.
  const categoryChecks = [
    ['材料', '材料库', '库材料 Catalog', '库材料名称'],
    ['工艺', '工艺库', '库工艺操作', '库工艺模型'],
    ['流程', '流程库', '库流程引用', null],
    ['实验', '实验库', '库实验配方', null],
  ];
  for (const [treeLabel, heading, field, extra] of categoryChecks) {
    await libraryTree.locator('.spx-tree-item').filter({ hasText: treeLabel }).first().click();
    await page.locator('.spx-library-category-head strong').filter({ hasText: heading }).waitFor();
    assert.equal(await page.getByLabel(field).count(), 1, `${treeLabel} must show ${field}`);
    if (extra) assert.equal(await page.getByLabel(extra).count(), 1, `${treeLabel} must show ${extra}`);
  }
  await libraryTree.locator('.spx-tree-item').filter({ hasText: '材料' }).first().click();
  await page.locator('.spx-library-category-head strong').filter({ hasText: '材料库' }).waitFor();
  assert.equal(await page.getByLabel('库材料 Catalog').count(), 1);
  await page.getByRole('button', { name: '新增库条目' }).click();
  await page.getByLabel('库条目名称').fill('P4 SiO2 preset');
  await page.getByLabel('库材料 Catalog').selectOption('OxideNitride');
  await page.getByLabel('库材料名称').selectOption('SiO2');
  await page.getByRole('textbox', { name: '库材料颜色', exact: true }).fill('blue');
  assert.equal(await page.locator('.spx-color-swatch.invalid').count(), 1);
  await page.getByRole('textbox', { name: '库材料颜色', exact: true }).fill('#123456');
  assert.equal(await page.locator('.spx-color-swatch.invalid').count(), 0);
  assert.match(await page.locator('.spx-color-swatch').getAttribute('style') ?? '', /rgb\(18, 52, 86\)/);
  await page.getByRole('button', { name: '保存', exact: true }).click();
  await page.getByText('已新增 P4 SiO2 preset').waitFor();
  await page.locator('.spx-library-manager-list button').filter({ hasText: 'P4 SiO2 preset' }).click();
  assert.equal(await page.getByLabel('库条目名称').inputValue(), 'P4 SiO2 preset');
  await libraryTree.locator('.spx-tree-item').filter({ hasText: '工艺' }).first().click();
  await page.locator('.spx-library-category-head strong').filter({ hasText: '工艺库' }).waitFor();
  await libraryTree.locator('.spx-tree-item').filter({ hasText: '材料' }).first().click();
  await page.locator('.spx-library-manager-list button').filter({ hasText: 'P4 SiO2 preset' }).click();
  assert.equal(await page.getByLabel('库条目名称').inputValue(), 'P4 SiO2 preset');

  // 02 in the top bar returns to the flow editor (no in-manager back button).
  await page.getByRole('button', { name: '02 流程' }).click();
  await page.locator('.spx-library-view').waitFor({ state: 'detached' });
  await page.locator('.spx-flow-editor').waitFor();
  assert.equal(await page.getByLabel('库管理').count(), 0);

  // The left project tree's library categories must switch the main workspace
  // even when another zone is active.
  await page.getByRole('button', { name: '01 工程' }).click();
  await libraryTree.locator('.spx-tree-item').filter({ hasText: '材料' }).first().click();
  await page.locator('.spx-doc-header h1').filter({ hasText: '材料库' }).waitFor();
  await page.locator('.spx-library-category-head strong').filter({ hasText: '材料库' }).waitFor();
  await libraryTree.locator('.spx-tree-item').filter({ hasText: '工艺' }).first().click();
  await page.locator('.spx-library-category-head strong').filter({ hasText: '工艺库' }).waitFor();
  await page.getByRole('button', { name: '02 流程' }).click();
  await page.locator('.spx-flow-editor').waitFor();

  const stored = (await getJson('/api/v1/workflow/library/items?category=material')).items;
  const preset = stored.find(item => item.name === 'P4 SiO2 preset');
  assert.ok(preset, JSON.stringify(stored));
  assert.equal(preset.payload.catalog, 'OxideNitride');
  assert.equal(preset.payload.material, 'SiO2');
  assert.equal(preset.payload.color, '#123456');

  await page.screenshot({ path: screenshot, fullPage: true });
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ status: 'passed', baseUrl, screenshot, errors }));
} finally {
  await browser.close();
}
