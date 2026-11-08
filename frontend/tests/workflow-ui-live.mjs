/** Real Chromium acceptance of the redesigned Workflow Studio. */
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const baseUrl = (process.env.WORKBENCH_URL ?? 'http://127.0.0.1:8812').replace(/\/$/, '');
const projectPath = process.env.WORKFLOW_PROJECT ?? path.join(path.dirname(fileURLToPath(import.meta.url)), 'fixtures', 'workflow-2d-trench.json');
const screenshot = process.env.WORKFLOW_SCREENSHOT ?? '/tmp/workflow-studio.png';

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
page.setDefaultTimeout(30000);
const errors = [];
page.on('pageerror', error => errors.push(error.message));

try {
  await page.goto(`${baseUrl}/`);
  await page.getByText('Etch Workbench').first().waitFor();
  await page.locator('input[type=file]').nth(1).setInputFiles(projectPath);
  await page.getByRole('button', { name: '去 02 定义流程' }).click();
  await page.getByRole('button', { name: '编译流程' }).click();
  await page.getByText(/编译成功/).waitFor();
  await page.getByRole('button', { name: /^(创建 Route|去 03)$/ }).click();
  await page.locator('.wf-status.planned').first().waitFor();
  await page.getByRole('button', { name: '执行当前 Segment' }).click();
  await page.locator('.wf-status.succeeded').first().waitFor({ timeout: 30000 });

  await page.getByRole('button', { name: '测量', exact: true }).click();
  await page.getByText(/测量 · 已测量/).waitFor({ timeout: 30000 });
  await page.getByText(/2\.000 µm/).first().waitFor({ timeout: 30000 });
  await page.getByText(/绑定 \(binding\)：已绑定/).waitFor({ timeout: 30000 });
  await page.screenshot({ path: screenshot, fullPage: true });

  await page.getByRole('button', { name: '传统工作区' }).click();
  await page.locator('.activity-rail').waitFor({ timeout: 15000 });
  await page.getByRole('button', { name: '返回 Etch Workbench' }).click();
  await page.getByText('Etch Workbench').first().waitFor({ timeout: 15000 });
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ status: 'passed', baseUrl, screenshot, errors }));
} finally {
  await browser.close();
}
