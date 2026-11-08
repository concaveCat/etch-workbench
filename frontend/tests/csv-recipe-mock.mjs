/**
 * Offline Chromium check of the recipe-panel CSV upload and dimension switch.
 *
 * The app is built into a temporary directory with a relative base so the real
 * source (not the prebuilt dist symlink) is exercised. All API calls are mocked;
 * this test only proves the DOM wiring:
 *   1. the recipe "上传 CSV 并添加步骤" button really opens the hidden input;
 *   2. the step editor's replace button does too;
 *   3. switching 2D -> 3D drops the incompatible csvProcess step with a notice.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { chromium } from 'playwright';
import { build } from 'vite';

const here = path.dirname(fileURLToPath(import.meta.url));
const frontend = path.resolve(here, '..');
const root = path.resolve(frontend, '..');
const outDir = await fs.mkdtemp(path.join(os.tmpdir(), 'vps-csv-ui-'));
await build({ root: frontend, base: './', logLevel: 'error', build: { outDir, emptyOutDir: true } });

const materials = JSON.parse(await fs.readFile(path.join(root, 'docs/materials-catalog.json'), 'utf8')).materials;
const asset = (assetId, fileName) => ({
  assetId, fileName, schema: 'viennaps-rate-grid-v1', canonicalFormat: 'ascii-lf-comma-decimal-v1',
  canonicalSha256: assetId, dimension: 2, columns: ['x', 'rate'], rowCount: 2,
  coordinateUnit: 'um', rateUnit: 'um/s', x: { min: -2, max: 2, count: 2 },
  rate: { min: 0.4, max: 0.5, sign: 'deposit', }, linearCompatible: true, idwCompatible: true,
  sizeBytes: 16, sourceSizeBytes: 16, sourceSha256: assetId,
});

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox', '--allow-file-access-from-files'] });
const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
const page = await context.newPage();
const errors = [];
page.on('pageerror', error => errors.push(error.message));
page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });

await page.addInitScript(({ catalog, first, second }) => {
  const json = (data, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'content-type': 'application/json' } });
  const state = { uploads: [], lastValidatedProject: null };
  window.__csvTest = state;
  window.fetch = async (input, init = {}) => {
    const rawUrl = typeof input === 'string' ? input : input.url;
    const url = new URL(rawUrl, window.location.href);
    const pathname = url.pathname;
    const method = String(init.method ?? 'GET').toUpperCase();
    if (pathname === '/api/health') return json({ status: 'ok', viennaps: '4.6.2', viennals: '5.8.5' });
    if (pathname === '/api/capabilities') return json({ capabilities: {}, resourcePolicy: { run: { maximumSteps: 20 } } });
    if (pathname === '/api/gpu-devices') return json({ status: 'unavailable', devices: [], preferredGpuDeviceId: null, reason: 'offline browser test', probe: { bounded: true } });
    if (pathname === '/api/specialized-models') return json({ schemaVersion: 1, engineVersion: '4.6.2', unitsNote: '', models: [] });
    if (pathname === '/api/materials') return json({ schemaVersion: 1, engineVersion: '4.6.2', materials: catalog });
    if (pathname === '/api/projects' && method === 'GET') return json({ projects: [] });
    if (pathname === '/api/runs' && method === 'GET') return json({ runs: [] });
    if (pathname === '/api/csv-assets' && method === 'POST') {
      const file = url.searchParams.get('file_name') ?? 'rates.csv';
      const dimension = Number(url.searchParams.get('dimension'));
      state.uploads.push({ file, dimension });
      return json(file === 'rates.csv' ? first : second, 201);
    }
    if (pathname === '/api/projects/validate' && method === 'POST') {
      const body = JSON.parse(init.body);
      state.lastValidatedProject = body;
      return json({ valid: true, warnings: [], requestedInputHash: 'browser-csv-hash', normalizedProject: body, artifactEstimate: null });
    }
    if (pathname === '/api/projects' && method === 'POST') {
      const body = JSON.parse(init.body);
      return json({ id: 'saved-csv', project: body }, 201);
    }
    return json({ detail: `unexpected offline test request ${method} ${pathname}` }, 404);
  };
}, { catalog: materials, first: asset('a'.repeat(64), 'rates.csv'), second: asset('b'.repeat(64), 'rates2.csv') });

await page.goto(pathToFileURL(path.join(outDir, 'index.html')).href);
await page.locator('nav.activity-rail').getByRole('button', { name: '配方' }).click();
const addButton = page.getByRole('button', { name: '＋ 上传 CSV 并添加步骤' });
await addButton.waitFor();

// Major 2: the recipe button must open the root-level hidden input.
const [addChooser] = await Promise.all([
  page.waitForEvent('filechooser'),
  addButton.click(),
]);
await addChooser.setFiles({ name: 'rates.csv', mimeType: 'text/csv', buffer: Buffer.from('0,0.5\n1,0.4\n') });
await page.getByText('CSV 速率表 · rates.csv · LINEAR').first().waitFor();
let uploads = await page.evaluate(() => window.__csvTest.uploads);
assert.deepEqual(uploads, [{ file: 'rates.csv', dimension: 2 }]);

// The new step is selected; open its native tab and replace the table.
await page.locator('#step-tab-native').click();
const replaceButton = page.getByRole('button', { name: '重新上传并替换速率表' });
await replaceButton.waitFor();
const [replaceChooser] = await Promise.all([
  page.waitForEvent('filechooser'),
  replaceButton.click(),
]);
await replaceChooser.setFiles({ name: 'rates2.csv', mimeType: 'text/csv', buffer: Buffer.from('0,0.6\n1,0.5\n') });
await page.getByText('CSV 速率表 · rates2.csv · LINEAR').first().waitFor();
uploads = await page.evaluate(() => window.__csvTest.uploads);
assert.equal(uploads.length, 2);
assert.deepEqual(uploads[1], { file: 'rates2.csv', dimension: 2 });

// Major 3: a dimension switch must drop the incompatible csvProcess step.
await page.locator('nav.activity-rail').getByRole('button', { name: '结构' }).click();
await page.getByRole('button', { name: '切换到 3D' }).click();
await page.getByText(/已移除 CSV 速率表步骤/).first().waitFor();
await page.locator('nav.activity-rail').getByRole('button', { name: '配方' }).click();
assert.equal(await page.getByText('CSV 速率表 · rates2.csv · LINEAR').count(), 0);
assert.equal(errors.length, 0, errors.join('\n'));

await browser.close();
await fs.rm(outDir, { recursive: true, force: true });
console.log('csv recipe DOM check passed');
