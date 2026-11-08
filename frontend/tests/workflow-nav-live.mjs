/** 02 navigation + dismissable banners Chromium acceptance.
 *
 * The library manager is a main-workspace view reached only from the left
 * tree "库" group: 材料/工艺/流程/实验 each open their own category, the 02
 * tab strip and the manager's own category tabs are gone, and the top "02 流程"
 * button is the only way back to the flow editor. One-shot banners carry a ×.
 */
import assert from 'node:assert/strict';
import { writeFileSync, rmSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const here = path.dirname(fileURLToPath(import.meta.url));
const baseUrl = (process.env.WORKBENCH_URL ?? 'http://127.0.0.1:8841').replace(/\/$/, '');
const projectPath = process.env.WORKFLOW_PROJECT ?? path.join(here, 'fixtures', 'workflow-2d-five.json');
const screenshot = process.env.WORKFLOW_SCREENSHOT ?? path.resolve(here, '../../var/workflow-nav.png');
const invalidProjectPath = path.join(os.tmpdir(), `workflow-nav-invalid-${process.pid}.json`);
writeFileSync(invalidProjectPath, '{ this is not a project ');

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const page = await browser.newPage({ viewport: { width: 1600, height: 1100 } });
page.setDefaultTimeout(60000);
const errors = [];
page.on('pageerror', error => errors.push(error.message));

const libraryTree = () => page.locator('[aria-label="库分类"]');
const clickLibraryCategory = async label => {
  await libraryTree().locator('.spx-tree-item').filter({ hasText: label }).first().click();
};
const activeLibraryCategory = async () => {
  const rows = libraryTree().locator('.spx-tree-item');
  const count = await rows.count();
  const active = [];
  for (let index = 0; index < count; index += 1) {
    const row = rows.nth(index);
    if ((await row.getAttribute('aria-selected')) === 'true') active.push((await row.innerText()).trim().split('\n')[0]);
  }
  return active;
};

try {
  await page.goto(`${baseUrl}/`);
  await page.getByText('Etch Workbench').first().waitFor();

  // ---- 01: import a project and save it so projectNotice has something to say.
  await page.locator('input[type=file]').nth(1).setInputFiles(projectPath);
  await page.locator('.spx-save-button').filter({ hasText: '草稿（未保存）' }).waitFor();
  await page.getByRole('button', { name: '保存工程', exact: true }).click();
  await page.locator('.spx-project-status').filter({ hasText: '已写入服务端' }).waitFor();

  const projectNotice = page.locator('.spx-project-notice');
  await projectNotice.waitFor();
  assert.match(await projectNotice.innerText(), /已保存/);
  const noticeClose = projectNotice.getByRole('button', { name: '关闭提示' });
  assert.equal(await noticeClose.count(), 1, 'project notice must expose a × with aria-label 关闭提示');
  await noticeClose.click();
  await projectNotice.waitFor({ state: 'detached' });
  assert.equal(await page.locator('.spx-project-status').filter({ hasText: '已写入服务端' }).count(), 1, 'closing the notice must not touch project state');

  // ---- 02 has no view switch, no manager header and no manager category tabs.
  await page.getByRole('button', { name: '02 流程' }).click();
  await page.locator('.spx-flow-editor').waitFor();
  assert.equal(await page.locator('.spx-flow-viewswitch').count(), 0, '02 must not render the 流程编辑/库管理 tab strip');
  assert.equal(await page.getByRole('tab', { name: '库管理' }).count(), 0);
  assert.equal(await page.getByRole('button', { name: '返回流程编辑' }).count(), 0);
  assert.equal(await page.locator('.spx-library-manager-tabs').count(), 0);
  assert.doesNotMatch(await page.locator('.spx-library-head').innerText(), /管理库/, '02 library browser must not link to the manager');

  // ---- The left tree drives every library category.
  const categoryChecks = [
    ['材料', '材料库', ['库材料 Catalog', '库材料名称']],
    ['工艺', '工艺库', ['库工艺操作', '库工艺模型']],
    ['流程', '流程库', ['库流程引用']],
    ['实验', '实验库', ['库实验配方']],
  ];
  for (const [treeLabel, heading, fields] of categoryChecks) {
    await clickLibraryCategory(treeLabel);
    await page.locator('.spx-library-view').waitFor();
    await page.locator('.spx-library-category-head strong').filter({ hasText: heading }).waitFor();
    await page.locator('.spx-library-category-head').filter({ hasText: /·\s*\d+ 条/ }).waitFor();
    assert.match(await page.locator('.spx-library-category-head').innerText(), new RegExp(`${heading}\\s*·\\s*\\d+ 条`), `${heading} head must show the item count`);
    for (const field of fields) {
      assert.equal(await page.getByLabel(field).count(), 1, `${heading} must show ${field}`);
    }
    // The manager shows exactly one category, with no English category tabs.
    assert.equal(await page.locator('.spx-library-manager-tabs').count(), 0);
    assert.equal(await page.locator('.spx-library-view').getByRole('button', { name: /^(Materials|Processes|Flows|Experiments)$/ }).count(), 0);
    // Active highlight follows the open category.
    assert.deepEqual(await activeLibraryCategory(), [treeLabel], `${treeLabel} must be the only active tree row`);
  }

  // ---- Flow library empty state must not be a dead end.
  await clickLibraryCategory('材料');
  await page.locator('.spx-library-view').waitFor();
  await page.screenshot({ path: screenshot.replace(/\.png$/, '-material.png'), fullPage: true });

  // Manager status is a one-shot banner too: save an entry, then dismiss it.
  await page.getByRole('button', { name: '新增库条目' }).click();
  await page.getByLabel('库条目名称').fill('Nav probe');
  await page.getByRole('button', { name: '保存', exact: true }).click();
  const managerBanner = page.locator('.spx-library-manager-form .wf-summary[role="status"]');
  await managerBanner.waitFor();
  await managerBanner.getByRole('button', { name: '关闭提示' }).click();
  await managerBanner.waitFor({ state: 'detached' });

  await clickLibraryCategory('流程');
  await page.locator('.spx-library-view').getByText('流程库暂无条目').waitFor();
  await page.screenshot({ path: screenshot.replace(/\.png$/, '-flow-empty.png'), fullPage: true });
  const flowEscape = page.locator('.spx-library-view').getByRole('button', { name: '先在 02 保存流程模板' });
  await flowEscape.waitFor();
  assert.equal(await page.getByLabel('库流程引用').count(), 1, 'flow form keeps its reference picker');
  assert.match(await page.locator('.spx-library-reason').innerText(), /先在 02 保存流程模板/);
  await flowEscape.click();
  await page.locator('.spx-flow-editor').waitFor();
  assert.equal(await page.locator('.spx-library-view').count(), 0, 'the empty-state escape must leave the manager');

  // ---- Clicking 流程 really switches the workspace (not just the highlight).
  await clickLibraryCategory('流程');
  await page.locator('.spx-library-view').waitFor();
  assert.equal(await page.locator('.spx-flow-editor').count(), 0, 'flow editor must be replaced by the library manager');

  // ---- Top 02 returns to the flow editor.
  await page.getByRole('button', { name: '02 流程' }).click();
  await page.locator('.spx-flow-editor').waitFor();
  assert.equal(await page.locator('.spx-library-view').count(), 0, 'library manager heading must disappear');
  assert.equal(await page.getByRole('button', { name: /编译流程|重新编译/ }).count(), 1, 'flow compile action must be visible again');
  assert.equal(await page.locator('.spx-flow-steps').count(), 1, 'flow step table must be visible again');

  // ---- 01–06 navigation closes the manager: back on 02 you get the editor.
  await clickLibraryCategory('材料');
  await page.locator('.spx-library-view').waitFor();
  await page.getByRole('button', { name: '03 运行' }).click();
  await page.getByRole('button', { name: '02 流程' }).click();
  await page.locator('.spx-flow-editor').waitFor();
  assert.equal(await page.locator('.spx-library-view').count(), 0, 'leaving 02 must close the manager for the next visit');

  // ---- Global error banner: × closes it and the page stays usable.
  await page.getByRole('button', { name: '01 工程' }).click();
  await page.locator('input[type=file]').nth(1).setInputFiles(invalidProjectPath);
  const errorBanner = page.locator('.wf-error.spx-error');
  await errorBanner.waitFor();
  assert.match(await errorBanner.innerText(), /无法解析/);
  const bannerMetrics = await page.evaluate(() => {
    const banner = document.querySelector('.wf-error.spx-error');
    const text = banner?.querySelector('.spx-banner-text');
    if (!banner || !text) return null;
    text.textContent = '后端校验失败：'.repeat(200);
    return {
      bannerHeight: banner.getBoundingClientRect().height,
      textClientHeight: text.clientHeight,
      textScrollHeight: text.scrollHeight,
    };
  });
  assert.ok(bannerMetrics, 'error banner must expose a text node');
  assert.ok(bannerMetrics.bannerHeight <= 100,
    `a long error must not take over the page (${bannerMetrics.bannerHeight}px)`);
  assert.ok(bannerMetrics.textScrollHeight > bannerMetrics.textClientHeight,
    'long error text must scroll inside the banner');
  const errorClose = errorBanner.getByRole('button', { name: '关闭提示' });
  assert.equal(await errorClose.count(), 1);
  await errorClose.focus();
  assert.equal(await page.evaluate(() => document.activeElement?.getAttribute('aria-label')), '关闭提示', '× must be keyboard focusable');
  await page.screenshot({ path: screenshot.replace(/\.png$/, '-error.png'), fullPage: true });
  await errorClose.click();
  await errorBanner.waitFor({ state: 'detached' });
  await page.getByRole('button', { name: '02 流程' }).click();
  await page.locator('.spx-flow-editor').waitFor();

  await page.screenshot({ path: screenshot, fullPage: true });
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ status: 'passed', baseUrl, screenshot, errors }));
} finally {
  rmSync(invalidProjectPath, { force: true });
  await browser.close();
}
