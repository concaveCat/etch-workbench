/** Chromium checks for the refactor activity rail, context panels, solver settings and output dock. */
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const baseUrl = (process.env.WORKBENCH_URL ?? 'http://127.0.0.1:8803').replace(/\/$/, '');
const evidenceDir = path.join(root, 'docs/stage9-evidence/ui-refactor-alignment');
await fs.mkdir(evidenceDir, { recursive: true });
const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const page = await browser.newPage({ viewport: { width: 1680, height: 1100 } });
page.setDefaultTimeout(15000);
const pageErrors = [];
page.on('pageerror', error => pageErrors.push({ message: error.message, stack: error.stack }));
const activities = [
  ['结构', 'structure'], ['配方', 'recipe'], ['材料', 'materials'], ['结果', 'results'], ['设置', 'settings'],
];
try {
  await page.goto(`${baseUrl}/`);
  await page.getByText('本机服务已连接').waitFor();
  const designSurfaces = [
    ['top bar', '.topbar-shell'],
    ['activity rail', '.activity-rail'],
    ['context panel', '.context-panel-content'],
    ['viewer', '.viewer-panel'],
    ['property inspector', '.inspector-panel'],
    ['output dock', '.dock-tabs'],
    ['status bar', '.status-bar'],
  ];
  for (const [name, selector] of designSurfaces) {
    assert.equal(await page.locator(selector).isVisible(), true, `reference design surface is visible: ${name}`);
  }
  const rail = page.locator('.activity-rail');
  const labels = await rail.locator('.rail-item').evaluateAll(nodes => nodes.map(node => node.textContent.trim()));
  assert.deepEqual(labels, activities.map(([label]) => label), 'activity rail order and labels');
  const transitions = [];
  for (const [label, id] of activities) {
    await rail.getByRole('button', { name: label, exact: true }).click();
    const slot = page.locator(`.activity-slot[data-activity="${id}"]`);
    await slot.waitFor({ state: 'visible' });
    transitions.push({ label, id, visible: await slot.isVisible() });
  }
  await rail.getByRole('button', { name: '配方', exact: true }).click();
  await page.getByRole('button', { name: '＋ 添加单粒子步骤', exact: true }).click();
  const inspectorTabs = page.getByRole('tablist', { name: '步骤参数分组' });
  await inspectorTabs.waitFor({ state: 'visible' });
  const inspectorTabLabels = await inspectorTabs.getByRole('tab').evaluateAll(nodes => nodes.map(node => node.textContent.trim()));
  assert.deepEqual(inspectorTabLabels, ['基本', '物理', '原生参数'], 'reference inspector tab structure');
  await page.screenshot({ path: path.join(evidenceDir, 'recipe-inspector-layout.png'), fullPage: true });
  await rail.getByRole('button', { name: '设置', exact: true }).click();
  const engine = page.getByLabel('引擎偏好', { exact: true });
  const defaultEngine = await engine.inputValue();
  assert.equal(defaultEngine, 'auto', 'new projects default to automatic engine selection');
  const gpu = page.getByLabel('计算 GPU', { exact: true });
  const defaultGpu = await gpu.inputValue();
  assert.equal(defaultGpu, 'auto');
  const inventoryResponse = await page.request.get(`${baseUrl}/api/gpu-devices`);
  assert.equal(inventoryResponse.status(), 200);
  const inventory = await inventoryResponse.json();
  const uuids = await gpu.locator('option').evaluateAll(options => options.map(option => option.value));
  for (const device of inventory.devices) assert.ok(uuids.includes(device.uuid), `missing GPU option ${device.uuid}`);
  const dockTabs = await page.locator('.dock-tabs [role="tab"]').evaluateAll(nodes => nodes.map(node => node.textContent.trim()));
  assert.deepEqual(dockTabs, ['日志', '通量', '中间场', '原生参数', '产物'], 'output dock tabs');
  await page.screenshot({ path: path.join(evidenceDir, 'desktop-layout.png'), fullPage: true });

  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.locator('.topbar-shell details.ui-menu[open]').count(), 0,
    'default-open project menus must close at the reference mobile breakpoint');
  const mobileTabs = page.locator('.mobile-tabs');
  await mobileTabs.waitFor({ state: 'visible' });
  await mobileTabs.getByRole('button', { name: '材料', exact: true }).click();
  await page.locator('.activity-slot[data-activity="materials"]').waitFor({ state: 'visible' });
  await page.screenshot({ path: path.join(evidenceDir, 'mobile-layout.png'), fullPage: true });
  assert.deepEqual(pageErrors, []);
  const report = {
    status: 'passed', service: baseUrl, browser: 'Chromium', activities: transitions,
    designSurfaces: Object.fromEntries(designSurfaces.map(([name]) => [name, true])),
    inspectorTabs: inspectorTabLabels,
    settings: { defaultEngine, defaultGpu, gpuChoices: inventory.devices },
    dockTabs, mobileActivityNavigation: true, pageErrors,
  };
  await fs.writeFile(path.join(evidenceDir, 'layout-acceptance.json'), `${JSON.stringify(report, null, 2)}\n`);
  console.log(JSON.stringify({ status: report.status, activityCount: transitions.length, dockTabs,
    gpuChoices: inventory.devices.length, evidence: path.relative(root, path.join(evidenceDir, 'layout-acceptance.json')) }, null, 2));
} catch (error) {
  await fs.writeFile(path.join(evidenceDir, 'layout-acceptance.failure.json'), `${JSON.stringify({
    status: 'failed', message: error.message, stack: error.stack, pageErrors,
  }, null, 2)}\n`);
  throw error;
} finally {
  await browser.close();
}
