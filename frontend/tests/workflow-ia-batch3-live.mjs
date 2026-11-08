/** IA batch 3 Chromium acceptance: 04 experiments, 05 analysis, 06 report.
 *
 * Review items covered here:
 *  - 04/05/06 empty states without a project, with real next-step buttons and no
 *    full input tables / disabled-looking primary flows;
 *  - a 3-variant experiment with 2 replicates per variant, result-first layout,
 *    every replicate visible plus mean/std and Chinese status;
 *  - 05 auto-loads the finished experiment's candidates (condition labels),
 *    Chinese headers, axes/scale/legend per candidate view;
 *  - 06 blocks generation without a run/measurement with a visible reason, then
 *    generates a report containing the structure SVG and result tables, with the
 *    title/conclusions form above the generate button.
 */
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const here = path.dirname(fileURLToPath(import.meta.url));
const baseUrl = (process.env.WORKBENCH_URL ?? 'http://127.0.0.1:8812').replace(/\/$/, '');
const projectPath = process.env.WORKFLOW_PROJECT ?? path.join(here, 'fixtures', 'workflow-2d-five.json');
const screenshot = process.env.WORKFLOW_SCREENSHOT ?? '/tmp/workflow-ia-batch3.png';

const getJson = async endpoint => {
  const response = await fetch(`${baseUrl}${endpoint}`);
  assert.ok(response.ok, `${endpoint} -> ${response.status}`);
  return response.json();
};

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const page = await browser.newPage({ viewport: { width: 1600, height: 1100 } });
page.setDefaultTimeout(60000);
const errors = [];
page.on('pageerror', error => errors.push(error.message));

