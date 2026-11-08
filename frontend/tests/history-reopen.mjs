/** Read-only history reopen regression; never submits a simulation. */
import {chromium,expect} from '@playwright/test';
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
const root=path.resolve(import.meta.dirname,'../..'),out=path.join(root,'docs/history-reopen-evidence');
await fs.mkdir(out,{recursive:true});
const identity=JSON.parse(await fs.readFile(path.join(root,'var/server.json'))),base=`http://127.0.0.1:${identity.port}`;
const report={status:'running',baseURL:base,checks:[],errors:[],simulationSubmissions:0};let browser,page;
const check=(name,data={})=>{report.checks.push({name,...data});console.log('PASS',name);};
const sha=async p=>crypto.createHash('sha256').update(await fs.readFile(p)).digest('hex');
try{
 browser=await chromium.launch({headless:true,env:{...process.env,FONTCONFIG_FILE:path.join(root,'docs/materials-evidence/fontconfig.xml')},args:['--no-sandbox','--use-angle=swiftshader','--enable-unsafe-swiftshader']});
 const context=await browser.newContext({viewport:{width:1680,height:1100},acceptDownloads:true});page=await context.newPage();
 page.on('pageerror',e=>report.errors.push(String(e)));
 page.on('request',r=>{if(r.method()==='POST'&&/\/api\/runs(?:\/replay)?$/.test(r.url()))report.simulationSubmissions++;});
 const get=async p=>{const r=await context.request.get(base+p);expect(r.ok(),await r.text()).toBeTruthy();return r.json();};
 const runs=(await get('/api/runs')).runs;let dim2,dim3;
 for(const r of runs){
  const manifest=JSON.parse(await fs.readFile(path.join(root,'var/runs',r.runId,'manifest.json')));
  if(manifest.status!=='succeeded'||!manifest.snapshots?.length)continue;
  if(manifest.normalizedProject.dimension===3&&manifest.snapshots.length>1&&!dim3)dim3={...r,manifest};
  if(manifest.normalizedProject.dimension===2&&!dim2)dim2={...r,manifest};
  if(dim2&&dim3)break;
 }
 if(!dim2||!dim3)throw new Error('need existing successful 2D and 3D history');
 const original={};for(const target of [dim2,dim3])for(const file of ['manifest.json',...target.manifest.snapshots.flatMap(s=>[s.snapshot,s.vtk])]){
  const key=`${target.runId}/${file}`;original[key]=await sha(path.join(root,'var/runs',key));
 }
 const reopen=async()=>{await page.goto(base);await expect(page.getByText('本机服务已连接')).toBeVisible();await expect(page.getByLabel('基底材料',{exact:true}).locator('option')).toHaveCount(13);};
 const clickHistory=async target=>{await page.locator('.history-item').filter({hasText:target.runId.slice(0,8)}).click();};
 const assertShown3D=async()=>{
  await expect(page.getByTestId('viewer-3d')).toBeVisible();await expect(page.locator('.vtk-canvas canvas').first()).toBeVisible();
  const boxes=page.locator('.material3d-controls input[type=checkbox]');await expect.poll(()=>boxes.count()).toBeGreaterThan(0);
  for(let i=0;i<await boxes.count();i++)await expect(boxes.nth(i)).toBeChecked();
 };
 await reopen();await expect(page.getByRole('button',{name:'切换到 2D',exact:true})).toHaveAttribute('aria-pressed','true');
 await clickHistory(dim3);await assertShown3D();await expect(page.getByRole('button',{name:'切换到 2D',exact:true})).toHaveAttribute('aria-pressed','true');
 check('重新打开默认二维工程后，历史三维材料默认可见',{runId:dim3.runId});
 await page.screenshot({path:path.join(out,'reopened-3d.png'),fullPage:true});
 const boxes=page.locator('.material3d-controls input[type=checkbox]');await boxes.first().uncheck();await expect(boxes.first()).not.toBeChecked();await boxes.first().check();
 await clickHistory(dim2);await expect(page.getByRole('img',{name:'ViennaPS 二维材料界面查看器'})).toBeVisible();
 await clickHistory(dim3);await assertShown3D();check('二维历史与三维历史切换正常，显隐控件可用',{run2D:dim2.runId});
 if(dim3.manifest.snapshots.length>1){
  await page.getByLabel('当前结果步骤',{exact:true}).selectOption('initial');await expect(page.getByTestId('viewer-3d')).toBeVisible();
  await page.getByLabel('当前结果步骤',{exact:true}).selectOption(dim3.manifest.snapshots.at(-1).stepId);await assertShown3D();check('历史三维各步骤切换正常');
 }
 await page.reload();await expect(page.getByText('本机服务已连接')).toBeVisible();await clickHistory(dim3);await assertShown3D();check('刷新后再次打开三维历史正常');
 await page.reload();await expect(page.getByText('本机服务已连接')).toBeVisible();
 const edited=structuredClone(dim3.manifest.normalizedProject);edited.name='历史视图隔离测试';edited.view3D.visibleMaterialIds=[];
 edited.view3D.camera={position:[1000000,-1000000,1000000],focalPoint:[0,0,0],viewUp:[0,0,1],parallelScale:100000};
 await page.locator('input[type=file]').setInputFiles({name:'unrelated-editor.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify(edited))});
 await expect(page.getByRole('status')).toContainText('项目 JSON 已导入');await clickHistory(dim3);await assertShown3D();
 const evt=page.waitForEvent('download');await page.getByRole('button',{name:'导出项目',exact:true}).click();const download=await evt;const after=JSON.parse(await fs.readFile(await download.path(),'utf8'));
 expect(after.view3D.visibleMaterialIds).toEqual([]);expect(after.view3D.camera).toEqual(edited.view3D.camera);
 check('历史结果不继承或改写当前三维工程的空显隐列表与相机');
 await page.screenshot({path:path.join(out,'isolated-history-view.png'),fullPage:true});
 for(const [key,hash]of Object.entries(original))expect(await sha(path.join(root,'var/runs',key))).toBe(hash);
 expect(report.simulationSubmissions).toBe(0);expect(report.errors).toEqual([]);check('原始历史文件校验未变，未重新仿真',{files:Object.keys(original).length});
 report.status='passed';
}catch(e){report.status='failed';report.failure=String(e.stack??e);if(page)await page.screenshot({path:path.join(out,'failure.png'),fullPage:true}).catch(()=>{});console.error(e);process.exitCode=1;}
finally{if(browser)await browser.close();report.finishedAt=new Date().toISOString();await fs.writeFile(path.join(out,'acceptance.json'),JSON.stringify(report,null,2));}
