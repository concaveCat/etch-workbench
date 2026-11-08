/** P2 Chromium acceptance: layout clip canvas, 3 clip compiles, DOE queue and table/export. */
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const here = path.dirname(fileURLToPath(import.meta.url));
const baseUrl = (process.env.WORKBENCH_URL ?? 'http://127.0.0.1:8815').replace(/\/$/, '');
const layoutPath = process.env.WORKFLOW_LAYOUT ?? path.resolve(here, '../../docs/stage5-evidence/two-structure-two-layer.gds');
const projectPath = process.env.WORKFLOW_PROJECT ?? path.join(here, 'fixtures', 'workflow-2d-five.json');
const screenshot = process.env.WORKFLOW_SCREENSHOT ?? '/tmp/workflow-p2.png';

const getJson = async endpoint => {
  const response = await fetch(`${baseUrl}${endpoint}`);
  assert.ok(response.ok, `${endpoint} -> ${response.status}`);
  return response.json();
};

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const page = await browser.newPage({ viewport: { width: 1600, height: 1400 } });
page.setDefaultTimeout(60000);
const errors = [];
page.on('pageerror', error => errors.push(error.message));

try {
  await page.goto(`${baseUrl}/`);
  await page.getByText('Etch Workbench').first().waitFor();
  await page.locator('input[type=file]').nth(0).setInputFiles(layoutPath);
  await page.locator('.spx-file-name').filter({ hasText: 'two-structure-two-layer.gds' }).first().waitFor();
  await page.locator('input[type=file]').nth(1).setInputFiles(projectPath);
  await page.getByRole('button', { name: 'GDS / Layout' }).click();
  await page.getByRole('button', { name: '注册为 Layout' }).click();
  await page.getByLabel('版图结构').waitFor();
  await page.getByRole('button', { name: '预览' }).click();
  await page.locator('.wf-clip-canvas polygon').first().waitFor();
  assert.match(await page.locator('.wf-canvas-wrap').innerText(), /多边形/);

  // Three clips: two structures and one cropped/annotated region.
  await page.getByLabel('Clip 名称').fill('clip A');
  await page.getByRole('button', { name: '保存 Clip' }).click();
  await page.locator('.spx-clip-summary-list button').filter({ hasText: 'clip A' }).waitFor();
  await page.getByLabel('版图结构').selectOption('CELL_B');
  await page.getByLabel('Clip 名称').fill('clip B');
  await page.getByRole('button', { name: '保存 Clip' }).click();
  await page.locator('.spx-clip-summary-list button').filter({ hasText: 'clip B' }).waitFor();
  await page.getByLabel('版图结构').selectOption('CELL_A');
  for (const [label, value] of [['Clip x0', '0.5'], ['Clip y0', '0.2'], ['Clip x1', '1.5'], ['Clip y1', '0.8']]) {
    await page.getByLabel(label).fill(value);
  }
  await page.getByRole('button', { name: '预览' }).click();
  await page.locator('.wf-clip-canvas polygon').first().waitFor();
  await page.getByLabel('Clip 名称').fill('clip C');
  await page.getByRole('button', { name: '保存 Clip' }).click();
  await page.locator('.spx-clip-summary-list button').filter({ hasText: 'clip C' }).waitFor();
  await page.getByLabel('标注文本').fill('center');
  await page.getByLabel('标注 X').fill('0.5');
  await page.getByLabel('标注 Y').fill('0.3');
  await page.getByRole('button', { name: '添加标注' }).click();
  await page.locator('.wf-annotation').first().waitFor();
  const clips = (await getJson('/api/v1/workflow/clips')).clips;
  assert.equal(clips.length, 3, JSON.stringify(clips.map(item => item.name)));
  assert.deepEqual(clips.map(item => item.name).sort(), ['clip A', 'clip B', 'clip C']);
  assert.equal(clips.find(item => item.name === 'clip C').annotations.length, 1);

  const hashes = [];
  for (const name of ['clip A', 'clip B', 'clip C']) {
    await page.locator('.spx-clip-summary-list button').filter({ hasText: name }).click();
    const [response] = await Promise.all([
      page.waitForResponse(item => item.url().includes('/compile') && item.request().method() === 'POST'),
      page.getByRole('button', { name: '用此 Clip 编译' }).click(),
    ]);
    assert.equal(response.status(), 201, await response.text());
    const body = await response.json();
    assert.equal(body.binding.geometrySource.clipId, clips.find(item => item.name === name).clipId);
    hashes.push(body.compile.compileRecord.geometryBindingHash);
    await page.getByText(/流程已编译/).first().waitFor();
  }
  assert.equal(new Set(hashes).size, 3, 'three clips must produce three geometry bindings');

  // Run the last clip compile and take a measurement so the DOE has an active recipe.
  await page.getByRole('button', { name: '02 流程' }).click();
  await page.getByRole('button', { name: /^(创建 Route|去 03)$/ }).click();
  await page.getByRole('button', { name: '执行全部 Segment' }).click();
  await page.locator('.wf-status.succeeded').first().waitFor({ timeout: 60000 });
  await page.getByRole('button', { name: '测量', exact: true }).click();
  await page.getByText(/绑定 \(binding\)：已绑定/).first().waitFor();

  // 03 timeline is one initial structure plus every Route step; it no longer
  // repeats a per-segment starting state.
  await page.locator('.wf-structure-timeline').waitFor();
  assert.equal(await page.locator('.wf-step-chip[data-step-id="initial"]').count(), 1);
  assert.equal(await page.locator('.wf-step-chip').filter({ hasText: 'Segment 2 起始' }).count(), 0);
  assert.equal(await page.locator('.wf-step-chip[data-kind="step"]').count(), 5);
  assert.equal(await page.locator('.wf-segment-structure:not([disabled])').count(), 2);

  await page.getByRole('button', { name: '04 实验' }).click();
  // The default DOE has two legal factors; add a third so the full factorial
  // design stays 2×2×2 = 8 conditions.
  assert.equal(await page.locator('.wf-doe-factor').count(), 2, 'DOE defaults to two factors');
  await page.getByRole('button', { name: '＋ 添加因子' }).click();
  assert.equal(await page.locator('.wf-doe-factor').count(), 3, 'adding a factor keeps the design usable');
  for (const index of [0, 1, 2]) {
    await page.getByLabel(`因子 ${index + 1} 取值`).fill('0.01,0.02');
  }
  await page.getByLabel('DOE 重复数').fill('1');
  await page.getByRole('button', { name: '设计 DOE' }).click();
  await page.locator('[aria-label="DOE 设计摘要"]').filter({ hasText: '8 条件 × 1 重复 = 8 逻辑执行' }).waitFor();
  await page.getByRole('button', { name: '运行实验', exact: true }).click();
  await page.getByText('实验状态：已完成').waitFor({ timeout: 180000 });
  assert.match(await page.locator('.wf-summary').filter({ hasText: '队列' }).innerText(), /8\/8 运行成功/);
  // Results come first; the inputs fold away after a run and reopen on demand.
  await page.locator('.wf-experiment-inputs > summary').click();
  await page.getByRole('button', { name: '筛选 / 排序' }).click();
  const statsTable = page.locator('table.wf-table').filter({ hasText: '统计状态' });
  await statsTable.waitFor();
  await statsTable.locator('tbody tr').first().waitFor();
  assert.equal(await statsTable.locator('tbody tr').count(), 8);
  const csv = await page.getByRole('link', { name: '导出 CSV' }).getAttribute('href');
  assert.match(csv ?? '', /\/experiments\/exp_[a-f0-9]+\/export\.csv$/);

  // Retention preview/release/restore must complete without a 405 error.
  await page.locator('.wf-experiment-inputs details > summary').filter({ hasText: '高级：保留策略' }).click();
  await page.getByRole('button', { name: '预览释放' }).click();
  await page.getByText(/可释放 8 个 Run/).waitFor();
  await page.getByRole('button', { name: '应用 release-runs' }).click();
  await page.getByText(/当前 release-runs/).waitFor();
  assert.equal(await page.locator('.wf-error').count(), 0);
  await page.getByRole('button', { name: '恢复 keep-all' }).click();
  await page.getByText(/当前 keep-all/).waitFor();
  assert.equal(await page.locator('.wf-error').count(), 0);

  const experiment = (await getJson('/api/v1/workflow/experiments')).experiments[0];
  const document = await getJson(`/api/v1/workflow/experiments/${experiment.experimentId}`);
  assert.equal(document.kind, 'doe');
  assert.equal(document.binding.geometrySource.clipId, clips.find(item => item.name === 'clip C').clipId);
  assert.equal(document.binding.geometrySource.layoutId, clips.find(item => item.name === 'clip C').layoutId);
  assert.equal(document.design.conditionCount, 8);
  assert.equal(document.queue.succeeded, 8);
  assert.equal(document.aggregation.rows.length, 8);
  await page.screenshot({ path: screenshot, fullPage: true });
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({
    status: 'passed', baseUrl, screenshot, errors, clipBindings: hashes,
    doe: { conditions: document.design.conditionCount, runs: document.queue.succeeded, unit: document.unit },
  }));
} finally {
  await browser.close();
}
