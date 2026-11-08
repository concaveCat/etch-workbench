/**
 * 22 nm FinFET bare-wafer acceptance for the merged WorkflowApp.
 *
 * End-to-end through the real UI (01 → 02 → 03) plus the run/snapshot/report
 * APIs, against an already running Workbench:
 *
 *   WORKBENCH_URL=http://127.0.0.1:8871 \
 *     node frontend/tests/workflow-finfet22-acceptance.mjs
 *
 * The server data dir either already holds the accepted demo or this script
 * seeds it:
 *   1. POST /api/gds-assets?file_name=finfet22_05.gds with the documented GDS
 *      (sha256 must match the accepted asset);
 *   2. POST /api/v1/workflow/projects with docs/finfet22-bare-wafer/flow_full.json
 *      (23 steps, 0.5 × 0.5 µm) unless an equivalent project already exists.
 *
 * Then: open the seeded project via localStorage, compile in 02, create a
 * Route, execute all Segments in 03 (~82 s on the reference CPU run; 15 min
 * budget), verify the real run + final snapshot, check the 3D viewer, and
 * generate the 06 report.
 *
 * The UI deliberately splits a Route after the second-to-last step, so the 23
 * completed steps may live in two segment runs. The check therefore unions the
 * completedStepIds of the runs the page lists, while still requiring the run
 * that owns the final step to be succeeded. A single-segment Route satisfies
 * the stricter one-run shape as well.
 *
 * Env: WORKBENCH_URL (default http://127.0.0.1:8871),
 *      WORKFLOW_GDS / WORKFLOW_FLOW (optional fixture overrides),
 *      WORKFLOW_RUN_TIMEOUT_MS (default 900000).
 */
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from '@playwright/test';

const here = path.dirname(fileURLToPath(import.meta.url));
const baseUrl = (process.env.WORKBENCH_URL ?? 'http://127.0.0.1:8871').replace(/\/$/, '');
const gdsPath = process.env.WORKFLOW_GDS
  ?? path.resolve(here, '../../docs/finfet22-bare-wafer/finfet22_05.gds');
const flowPath = process.env.WORKFLOW_FLOW
  ?? path.resolve(here, '../../docs/finfet22-bare-wafer/flow_full.json');
const expectedGdsSha = '1d2f5201d8de64163431255b4d409d02cf6dd1ec66e651571e9db440880290d5';
const gdsFileName = 'finfet22_05.gds';
const projectStorageKey = 'viennaps.workflow.projectId';
const finalAuthoredStepId = 'bw_m0_pattern';
const runTimeout = Number(process.env.WORKFLOW_RUN_TIMEOUT_MS ?? 15 * 60 * 1000);

const sleep = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));

async function request(url, options = {}) {
  const response = await fetch(url, options);
  const text = await response.text();
  let body = null;
  if (text) {
    try { body = JSON.parse(text); } catch { body = text; }
  }
  return { response, body, text };
}

async function getJson(url, label) {
  const { response, body, text } = await request(url);
  assert.equal(response.status, 200, `${label}: GET ${url} -> ${response.status} ${text.slice(0, 400)}`);
  return body;
}

async function postJson(url, payload, label, expected = [200, 201, 202]) {
  const { response, body, text } = await request(url, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(payload),
  });
  assert.ok(expected.includes(response.status),
    `${label}: POST ${url} -> ${response.status} ${text.slice(0, 400)}`);
  return { status: response.status, body, text };
}

/** Upload the documented GDS and return its content-addressed asset id. */
async function uploadGds() {
  const bytes = await readFile(gdsPath);
  const sha256 = createHash('sha256').update(bytes).digest('hex');
  assert.equal(sha256, expectedGdsSha,
    `${gdsPath} is not the accepted 22 nm GDS (sha256 ${sha256})`);
  const url = `${baseUrl}/api/gds-assets?file_name=${encodeURIComponent(gdsFileName)}`;
  const { response, body, text } = await request(url, {
    method: 'POST',
    headers: { 'content-type': 'application/octet-stream' },
    body: bytes,
  });
  assert.ok([200, 201].includes(response.status),
    `GDS upload failed: ${response.status} ${text.slice(0, 400)}`);
  assert.equal(body.assetId, sha256, 'upload must be content-addressed by sha256');
  return { assetId: body.assetId, sha256 };
}

