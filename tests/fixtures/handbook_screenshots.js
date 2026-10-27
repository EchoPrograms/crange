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
  p.template_readiness={vm_templates:{'approved-base':{status:'ready',image_id:'demo-debian13',system_disk_gb:32,ready_nodes:['proxmox2']},windows:{status:'ready',image_id:'demo-windows',system_disk_gb:64,ready_nodes:['proxmox3']}},container_templates:{}};
  p.desired_revision='b'.repeat(64);
  p.change_states={scenario_box:{'staging-app':'edited','web-02':'added'}};
  p.scenario_boxes={...p.scenario_boxes,'web-02':{name:'Scenario web server',team_id:254,ip_last_octet:31,node_name:'auto',cores:2,memory_mb:4096,disk_gb:48,template:'approved-base',health_checks:[]}};
  const planId='c'.repeat(32),revision='b'.repeat(64),created=Math.floor(Date.now()/1000)-45;
  p.staged_plans=[{plan_id:planId,created,changes:[]}];
  await route.fulfill({json:data});
 });
 const now=Math.floor(Date.now()/1000),planId='c'.repeat(32),revision='b'.repeat(64);
 const jobs=[{id:planId,project:100,operation:'plan',actor:'fixture-admin',status:'succeeded',created:now-45,started:now-44,finished:now-40,output:`Plan ID: ${planId}\nInputs SHA-256: ${revision}\n  module.custom_router["staging"]: update\n  proxmox_sdn_vnet.custom["p100t110"]: create\n`},
  {id:'a'.repeat(32),project:100,operation:'release-test-check',actor:'fixture-admin',status:'succeeded',created:now-30,started:now-28,finished:now-5,output:'Demo: fresh-copy test completed.\nDefined checks passed; disposable copy ownership verified.\n'}];
 await page.route(origin+'/api/jobs**',async route=>{
  if(route.request().method()!=='GET') throw Error('Documentation must not submit jobs');
  const id=new URL(route.request().url()).pathname.match(/^\/api\/jobs\/([a-f0-9]{32})$/)?.[1];
  await route.fulfill({json:id?{job:jobs.find(j=>j.id===id)}:{jobs,total:jobs.length,page_count:1}});
 });
 await page.evaluate(({id})=>{sessionStorage.setItem('crange.job-context',JSON.stringify([{id,operation:'plan',project:100,view:'project-overview'}]));sessionStorage.setItem('crange.notified-job-notices',JSON.stringify([id]))},{id:'c'.repeat(32)});
 await page.reload(); await page.setViewportSize({width:1440,height:1050});
 const nav = label => page.locator('.sidebar').getByRole('button',{name:label,exact:true});
 const shot = async name => {await page.locator('#content[aria-busy="false"]').waitFor(); await page.screenshot({path:'output/playwright/handbook-'+name+'.png',fullPage:true});};
 await nav('Project Overview').click(); await shot('overview');
 await page.getByRole('button',{name:'Project Overview',exact:true}).getAttribute('data-notification').then(value=>{if(value!=='success')throw Error(`Expected reviewed-plan sidebar indicator; got ${value}`)});
 await page.locator('.project-infrastructure .infrastructure-plan-review').waitFor();
 await nav('Scenario').click(); await page.getByRole('tab',{name:'Workspace',exact:true}).click(); await shot('workspace');
 await page.locator('.scenario-machines .scenario-change-marker').first().waitFor();
 await page.getByRole('button',{name:'Add VM',exact:true}).click(); await page.locator('#action-panel').waitFor({state:'visible'}); await page.getByLabel('Name',{exact:true}).fill('web-docs'); await page.getByLabel('IP address (last number)',{exact:true}).fill('20'); await page.getByLabel('System disk (GiB, minimum is image size)',{exact:true}).fill('48');
 if(await page.getByLabel('Name',{exact:true}).inputValue()!=='web-docs'||await page.getByLabel('IP address (last number)',{exact:true}).inputValue()!=='20'||await page.getByLabel('System disk (GiB, minimum is image size)',{exact:true}).inputValue()!=='48')throw Error('Add VM fields did not retain the documented values');
 await page.locator('#action-title').click(); await page.locator('#action-panel').screenshot({path:'output/playwright/handbook-add-machine.png'}); await page.locator('#close-action').click();
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
 return {screenshots:12,syntheticData:true,nativeJobs:0,checks:['disk sizing field','scenario row change markers','project infrastructure review','sidebar completion indicator']};
}