try {
  await page.goto(`${baseUrl}/`);
  await page.getByText('Etch Workbench').first().waitFor();

  // ---- Empty states: no project yet, so 04/05/06 must not render their forms.
  await page.getByRole('button', { name: '04 实验', exact: true }).click();
  await page.locator('[aria-label="实验空态"]').waitFor();
  assert.match(await page.locator('[aria-label="实验空态"]').innerText(), /还不能创建实验/);
  assert.equal(await page.getByLabel('变体 1 duration').count(), 0, 'no project must not render the 10-row variant table');
  assert.equal(await page.locator('[aria-label="变体输入表"]').count(), 0);
  await page.getByRole('button', { name: '去 01 新建工程' }).waitFor();

  await page.getByRole('button', { name: '05 分析', exact: true }).click();
  await page.locator('[aria-label="分析空态"]').waitFor();
  assert.equal(await page.locator('.wf-analysis-advanced').count(), 0, 'empty analysis must not render process window / calibration');
  assert.equal(await page.getByLabel('窗口下限').count(), 0);
  await page.getByRole('button', { name: '去 04 创建实验' }).waitFor();

  await page.getByRole('button', { name: '06 报告', exact: true }).click();
  await page.locator('[aria-label="报告禁用原因"]').waitFor();
  assert.match(await page.locator('[aria-label="报告禁用原因"]').innerText(), /先完成 03 运行与测量/);
  assert.equal(await page.getByRole('button', { name: '生成报告', exact: true }).isDisabled(), true);
  assert.equal(await page.locator('iframe.wf-report-preview').count(), 0, 'no empty report preview');

  // ---- Load a project, compile and route, but do not run: 06 stays blocked.
  await page.getByRole('button', { name: '01 工程', exact: true }).click();
  await page.locator('input[type=file]').nth(1).setInputFiles(projectPath);
  await page.getByRole('button', { name: '去 02 定义流程' }).click();
  await page.getByRole('button', { name: '编译流程' }).click();
  await page.getByText(/编译成功/).waitFor();
  await page.getByRole('button', { name: '创建 Route', exact: true }).click();
  await page.getByRole('button', { name: '06 报告', exact: true }).click();
  await page.locator('[aria-label="报告禁用原因"]').waitFor();
  assert.equal(await page.getByRole('button', { name: '生成报告', exact: true }).isDisabled(), true, 'no run yet => report disabled');

  // ---- Run and measure so 04 has a pinned recipe.
  await page.getByRole('button', { name: '03 运行', exact: true }).click();
  await page.getByRole('button', { name: '执行全部 Segment' }).click();
  await page.locator('.wf-status.succeeded').first().waitFor({ timeout: 120000 });
  await page.getByRole('button', { name: '测量', exact: true }).click();
  await page.getByText(/绑定 \(binding\)：已绑定/).first().waitFor();

  // ---- 04: named 3-variant experiment, 2 replicates each.
  await page.getByRole('button', { name: '04 实验', exact: true }).click();
  await page.getByLabel('实验名称').fill('IA3 三变体实验');
  assert.equal(await page.locator('[aria-label="变体输入表"] tbody tr').count(), 3, 'default variant count is 3');
  for (const index of [1, 2, 3]) {
    await page.getByLabel(`变体 ${index} replicates`).fill('2');
  }
  const conditionSummary = await page.locator('[aria-label="变体输入表"] tbody tr').first().innerText();
  assert.match(conditionSummary, /V1 · 0\.01 s · seed 20261029 · ×2/);
  await page.getByRole('button', { name: '创建实验', exact: true }).click();
  await page.getByText('实验状态：待执行').waitFor();
  assert.equal(await page.locator('.wf-result-table tbody tr').count(), 3);
  await page.getByRole('button', { name: '运行实验', exact: true }).click();
  await page.getByText('实验状态：已完成').waitFor({ timeout: 240000 });

  // Results first: 3 rows, 2 repeats each, Chinese headers/statuses, sane precision.
  assert.equal(await page.locator('.wf-result-table tbody tr').count(), 3);
  assert.equal(await page.locator('.wf-result-table .wf-repeat').count(), 6, 'every replicate must be shown');
  const resultHeaders = await page.locator('.wf-result-table thead').innerText();
  for (const header of ['条件', '重复（运行状态 / 指标）', '均值', '标准差', '运行 / 指标']) {
    assert.ok(resultHeaders.includes(header), `result header must be Chinese: ${header}`);
  }
  const resultText = await page.locator('.wf-result-table').innerText();
  assert.doesNotMatch(resultText, /succeeded|planned|running/, 'statuses must be Chinese');
  assert.doesNotMatch(resultText, /\d\.\d{8,}/, 'numbers must not leak float noise');
  assert.match(resultText, /成功/);
  assert.match(resultHeaders, /µm/, 'result units must use the micro sign');
  assert.doesNotMatch(resultText, /\bum\b/, 'result units must not fall back to "um"');
  // 4 significant digits at most (`0.0002`, not `0.0002000001`).
  const measuredCells = [
    ...await page.locator('.wf-result-table .wf-repeat code').allInnerTexts(),
    ...await page.locator('.wf-result-table tbody tr td:nth-child(4)').allInnerTexts(),
    ...await page.locator('.wf-result-table tbody tr td:nth-child(5)').allInnerTexts(),
  ];
  for (const text of measuredCells) {
    const digits = text.replace(/[^0-9]/g, '').replace(/^0+/, '');
    assert.ok(digits.length <= 4, `result value must keep 4 significant digits: ${text}`);
  }
  // No cloud cost configured => no "$0.0000".
  const queueSummary = await page.locator('.wf-summary').filter({ hasText: '队列' }).innerText();
  assert.ok(!queueSummary.includes('$0.0000'), 'zero cost must not render as $0.0000');
  assert.match(queueSummary, /本地 CPU · 无云成本/);

  // The result table stays above the Output panel: the first three rows are
  // visible without scrolling behind it.
  const thirdRow = await page.locator('.wf-result-table tbody tr').nth(2).boundingBox();
  const outputBox = await page.locator('.spx-output').boundingBox();
  assert.ok(thirdRow && outputBox && thirdRow.y + thirdRow.height <= outputBox.y + 1,
    'the first three result rows must not be covered by the Output panel');

  const experimentSummaries = (await getJson('/api/v1/workflow/experiments')).experiments;
  const experiment = await getJson(`/api/v1/workflow/experiments/${experimentSummaries[0].experimentId}`);
  assert.equal(experiment.variants.length, 3);
  assert.equal(experiment.variants[0].replicates.length, 2);
  assert.ok(experiment.variants.every(variant => variant.replicates.every(replicate => replicate.runId)));

  // ---- 05: candidates arrive automatically with condition labels.
  await page.getByRole('button', { name: '在 05 分析' }).click();
  await page.waitForFunction(() => document.querySelectorAll('.wf-compare-canvas').length === 3, null, { timeout: 120000 });
  assert.equal(await page.locator('.wf-compare-grid figure').count(), 3);
  const candidateCount = await page.locator('.wf-compare-grid figure').count();
  const caption = await page.locator('.wf-compare-grid figcaption').first().innerText();
  assert.match(caption, /V1 · 0\.01 s · seed \d+/);
  const conditionChips = await page.locator('.wf-condition-chips').innerText();
  assert.equal(await page.locator('.wf-condition-chip').count(), 3);
  assert.match(conditionChips, /V2 · 0\.02 s · seed/);
  const compareHeaders = await page.locator('[aria-label="候选对比表"] thead').innerText();
  for (const header of ['对比', '状态', '最大点偏差', '边界偏差', '材料偏差', '原因']) {
    assert.ok(compareHeaders.includes(header), `compare header must be Chinese: ${header}`);
  }
  const compareText = await page.locator('[aria-label="候选对比表"]').innerText();
  assert.doesNotMatch(compareText, /WITHIN_TOLERANCE|OUTSIDE_TOLERANCE|NOT_EVALUATED/);
  assert.ok((await page.locator('.wf-compare-canvas .wf-axes text').count()) > 0, 'each candidate view must draw axes/scale');
  // 05 uses the same StructureCanvas: the fill must hang below the surface in
  // every candidate view, not above it.
  const candidateFillGeometry = await page.locator('.wf-compare-canvas').evaluateAll(svgs => svgs.map(svg => {
    const box = svg.viewBox.baseVal;
    const line = svg.querySelector('.wf-interface');
    const surfaceY = line
      ? Math.max(Number(line.getAttribute('y1')), Number(line.getAttribute('y2')))
      : null;
    // The plot is inset by the margins that hold the titles/ticks/scale bar.
    const axisLines = Array.from(svg.querySelectorAll('.wf-axis-line line'));
    const plotBottom = axisLines[0] ? Number(axisLines[0].getAttribute('y1')) : box.y + box.height;
    const plotTop = axisLines[1] ? Number(axisLines[1].getAttribute('y2')) : box.y;
    const polygons = Array.from(svg.querySelectorAll('polygon.wf-fill')).map(node =>
      (node.getAttribute('points') ?? '').trim().split(/\s+/).map(pair => pair.split(',').map(Number)));
    return { top: box.y, height: box.height, surfaceY, plotTop, plotBottom, polygons };
  }));
  assert.equal(candidateFillGeometry.length, 3);
  for (const check of candidateFillGeometry) {
    assert.ok(check.polygons.length >= 1, 'candidate view must fill the material');
    assert.ok(check.surfaceY !== null, 'candidate view must draw the interface line');
    const plotHeight = check.plotBottom - check.plotTop;
    assert.ok(plotHeight > 0, 'candidate plot must have a height');
    for (const polygon of check.polygons) {
      const ys = polygon.map(([, y]) => y);
      assert.ok(Math.min(...ys) >= check.surfaceY - 1e-6, 'candidate fill must start at the surface line');
      assert.ok(Math.max(...ys) >= check.plotBottom - plotHeight * 0.02, 'candidate fill must reach the plot bottom');
      assert.ok(Math.max(...ys) <= check.plotBottom + 1e-6, 'candidate fill must not pass the plot bottom');
    }
  }
  // ---- N7: candidate views keep titles/ticks/scale bar apart ------------
  const candidateLayout = await page.locator('.wf-compare-canvas').first().evaluate(svg => {
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
    const vTitle = rect('.wf-axis-title--v');
    const scale = rect('.wf-scale-label');
    const vTicks = Array.from(svg.querySelectorAll('.wf-tick-label--v')).map(node => {
      const box = node.getBoundingClientRect();
      return { left: box.left, top: box.top, right: box.right, bottom: box.bottom };
    });
    const zeros = Array.from(svg.querySelectorAll('.wf-tick-label')).filter(node => (node.textContent ?? '').trim() === '0');
    return {
      xTitle, vTitle, scale, zeros: zeros.length,
      xTitleHitsScale: overlap(xTitle, scale),
      vTitleHitsTicks: vTicks.some(tick => overlap(vTitle, tick)),
      tickColor: getComputedStyle(svg.querySelector('.wf-tick-label')).fill,
    };
  });
  assert.ok(candidateLayout.xTitle && candidateLayout.vTitle && candidateLayout.scale,
    'candidate views must keep axis titles and a scale bar');
  assert.equal(candidateLayout.xTitleHitsScale, false, 'candidate X title must not overlap the scale bar');
  assert.equal(candidateLayout.vTitleHitsTicks, false, 'candidate height title must not overlap a tick label');
  assert.equal(candidateLayout.zeros, 1, `candidate origin must be labelled once, got ${candidateLayout.zeros}`);
  assert.equal(candidateLayout.tickColor, 'rgb(38, 49, 58)', `candidate tick text must stay dark, got ${candidateLayout.tickColor}`);

  // ---- compact candidate labels share one viewBox and no extra zoom -----
  const candidateViewBoxes = await page.locator('.wf-compare-canvas')
    .evaluateAll(svgs => svgs.map(svg => svg.getAttribute('viewBox')));
  assert.equal(new Set(candidateViewBoxes).size, 1,
    `candidate views must share the synced viewBox: ${JSON.stringify(candidateViewBoxes)}`);
  assert.equal(await page.locator('.wf-compare-grid .wf-structure-zoom, .wf-compare-grid button[aria-label="放大"], .wf-compare-grid button[aria-label="重置视图"]').count(), 0,
    '05 candidate cards must not add a second set of zoom controls');
  const candidateLabelSize = await page.locator('.wf-compare-canvas').first().evaluate(svg => {
    const heights = Array.from(svg.querySelectorAll('.wf-tick-label, .wf-axis-title--x, .wf-scale-label, .wf-structure-note'))
      .map(node => node.getBoundingClientRect().height).filter(height => height > 0);
    return { maxHeight: heights.length ? Math.max(...heights) : 0, count: heights.length };
  });
  assert.ok(candidateLabelSize.count >= 4 && candidateLabelSize.maxHeight > 0 && candidateLabelSize.maxHeight <= 11,
    `candidate labels must stay compact (~8px on screen), got ${JSON.stringify(candidateLabelSize)}`);

  const legend = await page.locator('.wf-compare-legend').first().innerText();
  assert.match(legend, /Si|材料/);

  // Candidate table: readable run/step names, no raw ids in the main text.
  const candidateTable = page.locator('[aria-label="候选表"]');
  await candidateTable.waitFor();
  assert.equal(await candidateTable.locator('tbody tr').count(), 3, 'candidate table lists every candidate');
  const candidateText = await candidateTable.innerText();
  assert.match(candidateText, /V1 · 0\.01 s/);
  assert.doesNotMatch(candidateText, /(?:^|\s)(?:run_|s_|c_|exp_)[a-z0-9]{6,}/, 'candidate table must not show raw ids');
  const compareMaterials = await page.locator('[aria-label="候选对比表"] tbody td:nth-child(5)').allInnerTexts();
  assert.ok(compareMaterials.every(text => text.includes('无材料差异') || text.includes('：')), 'material delta must be readable');

  // Metric table always exists with at least one row.
  const metricTable = page.locator('[aria-label="测量指标表"]');
  await metricTable.waitFor();
  assert.ok(await metricTable.locator('tbody tr').count() >= 1, 'metric table must list the measurement');
  const metricHeaders = await metricTable.locator('thead').innerText();
  for (const header of ['Run', '步骤', '方法', '状态', '数值', '单位', '说明', '绑定状态']) {
    assert.ok(metricHeaders.includes(header), `metric header must exist: ${header}`);
  }
  assert.doesNotMatch(await metricTable.innerText(), /(?:^|\s)(?:run_|s_|c_)[a-z0-9]{6,}/, 'metric table must not show raw ids');

  assert.equal(await page.locator('.wf-analysis-advanced[open]').count(), 0, 'process window / calibration stay folded by default');

  // Process window validation stays next to the lower/upper inputs.
  await page.locator('.wf-analysis-advanced > summary').click();
  await page.getByLabel('窗口下限').fill('0.3');
  await page.getByLabel('窗口上限').fill('0.0001');
  await page.getByRole('button', { name: '计算窗口' }).click();
  const windowError = page.locator('[aria-label="窗口校验错误"]');
  await windowError.waitFor();
  assert.match(await windowError.innerText(), /下限必须小于上限/);
  const lowerBox = await page.getByLabel('窗口下限').boundingBox();
  const errorBox = await windowError.boundingBox();
  assert.ok(lowerBox && errorBox && Math.abs(errorBox.y - (lowerBox.y + lowerBox.height)) < 80,
    'window error must sit right under the inputs');
  assert.equal(await page.getByLabel('窗口下限').getAttribute('aria-invalid'), 'true');
  await page.getByLabel('窗口下限').fill('0');
  await page.getByLabel('窗口上限').fill('1');
  await page.getByRole('button', { name: '计算窗口' }).click();
  await page.locator('.wf-summary').filter({ hasText: '工艺窗口' }).waitFor();
  assert.equal(await page.locator('[aria-label="窗口校验错误"]').count(), 0, 'fixing the inputs clears the inline error');

  // ---- 06: form above the button, then a report with structure + results.
  await page.getByRole('button', { name: '06 报告', exact: true }).click();
  const titleBox = await page.getByLabel('报告标题').boundingBox();
  const conclusionsBox = await page.getByLabel('报告结论').boundingBox();
  const generateBox = await page.getByRole('button', { name: '生成报告', exact: true }).boundingBox();
  assert.ok(titleBox && conclusionsBox && generateBox, 'report form must be visible');
  assert.ok(titleBox.y < generateBox.y && conclusionsBox.y < generateBox.y, 'title/conclusions must sit above the generate button');

  await page.getByLabel('报告标题').fill('IA3 报告');
  await page.getByLabel('报告结论').fill('选择 V2 · 0.02 s，CD 在公差内。');
  await page.getByRole('button', { name: '生成报告', exact: true }).click();
  await page.locator('.wf-report-result').filter({ hasText: 'IA3 报告' }).waitFor({ timeout: 120000 });
  assert.match(await page.locator('.wf-report-result').innerText(), /已生成/);
  assert.match(await page.locator('.wf-report-conclusions').innerText(), /选择 V2/);

  const summaryText = await page.locator('[aria-label="报告内容摘要"]').innerText();
  for (const text of ['P1 2D five-step fixture', 'Route', '测量', '实验']) {
    assert.ok(summaryText.includes(text), `report summary must contain ${text}`);
  }
  assert.doesNotMatch(summaryText, /\b(flow_|c_|run_|exp_|metric_|rep_)[a-z0-9]{6,}/, 'report summary must not show raw ids');
  const frame = page.frameLocator('iframe.wf-report-preview');
  await frame.locator('body').waitFor();
  const reportText = await frame.locator('body').innerText();
  for (const text of ['IA3 报告', 'IA3 三变体实验', '选择 V2', '结论', '视图']) {
    assert.ok(reportText.includes(text), `report must contain ${text}`);
  }
  await frame.getByRole('heading', { name: '结构视图' }).waitFor();
  assert.ok((await frame.locator('svg.report-svg').count()) >= 1, 'report must embed the structure view');
  assert.match(reportText, /均值|mean/, 'report must contain the experiment result table');

  const reportRow = page.locator('.spx-tree-item').filter({ hasText: '报告' }).first();
  const treeText = await reportRow.innerText();
  assert.match(treeText, /\d{4}-\d{2}-\d{2} \d{2}:\d{2}/);
  // Round 5: the value carries "已生成 · 时间" and the badge repeats the state.
  assert.ok((treeText.match(/已生成/g) ?? []).length >= 1, 'report status is visible in the tree');
  assert.match(treeText, /已生成 · \d{4}-\d{2}-\d{2}/, 'the report row shows 已生成 · 时间');
  assert.doesNotMatch(treeText, /report_[a-f0-9]{8,}/, 'left tree must not lead with a report id');

  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('button', { name: '下载 HTML' }).click();
  const download = await downloadPromise;
  assert.equal(download.suggestedFilename(), 'IA3 报告.html');

  // ---- 04 DOE: the defaults are legal and design without a 422.
  await page.getByRole('button', { name: '04 实验', exact: true }).click();
  await page.locator('.wf-experiment-inputs > summary').click();
  assert.equal(await page.locator('.wf-doe-factor').count(), 2, 'DOE defaults to two factors');
  const factorOne = await page.getByLabel('因子 1 参数').inputValue();
  const factorTwo = await page.getByLabel('因子 2 参数').inputValue();
  assert.deepEqual([factorOne, factorTwo], ['rateMagnitude', 'duration'], 'default DOE factors must differ');
  assert.equal(await page.getByRole('button', { name: '设计 DOE' }).isDisabled(), false, 'default DOE factors must be valid');

  // A repeated (step, parameter) pair is caught inline and blocks the button.
  const firstStep = await page.getByLabel('因子 1 步骤').inputValue();
  await page.getByLabel('因子 2 步骤').selectOption(firstStep);
  await page.getByLabel('因子 2 参数').selectOption('rateMagnitude');
  const factorError = page.locator('.wf-doe-factor .wf-field-error').first();
  await factorError.waitFor();
  assert.match(await factorError.innerText(), /重复/);
  assert.equal(await page.getByRole('button', { name: '设计 DOE' }).isDisabled(), true, 'duplicate factors must disable the button');
  assert.match(await page.locator('[aria-label="DOE 禁用原因"]').innerText(), /先修正因子/);

  // Fixing the parameter re-enables the button and the design succeeds.
  await page.getByLabel('因子 2 参数').selectOption('duration');
  await page.locator('.wf-doe-factor .wf-field-error').first().waitFor({ state: 'detached' });
  assert.equal(await page.getByRole('button', { name: '设计 DOE' }).isDisabled(), false);
  await page.getByRole('button', { name: '设计 DOE' }).click();
  await page.getByText('实验状态：待执行').waitFor({ timeout: 60000 });
  assert.equal(await page.locator('.spx-log.error').count(), 0, 'default DOE must not return a 422');
  assert.ok((await page.locator('.wf-result-table tbody tr').count()) > 0, 'DOE design must produce variants');

  await page.screenshot({ path: screenshot, fullPage: true });
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({
    status: 'passed', baseUrl, screenshot, errors,
    experiment: { variants: experiment.variants.length, replicates: experiment.variants[0].replicates.length },
    candidates: candidateCount,
    doeFactors: [factorOne, factorTwo],
  }));
} finally {
  await browser.close();
}