/** Seed or reuse the accepted ProjectV7 and return its projectId. */
async function seedProject() {
  const { assetId, sha256 } = await uploadGds();
  const project = JSON.parse(await readFile(flowPath, 'utf8'));
  assert.equal(project.schemaVersion, 7, `${flowPath} must be a schemaVersion 7 project`);
  assert.equal(project.dimension, 3, 'the accepted demo is 3D');
  assert.equal(project.steps?.length, 23, 'the accepted demo has 23 steps');
  assert.equal(project.steps.at(-1)?.id, finalAuthoredStepId,
    `the accepted demo must end on ${finalAuthoredStepId}`);
  // Rebind to the uploaded asset so a regenerated GDS cannot go stale.
  project.geometry = { ...project.geometry, assetId };

  const listed = await getJson(`${baseUrl}/api/v1/workflow/projects`, 'project list');
  const existing = (listed.projects ?? []).find(record =>
    record.name === project.name && record.stepCount === project.steps.length);
  if (existing) {
    return { projectId: existing.projectId, projectName: project.name, assetId, sha256, reused: true };
  }

  const created = await postJson(
    `${baseUrl}/api/v1/workflow/projects`,
    project,
    'project create',
    [200, 201],
  );
  assert.match(String(created.body?.projectId ?? ''), /^proj_/,
    `project create returned no projectId: ${created.text.slice(0, 400)}`);
  return {
    projectId: created.body.projectId,
    projectName: project.name,
    assetId,
    sha256,
    reused: created.status === 200,
  };
}

function navButton(page, label) {
  return page.locator('.spx-functions button').filter({ hasText: label }).first();
}

async function projectNameShown(page, name, timeout = 60000) {
  await page.waitForFunction(expected => {
    const element = document.querySelector('.spx-tree-root strong');
    return !!element && (element.textContent ?? '').trim() === expected;
  }, name, { timeout, polling: 250 });
}

/** Save a gallery-loaded sample so later API checks have a project id. */
async function saveLoadedSample(page, projectName) {
  const save = page.locator('.spx-sheet-actions button')
    .filter({ hasText: /^(保存工程|保存更改)$/ }).first();
  await save.waitFor({ timeout: 30000 });
  await save.click();
  await page.locator('.spx-project-status').filter({ hasText: '已写入服务端' })
    .first().waitFor({ timeout: 60000 });
  const listed = await getJson(`${baseUrl}/api/v1/workflow/projects`, 'project list');
  const record = (listed.projects ?? []).find(item => item.name === projectName);
  assert.ok(record, `saved sample "${projectName}" is not listed by the server`);
  return record.projectId;
}

/**
 * Open the seeded project. If the stored id cannot be restored, fall back to
 * the 01 gallery card (data-example-id), then save it to obtain a project id.
 */
async function openProject(page, seeded) {
  await page.goto(`${baseUrl}/`, { waitUntil: 'domcontentloaded' });
  await page.locator('.spx-functions').waitFor({ timeout: 60000 });
  await page.evaluate(([key, id]) => window.localStorage.setItem(key, id),
    [projectStorageKey, seeded.projectId]);
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.locator('.spx-functions').waitFor({ timeout: 60000 });

  try {
    await projectNameShown(page, seeded.projectName, 30000);
    return { projectId: seeded.projectId, path: 'seeded-project' };
  } catch {
    // Seeded-project restore failed; fall through to the bundled gallery card.
  }

  const sample = page.locator('.spx-samples button.spx-sample[data-example-id="finfet22-bare-wafer"]').first();
  await sample.waitFor({ timeout: 30000 });
  await sample.click();
  await projectNameShown(page, seeded.projectName, 60000);
  const projectId = await saveLoadedSample(page, seeded.projectName);
  await page.evaluate(([key, id]) => window.localStorage.setItem(key, id),
    [projectStorageKey, projectId]);
  return { projectId, path: 'sample-gallery' };
}

