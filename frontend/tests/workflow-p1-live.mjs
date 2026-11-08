/** P1 Chromium acceptance: recipe version, template variable, branch rerun, 10-variant experiment. */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const here = path.dirname(fileURLToPath(import.meta.url));
const baseUrl = (process.env.WORKBENCH_URL ?? 'http://127.0.0.1:8814').replace(/\/$/, '');
const projectPath = process.env.WORKFLOW_PROJECT ?? path.join(here, 'fixtures', 'workflow-2d-five.json');
const screenshot = process.env.WORKFLOW_SCREENSHOT ?? '/tmp/workflow-p1.png';
const SHA256 = /^[a-f0-9]{64}$/;

const getJson = async endpoint => {
  const response = await fetch(`${baseUrl}${endpoint}`);
  assert.ok(response.ok, `${endpoint} -> ${response.status}`);
  return response.json();
};

const waitForCompile = async (predicate, label, timeout = 30000) => {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const summaries = (await getJson('/api/v1/workflow/compiles')).compiles.slice(0, 5);
    for (const summary of summaries) {
      const candidate = await getJson(`/api/v1/workflow/compiles/${summary.compileId}`);
      if (predicate(candidate)) return candidate;
    }
    await new Promise(resolve => setTimeout(resolve, 300));
  }
  throw new Error(`latest compile did not satisfy: ${label}`);
};

const authoredIds = compile => compile.sourceMap.map(entry => entry.authoredStepId);
const authoredDuration = (compile, authoredId) => {
  const entry = compile.sourceMap.find(item => item.authoredStepId === authoredId);
  const step = compile.project.steps.find(item => item.id === entry.stepId);
  return Number(step.duration);
};

const waitForSegment = async (segmentId, timeout = 90000) => {
  const deadline = Date.now() + timeout;
  let segment;
  while (Date.now() < deadline) {
    segment = await getJson(`/api/v1/workflow/segments/${segmentId}`);
    if (['succeeded', 'failed', 'cancelled', 'interrupted'].includes(segment.status)) {
      assert.equal(segment.status, 'succeeded', JSON.stringify(segment));
      return segment;
    }
    await new Promise(resolve => setTimeout(resolve, 400));
  }
  throw new Error(`segment ${segmentId} did not finish: ${JSON.stringify(segment)}`);
};

