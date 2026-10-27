async page => {
 const checks=[],check=(ok,label)=>{if(!ok)throw Error(label);checks.push(label)};
 const errors=[];page.on('pageerror',error=>errors.push(error.message));
 await page.goto('http://127.0.0.1:8789/');
 await page.evaluate(()=>{
  localStorage.setItem('crange.view','scenario');localStorage.setItem('crange.project','100');
  sessionStorage.clear();
  sessionStorage.setItem('crange.job-context',JSON.stringify([
   {id:'review',view:'scenario',scope:'workspace'}, {id:'deploy',view:'scenario',scope:'workspace'},
   {id:'capture',view:'scenario',scope:'releases'}, {id:'rollout',view:'scenario',scope:'deployments'}]));
  sessionStorage.setItem('crange.notified-job-notices',JSON.stringify(['review','deploy','capture']));
 });
 await page.reload();await page.locator('#scenario-tab-workspace').waitFor();
 await page.setViewportSize({width:1440,height:1000});
 const tab=id=>page.locator('#scenario-tab-'+id);
 check(await tab('workspace').getAttribute('data-notification')==='success','Workspace shows a green completed-task indicator');
 check(await tab('releases').getAttribute('data-notification')==='error','Releases shows a red failed-task indicator');
 check(await tab('deployments').getAttribute('data-notification')==='pending','Team deployments shows a blue active-task indicator');
 check(await tab('releases').getAttribute('aria-description').then(x=>x.includes('failed')),'Tab notification has an accessible status description');
 await page.screenshot({path:'output/playwright/scenario-notifications-desktop.png'});
 await page.getByRole('button',{name:'Dismiss Staging deployed notification',exact:true}).click();
 check(await page.locator('.scenario-deployment-good').count()===0,'Dismiss removes the completed deployment message and metadata');
 check(await page.locator('.scenario-deployment-actions').getByRole('button',{name:'Review changes',exact:true}).isVisible(),'Review changes remains available after dismissal');
 check(await tab('workspace').getAttribute('data-notification')===null,'Dismissing staging completion clears the Workspace indicator');
 check(await tab('releases').getAttribute('data-notification')==='error','Dismissing Workspace leaves the Releases notification intact');
 await page.reload();await tab('workspace').waitFor();
 check(await page.locator('.scenario-deployment-good').count()===0,'Dismissed completion stays hidden after reload');
 await page.getByRole('button',{name:'Add VM',exact:true}).click();
 const disk=page.locator('#job-fields [name="disk"]');
 check(await disk.isEnabled(),'Verified image enables custom disk sizing');
 check(await page.locator('.scenario-disk-profile-note').textContent().then(x=>x.includes('22 GiB')),'Sizing field shows the verified 22 GiB minimum');
 await page.locator('#close-action').click();
 await page.setViewportSize({width:390,height:844});
 check(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Scenario tab notifications fit a narrow viewport');
 await page.screenshot({path:'output/playwright/scenario-notifications-mobile.png'});
 await page.evaluate(()=>{
  const job={id:'new-review',project:100,operation:'stage-changes',status:'running',created:Date.now()/1000,started:Date.now()/1000,output:''};
  jobs.unshift(job);uiJobContexts.set(job.id,{view:'scenario',scope:'workspace'});render();
 });
 check(await tab('workspace').getAttribute('data-notification')==='pending','A new task shows a fresh indicator after dismissal');
 check(await page.locator('.scenario-deployment[aria-busy="true"]').isVisible(),'A new running task remains visible after dismissing an older completion');
 check(errors.length===0,'Scenario feedback has no JavaScript errors');
 return {checks,errors};
}
