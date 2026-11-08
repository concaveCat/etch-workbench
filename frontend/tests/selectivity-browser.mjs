/** Parent-run browser acceptance for schema 4 material selectivity. */
import {chromium,expect} from '@playwright/test';
import fs from 'node:fs/promises';
import path from 'node:path';

const root=path.resolve(import.meta.dirname,'../..');
const out=path.join(root,'docs/selectivity-evidence');
await fs.mkdir(out,{recursive:true});
const identity=JSON.parse(await fs.readFile(path.join(root,'var/server.json'),'utf8'));
const base=`http://127.0.0.1:${identity.port}`;
const report={status:'running',baseURL:base,checks:[],errors:[],simulationSubmissions:0};
let browser,page;
const pass=(name,data={})=>{report.checks.push({name,...data});console.log('PASS',name);};
const rowFor=name=>page.locator('.rate-row').filter({has:page.getByText(name,{exact:true})});
const setNumber=async(locator,value)=>{await locator.fill(String(value));await locator.blur();};
try{
  browser=await chromium.launch({headless:true,env:{...process.env,FONTCONFIG_FILE:path.join(root,'docs/materials-evidence/fontconfig.xml')},args:['--no-sandbox','--use-angle=swiftshader','--enable-unsafe-swiftshader']});
  const context=await browser.newContext({viewport:{width:1680,height:1100},acceptDownloads:true});
  page=await context.newPage();
  page.on('pageerror',error=>report.errors.push(String(error)));
  page.on('request',request=>{if(request.method()==='POST'&&/\/api\/runs$/.test(new URL(request.url()).pathname))report.simulationSubmissions++;});
  const get=async endpoint=>{const response=await context.request.get(base+endpoint);expect(response.ok(),await response.text()).toBeTruthy();return response.json();};
  const waitRun=async runId=>{
    let info;
    await expect.poll(async()=>{info=await get(`/api/runs/${runId}`);return info.status;},{timeout:60000,intervals:[250,500]}).toMatch(/succeeded|failed|timed_out|cancelled/);
    expect(info.status,info.failureReason).toBe('succeeded');
    return info;
  };
  const run=async kind=>{
    const responsePromise=page.waitForResponse(response=>response.url()===`${base}/api/runs`&&response.request().method()==='POST');
    await page.getByRole('button',{name:kind==='simulate'?'运行工艺':'生成结构预览',exact:true}).click();
    const response=await responsePromise;
    expect(response.status(),await response.text()).toBe(202);
    const body=response.request().postDataJSON();
    const started=await response.json();
    const info=await waitRun(started.runId);
    const row=info.snapshots.at(-1);
    const snapshot=await get(`/api/runs/${started.runId}/snapshots/${encodeURIComponent(row?.stepId??'initial')}`);
    await expect(page.getByLabel('当前结果步骤',{exact:true})).toHaveValue(row.stepId);
    const manifest=JSON.parse(await fs.readFile(path.join(root,'var/runs',started.runId,'manifest.json'),'utf8'));
    return {runId:started.runId,body,info,snapshot,manifest};
  };
  const downloadJSON=async buttonName=>{
    const downloadPromise=page.waitForEvent('download');
    await page.getByRole('button',{name:buttonName,exact:true}).click();
    const download=await downloadPromise;
    return JSON.parse(await fs.readFile(await download.path(),'utf8'));
  };
  const openService=async()=>{await page.goto(base);await expect(page.getByText('本机服务已连接')).toBeVisible();await expect(page.getByLabel('基底材料',{exact:true}).locator('option')).toHaveCount(13);};

  await openService();
  await page.getByLabel('项目名称',{exact:true}).fill('浏览器验收：选择性刻蚀');
  await page.getByRole('button',{name:'＋ 添加各向同性步骤',exact:true}).click();
  await page.getByLabel('速率模式',{exact:true}).selectOption('material');
  await expect(page.locator('.rate-row')).toHaveCount(3);
  await expect(rowFor('Si3N4').locator('.protected-mark')).toHaveText('0 · 保护');

  await page.getByLabel('新增材料速率行',{exact:true}).selectOption('PolySi');
  await page.getByRole('button',{name:'＋ 添加材料行',exact:true}).click();
  await expect(page.locator('.rate-row')).toHaveCount(4);
  await rowFor('PolySi').getByRole('button',{name:'移除 PolySi 速率覆盖',exact:true}).click();
  await expect(page.locator('.rate-row')).toHaveCount(3);
  await page.getByLabel('选择性参考材料',{exact:true}).selectOption('Si3N4');
  await expect(rowFor('Si3N4').locator('.rate-ratio')).toHaveText('— · 0/0 未定义');
  await expect(rowFor('Si').locator('.rate-ratio')).toHaveText(/^0\.0+×$/);
  await rowFor('SiO2').getByRole('button',{name:'移除 SiO2 速率覆盖',exact:true}).click();
  await expect(page.locator('.rate-row')).toHaveCount(2);
  await setNumber(page.getByLabel('未列出材料默认速率',{exact:false}),'0');
  await setNumber(rowFor('Si').getByLabel('刻蚀速率',{exact:true}),'.18');
  await expect(page.getByLabel('速率模式',{exact:true})).toHaveValue('material');
  await page.getByLabel('选择性参考材料',{exact:true}).selectOption('Si');
  await expect(rowFor('Si3N4').locator('.rate-ratio')).toHaveText('∞ · 保护');
  await expect(rowFor('Si').locator('.rate-ratio')).toHaveText('1.000×');
  await page.getByLabel('选择性参考材料',{exact:true}).selectOption('Si3N4');
  pass('2D材料速率表可增删行、显示保护0及0/0未定义参考比值',{overrides:Object.fromEntries(await Promise.all((await page.locator('.rate-row').all()).map(async row=>[await row.locator('strong').innerText(),await row.getByLabel('刻蚀速率').inputValue()]))) });

  let result=await run('simulate');
  expect(result.body.project.schemaVersion).toBe(4);
  expect(result.body.project.dimension).toBe(2);
  expect(result.body.project.steps[0]).toMatchObject({model:'isotropic',operation:'etch',rateMode:'material',rateMagnitude:0,materialRates:{Si:.18,Si3N4:0},selectivityReference:'Si3N4'});
  expect(Object.hasOwn(result.body.project.steps[0].materialRates,'SiO2')).toBe(false);
  expect(result.body.project.solver.random).toMatchObject({mode:'fixed',seed:expect.any(Number)});
  expect(result.snapshot.schemaVersion).toBe(4);
  expect(result.snapshot.dimension).toBe(2);
  expect(result.snapshot.noOp).toBe(false);
  expect(result.manifest.actualSeed).toBeNull();
  pass('2D各向同性：fallback=0、材料覆盖非零时执行，射线种子不适用',{runId:result.runId,schemaVersion:result.snapshot.schemaVersion,dimension:result.snapshot.dimension,noOp:result.snapshot.noOp});

  await page.getByRole('button',{name:'＋ 添加单粒子步骤',exact:true}).click();
  await page.getByLabel('速率模式',{exact:true}).selectOption('material');
  await page.getByLabel('选择性参考材料',{exact:true}).selectOption('Si3N4');
  await setNumber(page.getByLabel('未列出材料默认速率',{exact:false}),'0');
  await setNumber(rowFor('Si').getByLabel('刻蚀速率',{exact:true}),'.12');
  await rowFor('SiO2').getByRole('button',{name:'移除 SiO2 速率覆盖',exact:true}).click();
  result=await run('simulate');
  expect(result.body.project.schemaVersion).toBe(4);
  expect(result.body.project.steps).toHaveLength(2);
  expect(result.body.project.steps[0]).toMatchObject({model:'isotropic',rateMode:'material',rateMagnitude:0,materialRates:{Si:.18,Si3N4:0}});
  expect(result.body.project.steps[1]).toMatchObject({model:'singleParticle',operation:'etch',rateMode:'material',rateMagnitude:0,materialRates:{Si:.12,Si3N4:0}});
  expect(Object.hasOwn(result.body.project.steps[1].materialRates,'SiO2')).toBe(false);
  expect(result.body.project.solver.random).toMatchObject({mode:'fixed',seed:expect.any(Number)});
  expect(result.snapshot.schemaVersion).toBe(4);
  expect(result.snapshot.dimension).toBe(2);
  expect(result.snapshot.noOp).toBe(false);
  expect(result.manifest.actualSeed).toBe(result.body.project.solver.random.seed);
  const configured=result.manifest.actualExecution.find(s=>s.model==='singleParticle');
  expect(configured.actualParameters.actualMaterialRatesByName.Si).toBe(-.12);
  expect(Math.abs(configured.actualParameters.actualMaterialRatesByName.Si3N4)).toBe(0);
  expect(Math.abs(configured.actualParameters.actualDefaultRate)).toBe(0);
  pass('2D单粒子：fallback=0、材料覆盖非零时使用seed完成仿真',{runId:result.runId,seed:result.body.project.solver.random.seed,noOp:result.snapshot.noOp});

  const replay=await downloadJSON('导出重放配置');
  expect(replay.format).toBe('viennaps-replay');
  expect(replay.normalizedProject.schemaVersion).toBe(4);
  expect(replay.normalizedProject.steps[1]).toMatchObject({rateMode:'material',rateMagnitude:0,materialRates:{Si:.12,Si3N4:0},selectivityReference:'Si3N4'});
  pass('导出重放文件保留schema4速率配置',{runId:result.runId});

  const saveResponse=page.waitForResponse(response=>response.url()===`${base}/api/projects`&&response.request().method()==='POST');
  await page.getByRole('button',{name:'保存项目',exact:true}).click();
  const savedResponse=await saveResponse;
  expect(savedResponse.ok(),await savedResponse.text()).toBeTruthy();
  const saved=await savedResponse.json();
  expect(saved.project.schemaVersion).toBe(4);
  const projectId=saved.id;
  await page.screenshot({path:path.join(out,'selectivity-2d.png'),fullPage:true});

  await page.reload();
  await expect(page.getByText('本机服务已连接')).toBeVisible();
  await page.getByLabel('已保存项目',{exact:true}).selectOption(projectId);
  await page.getByRole('button',{name:'打开',exact:true}).click();
  await page.locator('.step-card').first().click();
  await expect(page.getByLabel('速率模式',{exact:true})).toHaveValue('material');
  await expect(page.locator('.rate-row')).toHaveCount(2);
  await expect(page.getByLabel('未列出材料默认速率',{exact:false})).toHaveValue('0');
  const exported=await downloadJSON('导出项目');
  expect(exported.schemaVersion).toBe(4);
  expect(exported.steps[0]).toMatchObject({rateMode:'material',rateMagnitude:0,materialRates:{Si:.18,Si3N4:0}});
  expect(exported.steps[1]).toMatchObject({rateMode:'material',rateMagnitude:0,materialRates:{Si:.12,Si3N4:0}});
  pass('保存、重开与导出项目保留两步材料速率配置',{projectId,schemaVersion:exported.schemaVersion});

  const replayResponse=page.waitForResponse(r=>r.url()===`${base}/api/runs/replay`&&r.request().method()==='POST');
  await page.locator('input[type=file]').setInputFiles({name:'selectivity-replay.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify(replay))});
  const replayStarted=await replayResponse;expect(replayStarted.status(),await replayStarted.text()).toBe(202);
  const replayId=(await replayStarted.json()).runId;const replayInfo=await waitRun(replayId);
  const replaySnapshot=await get(`/api/runs/${replayId}/snapshots/${replayInfo.snapshots.at(-1).stepId}`);
  expect(replaySnapshot.points).toEqual(result.snapshot.points);
  expect(replaySnapshot.lines).toEqual(result.snapshot.lines);
  expect(replaySnapshot.cellData.materialIds).toEqual(result.snapshot.cellData.materialIds);
  await expect(page.getByLabel('当前结果步骤',{exact:true})).toHaveValue(replayInfo.snapshots.at(-1).stepId);
  pass('从网页导入重放文件后真实执行，坐标/拓扑/材料数组一致',{runId:replayId});

  await page.getByRole('button',{name:'切换到 3D',exact:true}).click();
  await page.getByLabel('顶层图案',{exact:true}).selectOption('hole');
  const threeD=await run('preview');
  expect(threeD.body.project.schemaVersion).toBe(4);
  expect(threeD.body.project.dimension).toBe(3);
  expect(threeD.body.project.steps[1]).toMatchObject({rateMode:'material',materialRates:{Si:.12,Si3N4:0}});
  expect(threeD.snapshot.schemaVersion).toBe(4);
  expect(threeD.snapshot.dimension).toBe(3);
  await expect(page.getByTestId('viewer-3d')).toBeVisible();
  await expect(page.locator('.vtk-canvas canvas').first()).toBeVisible();
  const visible=page.locator('.material3d-controls input[type=checkbox]');
  const ids=[...new Set(threeD.snapshot.cellData.materialIds)].sort((a,b)=>a-b);
  await expect(visible).toHaveCount(ids.length);
  for(let index=0;index<ids.length;index++)await expect(visible.nth(index)).toBeChecked();
  pass('3D schema4预览识别正确维度，快照中的材料显隐项均默认可见',{runId:threeD.runId,dimension:threeD.snapshot.dimension,materialIds:ids});
  await page.screenshot({path:path.join(out,'selectivity-3d.png'),fullPage:true});

  await page.reload();await expect(page.getByText('本机服务已连接')).toBeVisible();
  const legacy=JSON.parse(await fs.readFile(path.join(root,'tests/fixtures/project-3d.json'),'utf8'));
  legacy.view3D.visibleMaterialIds=[];
  await page.locator('input[type=file]').setInputFiles({name:'legacy-3d.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify(legacy))});
  await expect(page.getByRole('status')).toContainText('项目 JSON 已导入');
  const unchanged=await downloadJSON('导出项目');expect(unchanged.schemaVersion).toBe(2);expect(unchanged.view3D.visibleMaterialIds).toEqual([]);
  await page.locator('.step-main').last().click();
  await page.getByLabel('速率模式',{exact:true}).selectOption('material');
  const upgraded=await run('preview');expect(upgraded.body.project.schemaVersion).toBe(4);
  await expect(page.getByTestId('viewer-3d')).toBeVisible();
  const legacyBoxes=page.locator('.material3d-controls input[type=checkbox]');await expect.poll(()=>legacyBoxes.count()).toBeGreaterThan(0);
  for(let index=0;index<await legacyBoxes.count();index++)await expect(legacyBoxes.nth(index)).toBeChecked();
  pass('旧三维项目浏览/导出保持版本，编辑材料速率升级后仍默认可见',{runId:upgraded.runId});

  expect(report.errors).toEqual([]);
  report.status='passed';
}catch(error){
  report.status='failed';report.failure=String(error.stack??error);
  if(page)await page.screenshot({path:path.join(out,'browser-failure.png'),fullPage:true}).catch(()=>{});
  console.error(error);process.exitCode=1;
}finally{
  if(browser)await browser.close();
  report.finishedAt=new Date().toISOString();
  await fs.writeFile(path.join(out,'browser-acceptance.json'),JSON.stringify(report,null,2));
}
