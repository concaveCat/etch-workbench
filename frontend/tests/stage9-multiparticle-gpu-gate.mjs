/** Real Chromium check that Stage 9 GPU exposure closes for an unmeasured particle set. */
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const evidence = path.join(root, 'docs/stage9-evidence');
const projectPath = path.join(evidence, 'browser-multiparticle/2d/project.json');
const outputPath = path.join(evidence, 'browser-multiparticle/gpu-gate-live.json');
const baseUrl = (process.env.WORKBENCH_URL ?? 'http://127.0.0.1:8786').replace(/\/$/, '');
const project = JSON.parse(await fs.readFile(projectPath, 'utf8'));
project.name = `Stage 9 GPU gate browser ${Date.now()}`;
project.solver.enginePreference = 'gpu';
const projectInput = path.join(evidence, 'browser-multiparticle/gpu-gate-input.json');
await fs.writeFile(projectInput, `${JSON.stringify(project, null, 2)}\n`);

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
const page = await context.newPage();
page.setDefaultTimeout(20000);
const pageErrors = [];
page.on('pageerror', error => pageErrors.push(error.message));
try {
  await page.goto(`${baseUrl}/`);
  await page.getByText('本机服务已连接').waitFor();
  await page.getByRole('navigation', { name: '项目操作' }).locator('input[type="file"]').setInputFiles(projectInput);
  await page.waitForFunction(name => document.querySelector('input[aria-label="项目名称"]')?.value === name, project.name);
  const gpuOption = page.getByLabel('引擎偏好', { exact: true }).locator('option[value="gpu"]');
  await gpuOption.waitFor({ state: 'attached' });
  assert.equal(await gpuOption.isDisabled(), false, 'the directly measured 1-ion + 1-neutral configuration should expose GPU');

  await page.locator('.step-card').first().locator('.step-main').click();
  const ionLabel = await page.getByLabel('粒子标签 1').inputValue();
  await page.getByRole('button', { name: `移除粒子 ${ionLabel}`, exact: true }).click();
  await page.waitForFunction(() => {
    const option = document.querySelector('select[aria-label="引擎偏好"] option[value="gpu"]');
    return option?.disabled === true;
  });
  assert.equal(await page.getByLabel('粒子标签 1').inputValue(), 'FluorineNeutralUI');
  assert.equal(await page.locator('.multi-species-card').count(), 1);
  assert.match(await page.locator('.capability-note').innerText(), /尚未全部核实/);

  const addIon = page.getByRole('button', { name: '＋ 添加离子', exact: true });
  for (let index = 0; index < 7; index += 1) await addIon.click();
  assert.equal(await page.locator('.multi-species-card').count(), 8);
  await page.getByRole('button', { name: '移除粒子 FluorineNeutralUI', exact: true }).click();
  await addIon.click();
  const finalSpecies = await page.locator('.multi-species-card legend').allTextContents();
  assert.equal(finalSpecies.length, 8);
  assert.ok(finalSpecies.every(label => label.startsWith('离子 · ')), finalSpecies.join(', '));
  assert.equal(await gpuOption.evaluate(option => option.disabled), true,
    'an unmeasured 8-ion configuration must remain outside the GPU matrix');
  assert.deepEqual(pageErrors, []);

  const result = { status: 'passed', browser: 'Chromium', baseUrl, schemaVersion: 7,
    baselineGpuEnabled: true,
    changedConfigurations: [
      { speciesKinds: ['neutral'], speciesCount: 1 },
      { speciesKinds: Array(8).fill('ion'), speciesCount: 8 },
    ],
    gpuDisabledAfterSpeciesChanges: true, pageErrors };
  await fs.writeFile(outputPath, `${JSON.stringify(result, null, 2)}\n`);
  console.log(JSON.stringify({ ...result, evidencePath: outputPath }, null, 2));
} finally {
  await context.close();
  await browser.close();
  await fs.unlink(projectInput).catch(() => {});
}