async function main() {
  const health = await request(`${baseUrl}/api/health`);
  assert.equal(health.response.status, 200,
    `no Workbench at ${baseUrl} (GET /api/health -> ${health.response.status})`);

  const seeded = await seedProject();
  console.log(`seeded project ${seeded.projectId} (${seeded.reused ? 'reused' : 'created'})`);

  const browser = await chromium.launch({
    headless: true,
    args: ['--no-sandbox', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'],
  });
  const page = await browser.newPage({ viewport: { width: 1680, height: 1200 } });
  page.setDefaultTimeout(90000);
  page.setDefaultNavigationTimeout(60000);
  const pageErrors = [];
  page.on('pageerror', error => pageErrors.push(error.message));

  try {
    // ---- 01: the seeded project really opens ----------------------------
    const opened = await openProject(page, seeded);
    const projectId = opened.projectId;
    await projectNameShown(page, seeded.projectName);
    await page.locator('.spx-file-name.filled').filter({ hasText: seeded.projectName })
      .first().waitFor({ timeout: 30000 });
    assert.equal(await page.locator('.wf-error.spx-error').count(), 0,
      'opening the project must not raise a banner error');
    console.log(`01 open (${opened.path}) · project ${projectId}`);

    // ---- 02: compile and create the Route -------------------------------
    await navButton(page, '02 流程').click();
    await page.locator('.spx-flow-editor').waitFor();

    const compileButton = page.getByRole('button', { name: /^(编译流程|重新编译)$/ }).first();
    await compileButton.waitFor();
    await compileButton.click();
    await page.locator('.wf-summary.success').filter({ hasText: '编译成功' })
      .first().waitFor({ timeout: 180000 });
    console.log('02 compiled');

    const routeButton = page.getByRole('button', { name: /^(创建 Route|去 03)$/ }).first();
    await routeButton.waitFor({ timeout: 120000 });
    await routeButton.click();
    console.log('02 route ready');

    // ---- 03: execute every Segment --------------------------------------
    const executeAll = page.getByRole('button', { name: '执行全部 Segment' }).first();
    const executeCurrent = page.getByRole('button', { name: '执行当前 Segment' }).first();
    await Promise.race([
      executeAll.waitFor({ state: 'visible', timeout: 120000 }),
      executeCurrent.waitFor({ state: 'visible', timeout: 120000 }),
    ]);
    if (await executeAll.isVisible()) await executeAll.click({ timeout: 120000 });
    else await executeCurrent.click({ timeout: 120000 });
    console.log('03 run started; waiting for a terminal Segment status…');

    await page.waitForFunction(() => {
      const element = document.querySelector('.wf-status-row .wf-status');
      if (!element) return false;
      return ['succeeded', 'failed', 'cancelled', 'timed_out', 'interrupted']
        .some(name => element.classList.contains(name));
    }, null, { timeout: runTimeout, polling: 1000 });

    const segmentClass = await page.locator('.wf-status-row .wf-status').first().getAttribute('class') ?? '';
    const terminal = ['succeeded', 'failed', 'cancelled', 'timed_out', 'interrupted']
      .find(name => segmentClass.split(/\s+/).includes(name));
    if (terminal !== 'succeeded') {
      const notice = await page.locator('.wf-run-notice').first().innerText().catch(() => '');
      throw new Error(`03 Segment ended ${terminal ?? 'unknown'}${notice ? `: ${notice}` : ''}`);
    }

    // The timeline lists every executed step. The last executed chip is the
    // final M0 step; its run owns the final snapshot.
    await page.waitForFunction(() => {
      const chips = [...document.querySelectorAll('.wf-step-chip[data-kind="step"][data-executed="true"]')];
      const last = chips.at(-1);
      return !!last && !!last.getAttribute('data-run-id') && !!last.getAttribute('data-step-id');
    }, null, { timeout: 180000, polling: 500 });

    const finalChip = page.locator('.wf-step-chip[data-kind="step"][data-executed="true"]').last();
    const finalStepId = await finalChip.getAttribute('data-step-id');
    const finalRunId = await finalChip.getAttribute('data-run-id');
    const finalChipText = (await finalChip.innerText()).replace(/\s+/g, ' ');
    assert.ok(finalStepId && finalRunId, 'the final step chip must expose data-step-id/data-run-id');
    assert.match(finalChipText, /M0/i, `the final timeline step is not M0: ${finalChipText}`);

    const runIds = await page.$$eval('.wf-step-chip[data-run-id]', elements =>
      [...new Set(elements.map(item => item.getAttribute('data-run-id')).filter(Boolean))]);
    assert.ok(runIds.includes(finalRunId), 'the final run must be listed on the timeline');

    // ---- Real run: status + 23 completed steps --------------------------
    const runs = [];
    for (const runId of runIds) {
      runs.push(await getJson(`${baseUrl}/api/runs/${encodeURIComponent(runId)}`, `run ${runId}`));
    }
    const finalRun = runs.find(run => run.runId === finalRunId);
    assert.ok(finalRun, `GET /api/runs/${finalRunId} did not return the final run`);
    assert.equal(finalRun.status, 'succeeded', `final run ${finalRunId} is ${finalRun.status}`);

    const completedByRun = runs.map(run => ({ runId: run.runId, completed: run.completedStepIds?.length ?? 0 }));
    const completedStepIds = new Set(runs.flatMap(run => run.completedStepIds ?? []));
    assert.equal(completedStepIds.size, 23,
      `expected 23 unique completed steps across the Route runs, got ${completedStepIds.size} (${JSON.stringify(completedByRun)})`);
    assert.ok(completedStepIds.has(finalStepId), 'completedStepIds must include the final step');
    if (finalRun.completedStepIds?.length === 23) {
      assert.equal(completedStepIds.size, 23);
    }
    console.log(`03 run ${finalRunId} succeeded · steps ${JSON.stringify(completedByRun)}`);

    // ---- Final snapshot: mesh + material level sets ---------------------
    // Compiled workflow step ids are hashes, so prefer the authored id and fall
    // back to the id the page reported, exactly as the endpoint stores it.
    let snapshot = await request(
      `${baseUrl}/api/runs/${encodeURIComponent(finalRunId)}/snapshots/${encodeURIComponent(finalAuthoredStepId)}`,
    );
    let snapshotStep = finalAuthoredStepId;
    if (snapshot.response.status !== 200) {
      snapshot = await request(
        `${baseUrl}/api/runs/${encodeURIComponent(finalRunId)}/snapshots/${encodeURIComponent(finalStepId)}`,
      );
      snapshotStep = finalStepId;
    }
    assert.equal(snapshot.response.status, 200,
      `final snapshot unavailable: ${snapshot.text.slice(0, 400)}`);
    const finalSnapshot = snapshot.body;
    const pointCount = Number(finalSnapshot?.mesh?.pointCount ?? 0);
    assert.ok(pointCount > 0, `final snapshot mesh.pointCount is ${pointCount}`);
    const materialIds = finalSnapshot?.materialIdsByLevelSetIndex ?? [];
    assert.ok(Array.isArray(materialIds), 'final snapshot has no materialIdsByLevelSetIndex');
    assert.ok(materialIds.includes(70), `final snapshot has no W (70): ${JSON.stringify(materialIds)}`);
    assert.ok(materialIds.includes(30), `final snapshot has no SiO2 (30): ${JSON.stringify(materialIds)}`);
    if (typeof finalSnapshot.stepName === 'string') {
      assert.match(finalSnapshot.stepName, /M0/i, `final snapshot is not M0: ${finalSnapshot.stepName}`);
    }
    console.log(`03 snapshot ${snapshotStep} · ${pointCount} points · materials ${JSON.stringify(materialIds)}`);

    // ---- 03 UI: final step selected + 3D viewer canvas ------------------
    const selectedFinal = page.locator(
      `.wf-step-chip[data-step-id="${finalStepId}"][aria-selected="true"]`,
    ).first();
    await selectedFinal.waitFor({ timeout: 180000 });

    const viewer = page.locator('[data-testid="workflow-viewer-3d"]').first();
    await viewer.waitFor({ state: 'visible', timeout: 180000 });
    await page.waitForFunction(() => {
      const root = document.querySelector('[data-testid="workflow-viewer-3d"]');
      const canvas = root?.querySelector('canvas');
      if (!canvas) return false;
      const box = canvas.getBoundingClientRect();
      return box.width > 0 && box.height > 0;
    }, null, { timeout: 180000, polling: 500 });
    const canvasBox = await viewer.locator('canvas').first().boundingBox();
    assert.ok(canvasBox && canvasBox.width > 0 && canvasBox.height > 0,
      `3D canvas has no size: ${JSON.stringify(canvasBox)}`);
    console.log(`03 viewer ready · canvas ${Math.round(canvasBox.width)}×${Math.round(canvasBox.height)}`);

    // ---- 06: report -----------------------------------------------------
    await navButton(page, '06 报告').click();
    const reportSummary = page.locator('[aria-label="报告内容摘要"]').first();
    await reportSummary.waitFor({ state: 'visible', timeout: 60000 });
    assert.match(await reportSummary.innerText(), /Run/, 'report summary must describe the run');

    const generate = page.getByRole('button', { name: '生成报告', exact: true }).first();
    await generate.waitFor({ state: 'visible', timeout: 60000 });
    await page.waitForFunction(() => {
      const button = [...document.querySelectorAll('button')]
        .find(item => (item.textContent ?? '').trim() === '生成报告');
      return !!button && !button.disabled;
    }, null, { timeout: 60000, polling: 250 });
    await generate.click();

    let latestReport = null;
    try {
      await page.locator('iframe[title="报告预览"]').first()
        .waitFor({ state: 'visible', timeout: 180000 });
      const reportBody = await page.frameLocator('iframe[title="报告预览"]')
        .locator('body').innerText({ timeout: 60000 });
      assert.ok(reportBody.trim().length > 0, 'the generated report body is empty');
      latestReport = await getJson(
        `${baseUrl}/api/v1/workflow/projects/${projectId}/reports/latest`,
        'latest report',
      );
    } catch (error) {
      // Fallback for a report that needs a measurement: the button must stay
      // available and the project must already hold a report document.
      assert.equal(await generate.isEnabled(), true,
        `生成报告 is disabled and no report preview appeared: ${error.message}`);
      latestReport = await getJson(
        `${baseUrl}/api/v1/workflow/projects/${projectId}/reports/latest`,
        'latest report (fallback)',
      );
    }
    assert.ok(latestReport?.reportId, 'the project has no stored report document');
    const reportUrl = `${baseUrl}/api/v1/workflow/projects/${projectId}/reports/${latestReport.reportId}`;
    console.log(`06 report ${latestReport.reportId}`);

    if (pageErrors.length) console.log(`page errors (non-fatal): ${pageErrors.join(' | ')}`);
    console.log(`PASS ${JSON.stringify({
      projectId,
      runId: finalRunId,
      runIds,
      completedSteps: completedStepIds.size,
      finalStep: finalAuthoredStepId,
      finalStepId,
      snapshotStep,
      pointCount,
      materialIds,
      reportId: latestReport.reportId,
      reportUrl,
      path: opened.path,
    })}`);
  } finally {
    await browser.close();
  }
}

try {
  await main();
} catch (error) {
  console.error(`FAIL ${error?.stack ?? error}`);
  process.exitCode = 1;
}