// Pre-create a cross-flow template whose authored step ids differ from the project.
const fixture = JSON.parse(readFileSync(projectPath, 'utf8'));
const crossFlowTemplateId = 'tpl_crossflow_p1';
const crossFlowSteps = fixture.steps.slice(0, 2).map(step => ({
  ...step,
  id: step.id.replace(/^p1_step_/, 'alt_p1_step_'),
  duration: '${duration}',
}));
const templateResponse = await fetch(`${baseUrl}/api/v1/workflow/templates`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    schemaVersion: 1, templateId: crossFlowTemplateId, name: 'Cross-flow template', description: '',
    steps: crossFlowSteps, variables: [{ name: 'duration', value: 0.02, unit: 's' }],
  }),
});
assert.ok([201, 409].includes(templateResponse.status), await templateResponse.text());

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const page = await browser.newPage({ viewport: { width: 1600, height: 1200 } });
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
  await page.getByRole('button', { name: /^(创建 Route|去 03)$/ }).click();
  await page.getByRole('button', { name: '执行全部 Segment' }).click();
  await page.locator('.wf-status.succeeded').first().waitFor({ timeout: 60000 });

  // Two recipe versions: v1 default, v2 after changing the reference height.
  await page.getByRole('button', { name: '测量', exact: true }).click();
  await page.getByText(/绑定 \(binding\)：已绑定/).first().waitFor();
  await page.getByLabel('参考高度').fill('0.1');
  await page.getByRole('button', { name: '测量', exact: true }).click();
  const versionTwoRow = page.locator('.wf-table tbody tr').filter({ hasText: / v2 · / });
  await versionTwoRow.waitFor();
  await versionTwoRow.getByRole('button', { name: /查看 .* 证据/ }).click();
  await page.getByText('参数', { exact: true }).waitFor();
  await page.getByText(/"referenceHeight":0\.1/).waitFor();
  const evidence = await page.locator('.wf-evidence-grid').innerText();
  assert.ok(evidence.includes('etch_depth'));
  assert.ok(evidence.includes('Si'));
  assert.ok(evidence.includes('v2'));
  assert.ok(evidence.includes('verified/binding-v1'));

  // Template with ${duration}, applied and diffed.
  await page.getByRole('button', { name: '02 流程' }).click();
  await page.getByText('模板（可选）').click();
  await page.getByRole('button', { name: '保存当前步骤为模板' }).click();
  await page.getByLabel('模板变量值').fill('0.03');
  await page.getByRole('button', { name: '替换流程' }).click();
  await page.getByRole('button', { name: '确认替换' }).click();
  await waitForCompile(
    latest => latest.sourceMap.length === 5 && authoredDuration(latest, 'p1_step_04') === 0.03,
    'replace template with two 0.03 steps',
  );
  await page.getByRole('button', { name: '比较上一版' }).click();
  await page.getByText('编译差异').waitFor();
  await page.getByText(/0\.01 → 0\.03/).first().waitFor();

  // Insert a two-step template: original steps must survive with new authored ids.
  await page.getByLabel('模板选择').selectOption(crossFlowTemplateId);
  await page.waitForTimeout(300);
  await page.getByRole('button', { name: '插入模板' }).click();
  const insertedCompile = await waitForCompile(latest => latest.sourceMap.length === 7, 'inserted seven steps');
  const insertedAuthoredIds = authoredIds(insertedCompile);
  const insertedIds = insertedAuthoredIds.slice(5);
  assert.deepEqual(
    insertedAuthoredIds.slice(0, 5),
    ['p1_step_01', 'p1_step_02', 'p1_step_03', 'p1_step_04', 'p1_step_05'],
  );
  assert.equal(new Set(insertedIds).size, 2);
  assert.ok(insertedIds.every(stepId => stepId.startsWith('tpl_')), JSON.stringify(insertedIds));
  assert.deepEqual(insertedIds.map(stepId => authoredDuration(insertedCompile, stepId)), [0.03, 0.03]);
  await page.getByRole('button', { name: '比较上一版' }).click();
  await page.getByText(/\+2 \/ -0 \/ 移动 0 \/ 修改 0/).waitFor();

  await page.getByRole('button', { name: /^(创建 Route|去 03)$/ }).click();
  await page.getByRole('button', { name: '执行全部 Segment' }).click();
  await page.locator('.wf-status.succeeded').first().waitFor({ timeout: 60000 });

  const routeSummary = (await getJson('/api/v1/workflow/routes')).routes[0];
  const routeId = routeSummary.routeId;
  const splitPlan = (await getJson(`/api/v1/workflow/routes/${routeId}`)).plan;
  assert.equal(splitPlan.segments.length, 2, 'route must be split into prefix and tail');
  const prefixBefore = splitPlan.segments[0];
  assert.match(prefixBefore.resultDigest ?? '', SHA256, 'prefix resultDigest must be materialized');
  const tailStepId = splitPlan.stepOrder[splitPlan.stepOrder.length - 1];
  const prefixStepId = splitPlan.stepOrder[splitPlan.stepOrder.length - 2];
  const splitCompile = await getJson(`/api/v1/workflow/compiles/${splitPlan.segments[0].sourceCompileId}`);
  const authoredByStep = new Map(splitCompile.sourceMap.map(entry => [entry.stepId, entry.authoredStepId]));
  assert.deepEqual(
    [...authoredByStep.values()],
    [
      'p1_step_01', 'p1_step_02', 'p1_step_03', 'p1_step_04', 'p1_step_05',
      ...insertedIds,
    ],
    'inserted template must append authored steps after the original ones',
  );
  assert.equal(authoredByStep.get(prefixStepId), insertedIds[0]);
  assert.equal(authoredByStep.get(tailStepId), insertedIds[1]);

  // Branch the tail step and execute it from the real prefix checkpoint.
  await page.getByText('高级：分支与测量决策').click();
  await page.getByLabel('尾步时长').fill('0.04');
  await page.getByRole('button', { name: '修改尾步并分支' }).click();
  await page.locator('.wf-status.verified').first().waitFor({ timeout: 30000 });

  const branchedPlan = (await getJson(`/api/v1/workflow/routes/${routeId}`)).plan;
  assert.equal(branchedPlan.attempts.length, 2, 'branch must append a second attempt');
  assert.ok(branchedPlan.reuse.length >= 1, 'branch must record a reused prefix');
  assert.equal(branchedPlan.reuse[0].fromStepId, splitPlan.stepOrder[0]);
  assert.equal(branchedPlan.reuse[0].toStepId, prefixStepId);
  const tailSegment = branchedPlan.segments.find(item => item.attemptId === branchedPlan.attempts[1].attemptId);
  assert.ok(tailSegment, 'branch must append a tail segment');
  assert.equal(tailSegment.startAfterStepId, prefixStepId);
  assert.equal(tailSegment.status, 'planned');
  assert.equal(
    branchedPlan.segments.find(item => item.segmentId === prefixBefore.segmentId).resultDigest,
    prefixBefore.resultDigest,
    'branch must not recompute the prefix',
  );

  await page.getByRole('button', { name: '执行当前 Segment' }).click();
  const finishedTail = await waitForSegment(tailSegment.segmentId);
  assert.match(finishedTail.resultDigest ?? '', SHA256, 'branch tail resultDigest must be materialized');
  assert.notEqual(finishedTail.runId, prefixBefore.runId);
  await page.waitForFunction(() => document.querySelectorAll('.wf-stale').length === 0, null, { timeout: 60000 });

  const branchCompile = await getJson(`/api/v1/workflow/compiles/${tailSegment.sourceCompileId}`);
  const branchTailStep = branchCompile.project.steps.find(step => step.id === tailStepId);
  assert.ok(branchTailStep, 'branch compile must contain the tail step');
  assert.equal(Number(branchTailStep.duration), 0.04, 'branch compile must use the edited duration');
  const unchangedPrefixStep = branchCompile.project.steps.find(step => step.id === prefixStepId);
  assert.equal(Number(unchangedPrefixStep.duration), 0.03, 'branch compile must keep the template prefix duration');

  // Persistent experiment must use the current recipe version and replicate seeds.
  await page.getByRole('button', { name: '04 实验' }).click();
  await page.getByRole('button', { name: '创建实验', exact: true }).click();
  await page.getByText('实验状态：待执行').waitFor();
  await page.getByRole('button', { name: '运行实验', exact: true }).click();
  await page.getByText('实验状态：已完成').waitFor({ timeout: 180000 });

  const experimentSummaries = (await getJson('/api/v1/workflow/experiments')).experiments;
  const experiment = await getJson(`/api/v1/workflow/experiments/${experimentSummaries[0].experimentId}`);
  assert.equal(experiment.status, 'completed');
  assert.equal(experiment.variants.length, 3);
  assert.equal(experiment.variants[0].replicates.length, 2);
  assert.equal(experiment.recipeVersion, 2, 'experiment must pin the latest recipe version');
  assert.deepEqual(
    experiment.project.steps.map(step => step.id),
    ['p1_step_01', 'p1_step_02', 'p1_step_03', 'p1_step_04', 'p1_step_05', ...insertedIds],
    'experiment must use the synchronized inserted project steps',
  );
  const recipes = (await getJson('/api/v1/workflow/metrology-recipes')).recipes.filter(item => item.recipeId === experiment.recipeId);
  assert.equal(experiment.recipeVersion, Math.max(...recipes.map(item => item.version)));
  for (const variant of experiment.variants) {
    variant.replicates.forEach((replicate, index) => {
      assert.equal(replicate.status, 'succeeded', JSON.stringify(replicate));
      assert.ok(replicate.measurementId, 'each replicate must carry a measurement');
      assert.equal(replicate.seed, 20261029 + variant.ordinal - 1 + index, 'replicate seed must follow the variant seed');
    });
  }

  await page.screenshot({ path: screenshot, fullPage: true });
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({
    status: 'passed', baseUrl, screenshot, errors, routeId,
    prefixDigest: prefixBefore.resultDigest, branchTailDigest: finishedTail.resultDigest,
    branchDuration: branchTailStep.duration, experimentRecipeVersion: experiment.recipeVersion,
  }));
} finally {
  await browser.close();
}
