/**
 * Round-4 review acceptance (blocking + high items).
 *
 * N1 measurement ambiguity is consistent across 03/04/05/report:
 *    a 2D GDS project whose clip maps both layers to Si measures "ambiguous";
 *    03 shows no depth, 04 separates run success from metric validity, 05 keeps
 *    numbers and reasons in different columns, the report says 未记录 + 说明.
 * N2 a continued Segment can be run from the UI without an English backend error.
 * N3 the 2D report SVG has filled material polygons and a legend of drawn
 *    materials only; the 3D report SVG is an isometric projection.
 * N9 the report shows the requested seed when actualSeed is null.
 * N4 unsaved edits trigger the native beforeunload prompt.
 * N5 switching projects drops the previous project's experiment/comparison/report.
 * N8 reopening a GDS project shows its file name and registered Layout.
 */
import assert from 'node:assert/strict';
import { mkdirSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';
import { executionCompileBasis } from '../src/workflow/fingerprint.ts';

const here = path.dirname(fileURLToPath(import.meta.url));
const baseUrl = (process.env.WORKBENCH_URL ?? 'http://127.0.0.1:8912').replace(/\/$/, '');
const layoutPath = process.env.WORKFLOW_LAYOUT
  ?? path.resolve(here, '../../docs/stage5-evidence/two-structure-two-layer.gds');
const projectPath = process.env.WORKFLOW_PROJECT ?? path.join(here, 'fixtures', 'workflow-2d-five.json');
const project3dPath = process.env.WORKFLOW_PROJECT_3D ?? path.join(here, 'fixtures', 'workflow-3d-plane.json');
const shotDir = process.env.WORKFLOW_SHOT_DIR ?? path.resolve(here, '../../var/review-shots');
mkdirSync(shotDir, { recursive: true });

const getJson = async endpoint => {
  const response = await fetch(`${baseUrl}${endpoint}`);
  assert.ok(response.ok, `${endpoint} -> ${response.status}`);
  return response.json();
};
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
const putJson = async (endpoint, body) => {
  const response = await fetch(`${baseUrl}${endpoint}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const text = await response.text();
  assert.ok(response.ok, `${endpoint} -> ${response.status} ${text}`);
  return JSON.parse(text);
};
const waitSegment = async segmentId => {
  for (let index = 0; index < 600; index += 1) {
    const segment = await getJson(`/api/v1/workflow/segments/${segmentId}`);
    if (['succeeded', 'failed', 'cancelled', 'interrupted'].includes(segment.status)) return segment;
    await new Promise(resolve => setTimeout(resolve, 500));
  }
  throw new Error(`segment ${segmentId} did not finish`);
};
const waitExperiment = async experimentId => {
  for (let index = 0; index < 1200; index += 1) {
    const document = await getJson(`/api/v1/workflow/experiments/${experimentId}`);
    if (['completed', 'failed'].includes(document.status)) return document;
    await new Promise(resolve => setTimeout(resolve, 500));
  }
  throw new Error(`experiment ${experimentId} did not finish`);
};

// Re-runs against a persistent preview data dir must not hit the 409 guard.
const fixtureName = JSON.parse(readFileSync(projectPath, 'utf8')).name;
const managedNames = new Set([fixtureName, `${fixtureName} · 无前序`, 'Round4 空白工程', 'Round4 3D 报告']);
for (const row of (await getJson('/api/v1/workflow/projects')).projects ?? []) {
  if (managedNames.has(row.name)) {
    await fetch(`${baseUrl}/api/v1/workflow/projects/${row.projectId}`, { method: 'DELETE' });
  }
}

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
  name: 'round4 clip',
  structure: structure.name,
  layers: structure.layerDatatypes.map(([layer, datatype]) => ({ layer, datatype, material: 'Si' })),
  bbox: structure.bounds,
  annotations: [],
});

const fixture = JSON.parse(readFileSync(projectPath, 'utf8'));
fixture.name = fixtureName;
const gdsGeometry = {
  kind: 'gds', assetId: clip.gdsAssetId, fileName: clip.gdsFileName,
  structure: 'CLIP', unitScale: 1, position: [0, 0, 0],
  layers: clip.layers.map(item => ({ layer: item.layer, material: item.material })),
};
const compiled = await postJson(`/api/v1/workflow/clips/${clip.clipId}/compile`, { project: fixture });
const steps = compiled.compile.project.steps.filter(step => step.enabled);
// Two segments: the frontend splits after the second-to-last step.
const splitAfterStepId = steps.length > 1 ? steps[steps.length - 2].id : undefined;
const routeResponse = await postJson('/api/v1/workflow/routes/from-compile', {
  compileId: compiled.compileId, name: `${fixtureName} · Route`, splitAfterStepId,
});
const routePlan = routeResponse.plan;
assert.equal(routePlan.segments.length, 2, 'the fixture route needs two segments');

// First segment runs so the ambiguity has a real snapshot.
const first = routePlan.segments[0];
const executedFirst = await postJson(`/api/v1/workflow/segments/${first.segmentId}/execute`, {});
const firstDone = await waitSegment(executedFirst.segment.segmentId);
assert.equal(firstDone.status, 'succeeded', 'first segment must succeed');

await postJson('/api/v1/workflow/metrics', {
  schemaVersion: 1, metricId: 'metric_round4_depth', name: 'depth', unit: 'um',
});
const recipe = await postJson('/api/v1/workflow/metrology-recipes', {
  schemaVersion: 1, recipeId: 'recipe_round4_depth', version: 1, metricId: 'metric_round4_depth',
  name: 'round4 depth', dimension: 2, method: 'etch_depth',
  parameters: { kind: 'etch_depth', x: 0, referenceHeight: 0 }, material: 'Si',
});
const ambiguous = await postJson('/api/v1/workflow/measurements/from-run', {
  recipeId: recipe.recipeId, recipeVersion: recipe.version,
  runId: firstDone.runId, stepId: firstDone.throughStepId,
});
assert.equal(ambiguous.status, 'ambiguous', 'the GDS clip must measure ambiguously at x=0');
assert.equal(ambiguous.value, null);
const boundMeasurement = await postJson(`/api/v1/workflow/measurements/${ambiguous.measurementId}/bind`, {
  segmentId: first.segmentId, runId: firstDone.runId, stepId: firstDone.throughStepId,
});

const experiment = await postJson('/api/v1/workflow/experiments', {
  name: 'Round4 歧义实验',
  project: { ...fixture, geometry: gdsGeometry },
  flowId: compiled.flowId,
  compileId: compiled.compileId,
  recipeId: recipe.recipeId,
  recipeVersion: recipe.version,
  variants: [0.01, 0.02, 0.03].map((duration, index) => ({
    parameters: { [steps[0].id]: { duration } },
    replicates: 2,
    seed: 700 + index * 10,
  })),
  maxAttempts: 1,
  costPerRunUsd: 0,
});
await postJson(`/api/v1/workflow/experiments/${experiment.experimentId}/run`, {});
const finished = await waitExperiment(experiment.experimentId);
assert.equal(finished.status, 'completed', 'all six replicates must run');
const replicateCount = finished.variants.reduce((total, variant) => total + variant.replicates.length, 0);
assert.equal(replicateCount, 6);
for (const variant of finished.variants) {
  for (const replicate of variant.replicates) {
    assert.equal(replicate.status, 'succeeded', 'run success is separate from metric validity');
    assert.equal(replicate.value, null, 'ambiguous metrics carry no value');
    assert.equal(replicate.measurementStatus, 'ambiguous');
  }
}

const projectA = await postJson('/api/v1/workflow/projects', {
  ...fixture, geometry: gdsGeometry, domain: compiled.compile.project.domain,
});
const projectADoc = await getJson(`/api/v1/workflow/projects/${projectA.projectId}`);
await putJson(`/api/v1/workflow/projects/${projectA.projectId}/state`, {
  schemaVersion: 1,
  compileId: compiled.compileId,
  compileBasis: executionCompileBasis(projectADoc),
  routeId: routePlan.route.routeId,
  segmentRuns: [{ segmentId: first.segmentId, runId: firstDone.runId, measurementId: boundMeasurement.measurementId }],
  measurementIds: [boundMeasurement.measurementId],
  experimentIds: [experiment.experimentId],
});

// A second project with a two-segment route and no runs: reopening selects the
// continued segment, which is exactly the N2 scenario.
const routeB = await postJson('/api/v1/workflow/routes/from-compile', {
  compileId: compiled.compileId, name: `${fixtureName} · 无前序`, splitAfterStepId,
});
const projectB = await postJson('/api/v1/workflow/projects', {
  ...fixture, name: `${fixtureName} · 无前序`, geometry: gdsGeometry, domain: compiled.compile.project.domain,
});
const projectBDoc = await getJson(`/api/v1/workflow/projects/${projectB.projectId}`);
await putJson(`/api/v1/workflow/projects/${projectB.projectId}/state`, {
  schemaVersion: 1, compileId: compiled.compileId,
  compileBasis: executionCompileBasis(projectBDoc), routeId: routeB.plan.route.routeId,
  segmentRuns: [], measurementIds: [], experimentIds: [],
});

// A third project: plain 2D, used to prove a project switch clears 04/05/06.
const projectC = await postJson('/api/v1/workflow/projects', { ...fixture, name: 'Round4 空白工程' });

// A 3D project for the report isometric projection.
const fixture3d = JSON.parse(readFileSync(project3dPath, 'utf8'));
fixture3d.name = 'Round4 3D 报告';
// A hole (not a flat plane) so the isometric projection shows real morphology.
fixture3d.geometry = {
  kind: 'hole', radius: 1, depth: 1.2, taperAngle: 0,
  maskHeight: 0, maskTaperAngle: 0, substrateMaterial: 'Si', maskMaterial: 'Mask',
};
const compiled3d = await postJson('/api/v1/workflow/flows/from-project', { project: fixture3d });
const route3d = await postJson('/api/v1/workflow/routes/from-compile', {
  compileId: compiled3d.compileId, name: 'Round4 3D Route',
});
const run3d = await postJson(`/api/v1/workflow/segments/${route3d.plan.segments[0].segmentId}/execute`, {});
const done3d = await waitSegment(run3d.segment.segmentId);
assert.equal(done3d.status, 'succeeded');
const project3d = await postJson('/api/v1/workflow/projects', { ...fixture3d });
const project3dDoc = await getJson(`/api/v1/workflow/projects/${project3d.projectId}`);
await putJson(`/api/v1/workflow/projects/${project3d.projectId}/state`, {
  schemaVersion: 1, compileId: compiled3d.compileId,
  compileBasis: executionCompileBasis(project3dDoc), routeId: route3d.plan.route.routeId,
  segmentRuns: [{ segmentId: done3d.segmentId, runId: done3d.runId, measurementId: null }],
  measurementIds: [], experimentIds: [],
});

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const page = await browser.newPage({ viewport: { width: 1600, height: 1500 } });
page.setDefaultTimeout(60000);
const errors = [];
const dialogs = [];
page.on('pageerror', error => errors.push(error.message));
page.on('dialog', async dialog => {
  dialogs.push(dialog.type());
  await dialog.accept();
});

const openZone = async name => {
  await page.getByRole('button', { name, exact: true }).click();
};
const openProject = async name => {
  if (await page.locator('.spx-command').count() === 0) {
    await page.getByRole('button', { name: '文件', exact: true }).click();
    await page.getByRole('menuitem', { name: '关闭工程 / 返回开始页' }).click();
    await page.getByRole('button', { name: '01 工程', exact: true }).click();
    await page.locator('[aria-label="最近工程列表"]').waitFor();
  }
  const open = page.getByRole('button', { name: `打开工程 ${name}`, exact: true });
  await open.waitFor({ timeout: 30000 });
  await open.click();
  await page.locator('.spx-tree-root').filter({ hasText: name }).waitFor({ timeout: 30000 });
};

try {
  await page.goto(`${baseUrl}/`);

  // ---- Round-4 close-out: Chinese chrome, entry cards and the start page --
  const menubarText = await page.locator('.spx-menubar').innerText();
  for (const label of ['文件', '编辑', '视图', '工程', '运行', '工具', '帮助']) {
    assert.ok(menubarText.includes(label), `top menu must be Chinese, missing ${label}: ${menubarText}`);
  }
  assert.doesNotMatch(menubarText, /\b(File|Edit|View|Project|Run|Tools|Help)\b/);
  assert.equal(await page.locator('.spx-command').count(), 3, 'the start page shows the three entry cards');
  assert.equal((await page.locator('.spx-doc-state').innerText()).match(/未打开工程/g)?.length ?? 0, 0,
    'the empty project chip is hidden');
  assert.match(await page.locator('.spx-tree-root').innerText(), /未打开工程/, 'the left tree keeps the empty-project state');
  assert.equal(await page.locator('.spx-save-button').count(), 0, 'no save pill without a project');
  assert.equal(await page.locator('.spx-sheet-title').filter({ hasText: '最近工程' }).count(), 1);
  assert.equal(await page.locator('.spx-sheet-title').filter({ hasText: '示例工程' }).count(), 1);
  assert.equal(await page.locator('#spx-examples .spx-sample').count(), 9, 'all nine ViennaPS examples live in their own block');
  assert.equal(await page.locator('#spx-examples .spx-sample[data-example-id="trench-deposition"]').count(), 1);
  assert.equal(await page.locator('#spx-examples .spx-sample[data-example-id="gds-mask-import"]').count(), 1);
  await page.locator('.spx-command').filter({ hasText: '新建工程' }).click();
  const newForm = await page.locator('.spx-sheet').filter({ hasText: '新建工程' }).last().innerText();
  assert.doesNotMatch(newForm, /Domain & Grid|Solver|Engine|Rays per point|CPU threads|Plane height|X extent|gridDelta|Timeout \(s\)|\bSeed\b/, newForm);
  for (const label of ['计算域与网格', '求解器', '引擎', '每点射线数', 'CPU 线程', '网格步长', 'X 范围', '随机种子', '超时（s）']) {
    assert.ok(newForm.includes(label), `01 form must label ${label}: ${newForm}`);
  }
  await page.getByRole('button', { name: '取消', exact: true }).click();

  // ---- N1: project A restores with the ambiguous measurement --------------
  await openProject(fixtureName);
  assert.equal(await page.locator('.spx-command').count(), 0, 'entry cards hide while a project is open');
  assert.equal(await page.locator('.spx-save-button').count(), 1, 'the save pill appears with a project');
  const projectStatusText = await page.locator('.spx-project-status').innerText();
  assert.doesNotMatch(projectStatusText, /proj_/, `the project header must not show the raw id: ${projectStatusText}`);
  assert.match(await page.locator('.spx-project-status').getAttribute('title') ?? '', /proj_/, 'the project id stays in title');
  await openZone('03 运行');
  await page.locator('.wf-summary[aria-label="当前测量"]').waitFor({ timeout: 30000 });
  const measurementSummary = await page.locator('.wf-summary[aria-label="当前测量"]').innerText();
  assert.match(measurementSummary, /测量有歧义/, measurementSummary);
  assert.match(measurementSummary, /测量位置 X 处有多条材料界面相交/, measurementSummary);
  assert.doesNotMatch(measurementSummary, /0\.9|µm/, `an ambiguous measurement must not show a depth: ${measurementSummary}`);
  assert.doesNotMatch(measurementSummary, /multiple selected-material surfaces/, 'the raw English reason must not be shown');
  const structureNote = await page.locator('.wf-structure-note').first().textContent();
  assert.match(structureNote, /测量无有效值/, structureNote);
  assert.doesNotMatch(structureNote, /深度 \d/, `the structure note must not claim a depth: ${structureNote}`);
  const markerLabel = await page.locator('.wf-marker text').first().textContent();
  assert.match(markerLabel, /测量位置（无有效值）/, markerLabel);
  await page.screenshot({ path: path.join(shotDir, 'round4-03-ambiguous.png'), fullPage: true });
  const statusRow = await page.locator('.wf-status-row').innerText();
  assert.match(statusRow, /第 \d+ 段 · 成功/, `03 names the segment instead of the raw id: ${statusRow}`);
  assert.match(statusRow, /第 \d+ 段 · 测量有歧义/, `03 separates the measurement row: ${statusRow}`);

  // ---- N1: 04 separates run success from metric validity ------------------
  await openZone('04 实验');
  await page.locator('[aria-label="实验指标汇总"]').waitFor({ timeout: 30000 });
  const metricSummary = await page.locator('[aria-label="实验指标汇总"]').innerText();
  assert.match(metricSummary, /6\/6 运行成功 · 0\/6 有有效指标/, metricSummary);
  assert.match(metricSummary, /没有有效指标/, metricSummary);
  const resultTable = await page.locator('.wf-result-table').innerText();
  assert.match(resultTable, /测量有歧义/, resultTable);
  assert.match(resultTable, /说明：测量位置 X 处有多条材料界面相交/, resultTable);
  assert.doesNotMatch(resultTable, /multiple selected-material surfaces/, 'English reasons stay out of the table');
  const variantInputs = page.locator('details.wf-experiment-inputs');
  if (!await variantInputs.evaluate(element => element.open)) await variantInputs.locator('summary').click();
  const firstDuration = await page.getByLabel('变体 1 duration').inputValue();
  await page.getByLabel('变体 2 duration').fill(firstDuration);
  const duplicateRow = page.locator('.wf-variant-table tr.has-error');
  await duplicateRow.waitFor();
  assert.match(await duplicateRow.innerText(), /与 V1 重复/);
  assert.equal(await page.getByRole('button', { name: '创建实验' }).isDisabled(), true,
    'a duplicate variant duration blocks experiment creation');
  await page.getByLabel('变体 2 duration').fill('0.02');
  await page.waitForTimeout(150);
  assert.equal(await page.locator('.wf-variant-table tr.has-error').count(), 0);
  await page.screenshot({ path: path.join(shotDir, 'round4-04-metric-invalid.png'), fullPage: true });

  // ---- N1: 05 keeps numbers and reasons in separate columns ---------------
  await openZone('05 分析');
  const metricTable = page.locator('[aria-label="测量指标表"]');
  await metricTable.waitFor();
  const headers = await metricTable.locator('thead').innerText();
  for (const header of ['状态', '数值', '单位', '说明']) assert.ok(headers.includes(header), headers);
  const rows = await metricTable.locator('tbody tr').allInnerTexts();
  assert.ok(rows.length >= 1);
  for (const row of rows) {
    const cells = row.split('\t');
    assert.match(cells[4], /^(—|\d)/, `numeric column must be a number or —: ${row}`);
    assert.doesNotMatch(cells[5], /µm/, `unit stays empty without a value: ${row}`);
  }
  assert.match(rows[0], /测量有歧义/);
  assert.match(rows[0], /测量位置 X 处有多条材料界面相交/);
  const analysisAdvanced = page.locator('details.wf-analysis-advanced');
  if (!await analysisAdvanced.evaluate(element => element.open)) await analysisAdvanced.locator('summary').first().click();
  const csvFormat = page.locator('.wf-csv-format');
  if (!await csvFormat.evaluate(element => element.open)) await csvFormat.locator('summary').click();
  const csvText = await csvFormat.innerText();
  for (const column of ['measured', 'simulated', 'weight(可选)', 'label(可选)', '0.0120']) {
    assert.ok(csvText.includes(column), `CSV format reference must document ${column}: ${csvText}`);
  }
  const analysisText = await page.locator('.wf-card.wf-wide').innerText();
  assert.doesNotMatch(analysisText, /Choose File|No file chosen/, 'no native English file input');
  assert.ok(analysisText.includes('选择文件'));
  assert.ok(analysisText.includes('未选择文件'));
  await page.getByLabel('窗口下限').fill('0.5');
  await page.getByLabel('窗口上限').fill('0.1');
  const windowError = page.locator('[aria-label="窗口校验错误"]');
  await windowError.waitFor();
  assert.match(await windowError.innerText(), /窗口下限必须小于上限/);
  await page.getByLabel('窗口下限').fill('');
  await page.getByLabel('窗口上限').fill('');
  await page.screenshot({ path: path.join(shotDir, 'round4-05-csv-window.png'), fullPage: true });

  // ---- N1/N3/N9: report ---------------------------------------------------
  await openZone('06 报告');
  await page.getByRole('button', { name: '生成报告' }).click();
  await page.locator('.wf-report-preview').waitFor({ timeout: 60000 });
  const reportFrame = page.frameLocator('.wf-report-preview');
  const reportText = await reportFrame.locator('body').innerText();
  assert.match(reportText, /数值 未记录/, reportText);
  assert.match(reportText, /测量有歧义/, reportText);
  assert.match(reportText, /说明/, reportText);
  assert.doesNotMatch(reportText, /未记录 um/, 'no unit may follow a missing value');
  assert.doesNotMatch(reportText, /multiple selected-material surfaces/, 'the report shows Chinese reasons');
  assert.match(reportText, /种子（请求值）/);
  assert.match(reportText, /种子策略/);
  assert.match(reportText, /生成时间 \d{4}-\d{2}-\d{2} \d{2}:\d{2} \S+/, reportText);
  assert.doesNotMatch(reportText, /生成时间[^\n]*\d{4}-\d{2}-\d{2}T/, 'report time must not be ISO UTC');
  assert.doesNotMatch(reportText, /shared-run-seed|not-applicable/, 'raw tokens stay out of the report body');
  assert.match(reportText, /共享运行种子/);
  const reportHtml = await page.locator('.wf-report-preview').evaluate(frame => frame.contentDocument.documentElement.outerHTML);
  assert.match(reportHtml, /种子（请求值）<\/th><td>7</, 'fixed seed 7 must be shown');
  assert.ok((reportHtml.match(/<polygon/g) ?? []).length > 0, '2D report SVG must fill materials');
  assert.match(reportHtml, /材料图例/);
  assert.doesNotMatch(reportHtml, /SiO2/, 'the legend must not list materials that were not drawn');
  await page.screenshot({ path: path.join(shotDir, 'round4-06-report-2d.png'), fullPage: true });
  await page.locator('.wf-report-preview').evaluate(frame => {
    const heading = [...frame.contentDocument.querySelectorAll('h2')].find(node => node.textContent.includes('结构视图'));
    heading?.scrollIntoView({ block: 'start' });
  });
  await page.waitForTimeout(400);
  await page.locator('.wf-report-preview').screenshot({ path: path.join(shotDir, 'round4-06-report-2d-structure.png') });

  // ---- Library breadcrumb + Output log -----------------------------------
  await page.locator('.spx-left .spx-tree-item').filter({ hasText: '材料' }).first().click();
  await page.locator('.spx-library-view').waitFor();
  const breadcrumb = await page.locator('.spx-breadcrumb').innerText();
  assert.match(breadcrumb, /02 流程/);
  assert.match(breadcrumb, /材料库/);
  const libraryHead = await page.locator('.spx-library-category-head').first().innerText();
  assert.match(libraryHead, /材料库 · \d+ 条/, `one separator only: ${libraryHead}`);
  assert.doesNotMatch(libraryHead, /材料库 · ·/);
  await page.screenshot({ path: path.join(shotDir, 'round4-02-library-breadcrumb.png'), fullPage: true });
  await page.locator('.spx-breadcrumb').getByRole('button', { name: '返回流程' }).click();
  await page.locator('.spx-library-view').waitFor({ state: 'detached' });
  await page.getByRole('button', { name: /^(重新编译|编译流程)$/ }).click();
  await page.locator('.spx-log').filter({ hasText: '已编译流程' }).waitFor({ timeout: 60000 });
  const outputText = await page.locator('.spx-output-body').innerText();
  assert.doesNotMatch(outputText, /project-delete|compile-flow|library-insert/, outputText);
  assert.doesNotMatch(outputText, /[a-z]+-[a-z]+ …/, 'the log must not print internal handler ids');

  // ---- N8: reopening the GDS project keeps its file/layout ---------------
  await openZone('01 工程');
  await page.getByRole('button', { name: 'GDS / Layout' }).click();
  await page.locator('.spx-file-name').filter({ hasText: 'two-structure-two-layer.gds' }).first().waitFor({ timeout: 30000 });
  assert.match(await page.locator('.spx-sheet').filter({ hasText: '工程定义' }).innerText(), /来自已注册 Layout/);
  const registered = page.getByRole('button', { name: '已注册 Layout' });
  await registered.waitFor();
  assert.equal(await registered.isDisabled(), true, 'an existing Layout keeps the register button disabled');
  await page.screenshot({ path: path.join(shotDir, 'round4-01-gds-reopen.png'), fullPage: true });

  // ---- N2: the continued Segment runs its predecessors --------------------
  await openProject(`${fixtureName} · 无前序`);
  await openZone('03 运行');
  const chainButton = page.getByRole('button', { name: '执行前序并运行', exact: true });
  await chainButton.waitFor({ timeout: 30000 });
  assert.match(await page.locator('.wf-run').innerText(), /前序 1 个 Segment 尚未成功/);
  await chainButton.click();
  await page.locator('.wf-status-row .wf-status.succeeded').waitFor({ timeout: 180000 });
  await page.waitForTimeout(1000);
  const runText = await page.locator('.wf-run').innerText();
  assert.equal(await page.locator('.wf-error').count(), 0, 'no backend error banner after the chain');
  assert.doesNotMatch(runText, /continued Segment has no successful predecessor checkpoint/);
  assert.doesNotMatch(runText, /predecessor checkpoint/);
  assert.ok(await page.locator('.wf-step-chip').count() >= 2, 'the structure timeline updates after the chain');

  // ---- N5: switching projects clears 04/05/06 -----------------------------
  await openProject('Round4 空白工程');
  const treeText = await page.locator('.spx-tree').first().innerText();
  assert.match(treeText, /实验[\s\S]*?未创建/, treeText);
  assert.match(treeText, /分析[\s\S]*?未载入/, treeText);
  assert.match(treeText, /报告[\s\S]*?未生成/, treeText);
  await openZone('04 实验');
  assert.equal(await page.locator('[aria-label="实验列表"]').count(), 0, '04 must not list the previous experiment');
  assert.equal(await page.locator('.wf-result-table').count(), 0);
  await openZone('05 分析');
  assert.equal(await page.locator('[aria-label="测量指标表"]').count(), 0, '05 must not keep the previous metrics');
  const refreshCandidates = page.getByRole('button', { name: '刷新候选' });
  assert.equal(await refreshCandidates.isDisabled(), true);
  assert.match(await page.locator('[aria-label="刷新候选禁用原因"]').innerText(), /当前工程还没有已完成的实验\/运行/);
  await openZone('06 报告');
  const reportSummary = await page.locator('.wf-report-summary').innerText();
  assert.match(reportSummary, /实验[\s\S]*?未提供/, reportSummary);
  assert.match(reportSummary, /报告[\s\S]*?未生成/, reportSummary);
  assert.doesNotMatch(await page.locator('.spx-center').innerText(), /Round4 歧义实验/);
  await page.screenshot({ path: path.join(shotDir, 'round4-project-switch.png'), fullPage: true });

  // ---- N4: unsaved edits raise the native beforeunload prompt ------------
  await openZone('01 工程');
  await page.getByLabel('工程 gridDelta').fill('0.1');
  await page.waitForTimeout(300);
  dialogs.length = 0;
  await page.reload().catch(() => undefined);
  await page.waitForTimeout(1500);
  assert.ok(dialogs.includes('beforeunload'), `reload must ask before discarding edits: ${JSON.stringify(dialogs)}`);

  // Saving clears the dirty state, so the next reload must not ask again.
  await page.locator('.spx-tree-root').waitFor({ timeout: 30000 });
  await openZone('01 工程');
  await page.getByLabel('工程 gridDelta').fill('0.1');
  await page.keyboard.press('Control+s');
  await page.locator('.spx-save-button').filter({ hasText: '已保存' }).waitFor({ timeout: 30000 });
  await page.waitForTimeout(500);
  dialogs.length = 0;
  await page.reload().catch(() => undefined);
  await page.waitForTimeout(1200);
  assert.equal(dialogs.includes('beforeunload'), false, 'a saved project must reload without a prompt');

  // ---- N3: 3D report has an isometric projection -------------------------
  await page.locator('.spx-tree-root').waitFor({ timeout: 30000 });
  await openProject('Round4 3D 报告');
  await openZone('06 报告');
  await page.getByRole('button', { name: '生成报告' }).click();
  await page.locator('.wf-report-preview').waitFor({ timeout: 60000 });
  const report3d = await page.locator('.wf-report-preview').evaluate(frame => frame.contentDocument.documentElement.outerHTML);
  assert.match(report3d, /等轴投影/);
  assert.ok((report3d.match(/<polygon/g) ?? []).length > 0, '3D report must project triangles');
  await page.screenshot({ path: path.join(shotDir, 'round4-06-report-3d.png'), fullPage: true });
  await page.locator('.wf-report-preview').evaluate(frame => {
    const heading = [...frame.contentDocument.querySelectorAll('h2')].find(node => node.textContent.includes('结构视图'));
    heading?.scrollIntoView({ block: 'start' });
  });
  await page.waitForTimeout(400);
  await page.locator('.wf-report-preview').screenshot({ path: path.join(shotDir, 'round4-06-report-3d-structure.png') });

  assert.deepEqual(errors, []);
  console.log(JSON.stringify({
    status: 'passed', baseUrl, shotDir,
    projectA: projectA.projectId, projectB: projectB.projectId, projectC: projectC.projectId,
    experimentId: experiment.experimentId, measurementId: ambiguous.measurementId,
    dialogs,
  }));
} finally {
  await browser.close();
}
