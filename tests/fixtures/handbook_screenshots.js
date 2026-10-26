async page => {
 const origin = 'http://127.0.0.1:8768';
 if(new URL(page.url()).origin !== origin) throw Error('Local synthetic fixture required');
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.request.post(origin+'/__fixture__/control',{data:{reset:true}});
 // Only fixture responses are changed. No production hosts or jobs are used.
 await page.route(origin+'/api/projects', async route=>{
  const data=await(await route.fetch()).json(),p=data.projects[0];
  p.releases=[{version:'1.0.0',release_id:'demo-1.0.0',created_at:'2026-10-26T12:00:00Z',test_status:'passed'}, {version:'0.9.0',release_id:'demo-0.9.0',created_at:'2026-10-25T12:00:00Z',test_status:'not_tested'}];
  p.release_rollouts={'1':{desired:{version:'1.0.0',release_id:'demo-1.0.0'},applied:{version:'1.0.0',release_id:'demo-1.0.0'},health:{status:'passed'}}};
  p.template_labels={...p.template_labels,'approved-base':'Debian 13','windows':'Windows Server 2025'};
  p.template_readiness={vm_templates:{'approved-base':{status:'ready',image_id:'demo-debian13',ready_nodes:['proxmox2']},windows:{status:'ready',image_id:'demo-windows',ready_nodes:['proxmox3']}},container_templates:{}};
  await route.fulfill({json:data});
 });
 const jobs=[{id:'a'.repeat(32),project:100,operation:'release-test-check',actor:'fixture-admin',status:'succeeded',created:1793016000,started:1793016002,finished:1793016090,output:'Demo: fresh-copy test completed.\nDefined checks passed; disposable copy ownership verified.\n'}];
 await page.route(origin+'/api/jobs**',async route=>{
  if(route.request().method()!=='GET') throw Error('Documentation must not submit jobs');
  const id=new URL(route.request().url()).pathname.match(/^\/api\/jobs\/([a-f0-9]{32})$/)?.[1];
  await route.fulfill({json:id?{job:jobs.find(j=>j.id===id)}:{jobs,total:jobs.length,page_count:1}});
 });
 await page.reload(); await page.setViewportSize({width:1440,height:1050});
 const nav = label => page.locator('.sidebar').getByRole('button',{name:label,exact:true});
 const shot = async name => {await page.locator('#content[aria-busy="false"]').waitFor(); await page.screenshot({path:'output/playwright/handbook-'+name+'.png',fullPage:true});};
 await nav('Project Overview').click(); await shot('overview');
 await nav('Scenario').click(); await page.getByRole('tab',{name:'Workspace',exact:true}).click(); await shot('workspace');
 await page.getByRole('button',{name:'Add VM',exact:true}).click(); await page.locator('#action-panel').waitFor({state:'visible'}); await page.locator('#job-fields [name=ip]').fill('20'); await shot('add-machine'); await page.locator('#close-action').click();
 await page.getByRole('tab',{name:'Releases',exact:true}).click(); await shot('releases');
 await page.getByRole('tab',{name:'Team deployments',exact:true}).click(); await shot('deployments');
 await page.getByRole('button',{name:'Choose a release',exact:true}).click(); await page.getByRole('button',{name:'1.0.0 · Ready to deploy',exact:true}).waitFor(); await shot('release-picker'); await page.keyboard.press('Escape');
 await nav('Machines').click(); await shot('machines');
 await page.locator('#content').getByRole('button',{name:'Staging application',exact:true}).click(); await shot('machine-detail');
 const editor=page.locator('.machine-health');
 if(await editor.count()) {await editor.getByLabel('Check 1 type',{exact:true}).selectOption('http'); await editor.getByLabel('Check 1 path',{exact:true}).fill('/health');}
 await shot('health-checks');
 await nav('Project users').click(); await shot('project-users');
 await nav('Jobs').click(); await shot('jobs');
 await nav('Scenario').click(); await page.getByRole('tab',{name:'Workspace',exact:true}).click(); await page.setViewportSize({width:390,height:900}); await shot('workspace-mobile');
 if(errors.length)throw Error(errors.join('\n'));
 return {screenshots:12,syntheticData:true,nativeJobs:0};
}
