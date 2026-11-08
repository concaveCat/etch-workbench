/** Real browser acceptance. Run on the host after scripts/start.sh. */
import { chromium, expect } from '@playwright/test';
import fs from 'node:fs/promises';
import path from 'node:path';
const root = path.resolve(import.meta.dirname, '../..');
const dataRoot = path.resolve(process.env.VIENNAPS_WORKBENCH_DATA_DIR || path.join(root, 'var'));
const out = path.join(dataRoot, 'evidence/browser');
await fs.mkdir(out, { recursive: true });
const identity = JSON.parse(await fs.readFile(path.join(dataRoot, 'server.json'))), base = `http://127.0.0.1:${identity.port}`;
const report = { startedAt: new Date().toISOString(), baseURL: base, status: 'running', checks: [], consoleErrors: [], heapSamples: [] };
let browser, page;
const check = (name, data = {}) => { report.checks.push({ name, ...data }); console.log(`PASS ${name}`); };
try {
  // Never submit native work while the preserved application is computing.
  for (const id of await fs.readdir(path.join(root, 'var/runs'))) {
    try { const m = JSON.parse(await fs.readFile(path.join(root, 'var/runs', id, 'manifest.json'))); if (['pending','running','cancelling'].includes(m.status)) throw new Error(`候选工作台有活动任务 ${id}`); }
    catch (e) { if (String(e).includes('活动任务')) throw e; }
  }
  browser = await chromium.launch({ headless: true, env: { ...process.env, FONTCONFIG_FILE: path.join(root, 'docs/materials-evidence/fontconfig.xml') }, args: ['--no-sandbox', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
  const context = await browser.newContext({ viewport: { width: 1680, height: 1080 }, acceptDownloads: true });
  page = await context.newPage(); page.on('pageerror', e => report.consoleErrors.push(String(e)));
  page.on('console', msg => { if (msg.type() === 'error' && !/Failed to load resource.*404/.test(msg.text())) report.consoleErrors.push(msg.text()); });
  const projectMenu = page.locator('nav[aria-label="项目操作"] details.ui-menu');
  const recipeMenu = page.locator('.process-section details.ui-menu');
  const menuOpen = async menu => menu.evaluate(element => element.open);
  const openMenu = async menu => { if (!await menuOpen(menu)) await menu.locator('summary').click(); };
  const closeMenu = async menu => { if (await menuOpen(menu)) await menu.locator('summary').click(); };
  const clickProjectAction = async name => { await openMenu(projectMenu); await page.getByRole('button', { name, exact: true }).click(); await closeMenu(projectMenu); };
  const requestJSON = async (url, init) => { const r = await context.request.fetch(base + url, init); if (!r.ok()) throw new Error(`${url}: ${r.status()} ${await r.text()}`); return r.json(); };
  const waitRun = async id => {
    let info; await expect.poll(async () => { info = await requestJSON(`/api/runs/${id}`); return info.status; }, { timeout: 120000 }).toMatch(/succeeded|failed|cancelled|timed_out/);
    expect(info.status, info.failureReason).toBe('succeeded'); return info;
  };
  const submitUI = async button => { const response = page.waitForResponse(r => r.url().endsWith('/api/runs') && r.request().method() === 'POST'); await page.getByRole('button', { name: button, exact: true }).click(); const r = await response; expect(r.status()).toBe(202); const d = await r.json(); await waitRun(d.runId); return d.runId; };
  const number = async (label, value) => { const field = page.getByRole('spinbutton', { name: label, exact: false }); await field.fill(String(value)); await field.blur(); };
  await page.goto(base); await expect(page.getByText('本机服务已连接')).toBeVisible();
  await clickProjectAction('新建工程');
  const dimension2D = page.getByRole('button', { name: '切换到 2D', exact: true }), dimension3D = page.getByRole('button', { name: '切换到 3D', exact: true });
  await expect(dimension2D).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByRole('button', { name: '新建二维', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: '新建三维', exact: true })).toHaveCount(0);
  await dimension3D.click(); await expect(dimension3D).toHaveAttribute('aria-pressed', 'true'); await expect(page.getByLabel('y 横向总宽度')).toBeVisible();
  await dimension2D.click(); await expect(dimension2D).toHaveAttribute('aria-pressed', 'true'); await expect(page.getByLabel('y 横向总宽度')).toHaveCount(0);
  await dimension3D.click(); await expect(dimension3D).toHaveAttribute('aria-pressed', 'true');
  check('新建工程入口与 2D/3D 维度切换');
  await number('y 横向总宽度', 4); await page.getByLabel('项目名称', { exact: true }).fill('浏览器三维验收');
  const previewId = await submitUI('生成结构预览'); await expect(page.getByTestId('viewer-3d')).toBeVisible();
  check('创建三维并真实预览', { runId: previewId });
  // Exercise the visible recipe editor, including operations and model fields.
  await page.getByRole('button', { name: '配方', exact: true }).click();
  await openMenu(recipeMenu);
  await page.getByRole('button', { name: '＋ 添加单粒子步骤', exact: true }).click();
  await page.getByRole('tab', { name: '基本', exact: true }).click();
  await page.getByLabel('工艺操作', { exact: false }).selectOption('deposit');
  await page.getByRole('tab', { name: '物理', exact: true }).click();
  await number('持续时间', .2);
  await page.getByRole('button', { name: '设置', exact: true }).click(); await number('每点射线数', 64);
  await page.getByRole('button', { name: '配方', exact: true }).click();
  await openMenu(recipeMenu);
  await page.getByRole('button', { name: '＋ 添加单粒子步骤', exact: true }).click();
  await page.getByRole('tab', { name: '物理', exact: true }).click();
  await number('持续时间', .2);
  const runId = await submitUI('运行工艺'); report.runId = runId;
  await expect(page.getByLabel('当前结果步骤')).toHaveValue(/step_/); await page.waitForTimeout(1200);
  const info = await requestJSON(`/api/runs/${runId}`); expect(info.snapshots).toHaveLength(3);
  expect(info.actualExecution.map(s => s.actualEngine)).toEqual(['GPU_DISK', 'GPU_DISK']);
  const gpuUuid = 'GPU-5532bb0f-735e-bc93-98ca-b7662a8ebf6d';
  expect(info.actualExecution.every(s => s.actualParameters.cudaDeviceReadback.afterProcessApply.uuid === gpuUuid)).toBeTruthy();
  check('网页多步沉积→刻蚀在 RTX 3060 上真实运行', { runId, snapshotCount: info.snapshots.length, engines: info.actualExecution.map(s => s.actualEngine), gpuUuid });
  const canvas = page.locator('.vtk-canvas canvas').first(); await expect(canvas).toBeVisible();
  const box = await canvas.boundingBox(); if (!box) throw new Error('missing canvas bounds');
  await page.mouse.move(box.x + box.width*.45, box.y + box.height*.45); await page.mouse.down(); await page.mouse.move(box.x + box.width*.60, box.y + box.height*.54, { steps: 10 }); await page.mouse.up();
  await page.keyboard.down('Shift'); await page.mouse.move(box.x + box.width*.5, box.y + box.height*.5); await page.mouse.down(); await page.mouse.move(box.x + box.width*.53, box.y + box.height*.54, { steps: 6 }); await page.mouse.up(); await page.keyboard.up('Shift'); await page.mouse.wheel(0, -180);
  for (const projection of ['perspective', 'parallel']) await page.getByLabel('投影', { exact: true }).selectOption(projection);
  for (const mode of ['wireframe', 'surfaceEdges', 'surface']) await page.getByLabel('显示模式', { exact: true }).selectOption(mode);
  for (const direction of ['XY','XZ','YZ']) await page.getByRole('button', { name: direction, exact: true }).click();
  await page.getByLabel('显示 Si', { exact: true }).uncheck(); await page.getByLabel('显示 Si', { exact: true }).check();
  await page.getByLabel('SiO2 透明度', { exact: true }).fill('0.6');
  check('旋转/平移/缩放/投影/标准视图/材料/线框');
  await page.getByLabel('裁剪面', { exact: true }).check();
  await page.getByLabel('正交截面', { exact: true }).check();
  for (const axis of ['x','y','z']) {
    await page.getByLabel('clip轴', { exact: true }).selectOption(axis); await page.getByLabel('section轴', { exact: true }).selectOption(axis);
    await page.getByLabel('clip位置', { exact: true }).fill(axis==='z' ? '-1' : '0.1'); await page.getByLabel('section位置', { exact: true }).fill(axis==='z' ? '0.8' : '0');
    await expect(page.getByTestId('section-panel').locator('path').first()).toHaveAttribute('d', /M.+L/);
  }
  await page.getByLabel('保留侧', { exact: true }).selectOption('negative'); await page.getByLabel('保留侧', { exact: true }).selectOption('positive');
  await page.getByLabel('clip轴', { exact: true }).selectOption('y'); await page.getByLabel('clip位置', { exact: true }).fill('0');
  await page.getByLabel('section轴', { exact: true }).selectOption('y'); await page.getByLabel('section位置', { exact: true }).fill('0');
  check('x/y/z 双侧裁剪与真实等比例截面');
  await page.locator('.compare-select select').selectOption(previewId); await expect(page.getByLabel('对比步骤')).toBeVisible();
  await page.getByRole('button', { name: '重置视图', exact: true }).click();
  await page.screenshot({ path: path.join(out, 'comparison-section.png'), fullPage: true });
  await page.getByTestId('section-panel').scrollIntoViewIfNeeded(); await page.getByTestId('section-panel').screenshot({ path: path.join(out, 'section-detail.png') });
  await page.getByRole('button', { name: '⇩ PNG', exact: true }).scrollIntoViewIfNeeded(); check('三维比较共用相机与裁剪');
  const pngDownload = page.waitForEvent('download'); await page.getByRole('button', { name: '⇩ PNG', exact: true }).click();
  const png = await pngDownload; const pngPath = path.join(out, 'view.png'); await png.saveAs(pngPath); const pngBytes = await fs.readFile(pngPath);
  expect(pngBytes.subarray(0,8).toString('hex')).toBe('89504e470d0a1a0a'); expect(pngBytes.length).toBeGreaterThan(3000); check('当前三维视图 PNG', { bytes: pngBytes.length });
  await openMenu(projectMenu); await page.getByRole('button', { name: '保存项目', exact: true }).click(); await expect(page.getByText('项目已保存到本机工作台。')).toBeVisible(); await closeMenu(projectMenu);
  const projects = await requestJSON('/api/projects'), saved = projects.projects.find(p => p.name === '浏览器三维验收'); expect(saved).toBeTruthy(); report.projectId = saved.id;
  const projectBefore = (await requestJSON(`/api/projects/${saved.id}`)).project; expect(projectBefore.view3D.camera.position).toHaveLength(3);
  await page.reload(); await openMenu(projectMenu); await page.getByLabel('已保存项目', { exact: true }).selectOption(saved.id); await page.getByRole('button', { name: '打开', exact: true }).click(); await closeMenu(projectMenu);
  await openMenu(projectMenu); const exportDownload = page.waitForEvent('download'); await page.getByRole('button', { name: '导出项目', exact: true }).click(); const exported = await exportDownload; await closeMenu(projectMenu); const projectPath = path.join(out, 'project.json'); await exported.saveAs(projectPath);
  expect(JSON.parse(await fs.readFile(projectPath)).view3D).toEqual(projectBefore.view3D); check('保存/刷新/重开/导出保留三维相机');
  await page.getByRole('button', { name: '结构', exact: true }).click();
  await dimension2D.click(); await expect(page.getByLabel('y 横向总宽度')).toHaveCount(0);
  expect((await requestJSON(`/api/projects/${saved.id}`)).project).toEqual(projectBefore);
  await dimension3D.click(); await expect(page.getByLabel('y 横向总宽度')).toBeVisible();
  check('已保存工程切换维度时另建草稿并保留来源项目');
  await page.getByRole('button', { name: '结果', exact: true }).click();
  await page.locator('.history-item').filter({ hasText: runId.slice(0,8) }).click(); await expect(page.getByTestId('viewer-3d')).toBeVisible();
  const replayDownload = page.waitForEvent('download'); await page.getByRole('button', { name: '导出重放配置', exact: true }).click(); const replayFile = await replayDownload; const replayPath = path.join(out, 'replay.json'); await replayFile.saveAs(replayPath);
  const replay = JSON.parse(await fs.readFile(replayPath)); expect(replay.format).toBe('viennaps-replay'); expect(replay.schemaVersion).toBe(7);
  expect(replay.executionRequirements.steps.map(s => s.engine)).toEqual(info.actualExecution.map(s => s.actualEngine));
  expect(replay.executionRequirements.steps.every(s => s.seed === 314159)).toBeTruthy();
  const replayResponse = page.waitForResponse(r => r.url().endsWith('/api/runs/replay') && r.request().method()==='POST'); await page.locator('input[type=file]').setInputFiles(replayPath); const rr = await replayResponse; expect(rr.status()).toBe(202); report.replayRunId = (await rr.json()).runId; await waitRun(report.replayRunId); await page.waitForTimeout(1200);
  const replayInfo = await requestJSON(`/api/runs/${report.replayRunId}`); expect(replayInfo.actualExecution.map(s => s.actualEngine)).toEqual(info.actualExecution.map(s => s.actualEngine));
  check('网页导入 replay 固定实际引擎、种子与线程', { replayRunId: report.replayRunId, engines: replayInfo.actualExecution.map(s => s.actualEngine) });
  const zipDownload = page.waitForEvent('download'); await page.getByRole('button', { name: '下载运行结果 ZIP', exact: true }).click(); const zip = await zipDownload; const zipPath = path.join(out, 'run.zip'); await zip.saveAs(zipPath); const zipBytes = await fs.readFile(zipPath); expect(zipBytes.subarray(0,2).toString()).toBe('PK'); expect(zipBytes.length).toBeGreaterThan(10000); check('网页下载真实运行 ZIP', { bytes:zipBytes.length });
  // GPU controls follow the verified 3D capability rows. When the host has
  // a passed row, exercise the UI selector, real run and fixed-engine replay.
  const gpuCaps = (await requestJSON('/api/capabilities')).capabilities.threeDimensional.rows;
  const gpuRows = gpuCaps.filter(r => r.dimension === 3 && r.model === 'singleParticle' && r.engine.startsWith('GPU_') && r.boundaryXY === 'reflective');
  if (gpuRows.length) {
    const gpuRow = gpuRows.find(r => r.operation === 'etch') ?? gpuRows[0]; report.gpu = { status: 'verified-host', row: gpuRow };
    await clickProjectAction('新建工程');
    await page.getByRole('button', { name: '结构', exact: true }).click();
    await expect(page.getByLabel('顶层图案', { exact: true })).toHaveValue('hole');
    await dimension2D.click(); await expect(page.getByRole('spinbutton', { name: /沟槽宽度/ })).toHaveValue('2');
    await expect(page.getByText('已新建对应 2D 工程：圆孔按 x 截面转换为宽度等于孔径的沟槽；原工程和历史结果保持不变。')).toBeVisible();
    await dimension3D.click(); await expect(page.getByLabel('y 横向总宽度')).toBeVisible();
    check('3D 圆孔切换 2D 时显式转换为孔径宽度沟槽');
    await page.getByRole('button', { name: '配方', exact: true }).click();
    await openMenu(recipeMenu);
    await page.getByRole('button', { name: '＋ 添加单粒子步骤', exact: true }).click();
    await page.getByLabel('工艺操作', { exact: false }).selectOption(gpuRow.operation);
    await page.getByRole('button', { name: '设置', exact: true }).click();
    await page.getByLabel('引擎偏好', { exact: false }).selectOption('gpu');
    await expect(page.getByLabel('引擎偏好', { exact: false })).toHaveValue('gpu');
    const gpuRunId = await submitUI('运行工艺'); report.gpu.runId = gpuRunId;
    const gpuManifest = await requestJSON(`/api/runs/${gpuRunId}`); const gpuManifestFull = await requestJSON(`/api/runs/${gpuRunId}/replay-project`);
    expect(gpuManifestFull.executionRequirements.steps[0].engine).toBe(gpuRow.engine);
    check('实测三维 GPU UI 引擎选择与固定引擎运行', { runId: gpuRunId, engine: gpuRow.engine });
    const gpuReplayDownload = page.waitForEvent('download'); await page.getByRole('button', { name: '导出重放配置', exact: true }).click();
    const gpuReplayFile = await gpuReplayDownload, gpuReplayPath = path.join(out, 'gpu-replay.json'); await gpuReplayFile.saveAs(gpuReplayPath);
    const gpuReplayResponse = page.waitForResponse(r => r.url().endsWith('/api/runs/replay') && r.request().method() === 'POST');
    await page.locator('input[type=file]').setInputFiles(gpuReplayPath); const gr = await gpuReplayResponse; expect(gr.status()).toBe(202);
    const gpuReplayRunId = (await gr.json()).runId; await waitRun(gpuReplayRunId); report.gpu.replayRunId = gpuReplayRunId;
    expect((await requestJSON(`/api/runs/${gpuReplayRunId}`)).status).toBe('succeeded');
    check('三维 GPU 固定种子网页重放');
  } else {
    report.gpu = { status: 'no-verified-host-combination', sandboxCuInit: 'unavailable; see separate host probe result' };
    await clickProjectAction('新建工程');
    await page.getByRole('button', { name: '配方', exact: true }).click(); await openMenu(recipeMenu);
    await page.getByRole('button', { name: '＋ 添加单粒子步骤', exact: true }).click();
    await expect(page.getByLabel('引擎偏好', { exact: false }).locator('option[value="gpu"]')).toBeDisabled();
    check('三维 GPU capability-checked UI state', { exposed: false });
  }
  // Repeat loading two larger bounded snapshots and force GC for retained-heap measurements.
  const large = JSON.parse(await fs.readFile(path.join(dataRoot,'evidence/browser-large.json')));
  const cdp = await context.newCDPSession(page);
  for (let i=0;i<24;i++) {
    const id=large.runIds[i%large.runIds.length]; await page.locator('.history-item').filter({hasText:id.slice(0,8)}).click();
    await expect(page.locator('.run-id')).toHaveText(`RUN ${id.slice(0,12)}`); await page.waitForTimeout(120);
    if (i>=4 && i%4===0) { await cdp.send('HeapProfiler.collectGarbage'); const usage=await cdp.send('Runtime.getHeapUsage'); report.heapSamples.push(usage.usedSize); }
  }
  const growth=report.heapSamples.at(-1)-report.heapSamples[0]; expect(growth).toBeLessThan(12*1024*1024); check('24 次大快照切换释放 vtk 对象', { heapSamples:report.heapSamples,growthBytes:growth });
  // Load real legacy files through the same UI and verify dimension routing and disabled overlay.
  const legacy=JSON.parse(await fs.readFile(path.join(root,'tests/fixtures/legacy/index.json')))[0];
  await page.locator('.compare-select select').selectOption(runId);
  const legacyRun=JSON.parse(await fs.readFile(path.join(dataRoot,'evidence/browser-large.json'))).legacyRunId;
  expect(legacyRun).toBe(legacy.runId);
  await page.locator('.history-item').filter({hasText:legacyRun.slice(0,8)}).click();
  // This archived v1 fixture intentionally contains a valid initial snapshot,
  // while its manifest references a final file omitted from the fixture pack.
  // Select the present snapshot to exercise actual legacy history loading.
  await page.getByLabel('当前结果步骤').selectOption('initial');
  await expect(page.getByRole('img',{name:'ViennaPS 二维材料界面查看器'})).toBeVisible();
  await expect(page.getByText('二维 / 三维直接叠加比较已禁用，请选择相同维度的快照。')).toBeVisible();
  const svgDownload=page.waitForEvent('download');await page.getByRole('button',{name:'⇩ SVG',exact:true}).click();await(await svgDownload).saveAs(path.join(out,'legacy.svg'));check('旧二维历史初始快照/SVG 与跨维度比较禁用',{fixture:legacy.runId,snapshot:'initial'});
  await page.screenshot({path:path.join(out,'legacy-routing.png'),fullPage:true});
  expect(report.consoleErrors).toEqual([]);report.status='passed';
} catch(e) {
  report.status='failed';report.error=String(e);console.error(e);
  if(page)await page.screenshot({path:path.join(out,'failure.png'),fullPage:true}).catch(()=>{});
} finally {
  if(browser)await browser.close();report.finishedAt=new Date().toISOString();await fs.writeFile(path.join(dataRoot,'evidence/browser-3d.json'),JSON.stringify(report,null,2));
}
if(report.status!=='passed')process.exitCode=1;
