"use strict";

const scenarioViews = new Map();
function scenarioState(p) {
 if (!scenarioViews.has(p.project_id)) scenarioViews.set(p.project_id,{tab:"workspace",query:""});
 return scenarioViews.get(p.project_id);
}
function scenarioButton(label, callback, className="quiet") {
 const button=el("button",label,className);button.type="button";button.onclick=callback;return button;
}
function scenarioJob(job,label="Open terminal") {
 return scenarioButton(label,()=>showJob(job.id,job.operation));
}
function scenarioForm(label,operation,values={},className="quiet") {
 const button=action(label,operation,className),open=button.onclick;
 button.onclick=async event=>{
  const bound=Object.keys(values),remaining=(fields[operation]||[]).filter(([key])=>!bound.includes(key));
  if(bound.length&&!remaining.length&&!["release-test-cleanup","capture-inspect"].includes(operation)){
   button.disabled=true;
   try{
    const result=await queueJob(operation,{...values,...(values.teams?{teams:values.teams.map(Number)}:{})});
    if(operation==="release-test-check")showJob(result.job_id,operation);
   }catch(_){/* queueJob shows the error. */}
   finally{button.disabled=false}return;
  }
  await open(event);
  for(const [key,value] of Object.entries(values)) {
   const input=$("job-form").elements.namedItem(key);
   if(input){input.value=Array.isArray(value)?value.join(", "):String(value);if(input.closest("label"))input.closest("label").hidden=true;if(input.tagName==="INPUT")input.type="hidden";}
  }
  if(values.version){
   $("action-title").textContent=`${label} · ${values.version}`;
   $("job-form").querySelector("button.primary").textContent=label;
  }
  if(operation==="release-test-cleanup")$("job-fields").append(el("p",`Remove the temporary test copies and networks for release ${values.version}. This job reviews and applies their removal automatically, stopping if the plan changes other resources.`,"muted"));
 };return workflowAction(button,operation,values);
}
function scenarioDate(value) {
 const date=new Date(value);return Number.isNaN(date.getTime())?"—":date.toLocaleString(undefined,{month:"short",day:"numeric",year:"numeric",hour:"2-digit",minute:"2-digit"});
}
function scenarioText(primary,secondary) {
 const cell=el("div",undefined,"scenario-cell");cell.append(el("span",primary));
 if(secondary)cell.append(el("small"," "+secondary,"muted"));return cell;
}
function scenarioRam(value) {
 return value>=1024?`${Number((value/1024).toFixed(2))} GiB`:`${value} MiB`;
}
function scenarioLatest(p,operations) {
 return jobs.find(job=>job.project===Number(p.project_id)&&operations.includes(job.operation));
}
function scenarioSelectTab(p,tab) {
 scenarioState(p).tab=tab;render();document.getElementById("scenario-tab-"+tab)?.focus();
}
function scenarioTabNoticeStates(projectId,jobList=jobs,contexts=uiJobContexts,dismissed=dismissedJobNotices,notified=notifiedJobNotices) {
 const states={};
 for(const scope of ["workspace","releases","deployments"]){
  const relevant=jobList.filter(job=>Number(job.project)===Number(projectId)&&contexts.get(job.id)?.view==="scenario"&&(contexts.get(job.id)?.scope||jobScope(job.operation))===scope).sort((a,b)=>b.created-a.created||b.id.localeCompare(a.id));
  const active=relevant.find(job=>["queued","running","waiting-for-node","reconcile-required"].includes(job.status)),recent=relevant[0];
  const job=active||(recent&&["succeeded","failed","interrupted"].includes(recent.status)&&notified.has(recent.id)&&!dismissed.has(recent.id)?recent:null);
  if(job){const needsAction=workflowJobActionRequired(job,jobList);states[scope]={tone:["failed","interrupted"].includes(job.status)?"error":needsAction?"action":active?"pending":"success",label:`${jobContextTitle(job)} · ${needsAction?"Action required":job.status}`}}
 }
 return states;
}
function updateScenarioTabNotifications(projectId=project()?.project_id) {
 const states=scenarioTabNoticeStates(projectId);
 for(const button of document.querySelectorAll(".scenario-tabs [data-scenario-tab]")){
  const marker=button.querySelector(".nav-status-dot"),state=states[button.dataset.scenarioTab];
  if(!state){marker?.remove();button.removeAttribute("data-notification");button.removeAttribute("aria-description");continue}
  const dot=marker||el("span",undefined,"nav-status-dot");dot.className=`nav-status-dot nav-status-${state.tone}`;dot.setAttribute("aria-hidden","true");if(!marker)button.append(dot);
  button.dataset.notification=state.tone;button.setAttribute("aria-description",state.label);
 }
}
function renderScenario(content,p) {
 refreshProjectRuntime(p);
 const state=scenarioState(p),workspace=el("div",undefined,"scenario-workspace");
 const tabs=el("div",undefined,"scenario-tabs");tabs.setAttribute("role","tablist");tabs.setAttribute("aria-label","Scenario views");
 const choices=[["workspace","Workspace"],["releases","Releases"],["deployments","Team deployments"]];
 for(const [key,label] of choices){
  const button=scenarioButton(label,()=>scenarioSelectTab(p,key),"scenario-tab");
  button.id="scenario-tab-"+key;button.setAttribute("role","tab");button.setAttribute("aria-selected",String(state.tab===key));
  button.dataset.scenarioTab=key;
  button.setAttribute("aria-controls","scenario-panel");button.tabIndex=state.tab===key?0:-1;
  button.onkeydown=event=>{const index=choices.findIndex(([id])=>id===key);let next;
   if(event.key==="ArrowRight")next=(index+1)%choices.length;
   if(event.key==="ArrowLeft")next=(index+choices.length-1)%choices.length;
   if(event.key==="Home")next=0;if(event.key==="End")next=choices.length-1;
   if(next!==undefined){event.preventDefault();scenarioSelectTab(p,choices[next][0])}
  };tabs.append(button);
 }
 const panel=el("section",undefined,"scenario-panel");panel.id="scenario-panel";panel.setAttribute("role","tabpanel");
 panel.setAttribute("aria-labelledby","scenario-tab-"+state.tab);
 workspace.append(tabs,panel);content.append(workspace);
 if(state.tab==="workspace")renderScenarioWorkspace(panel,p,state);
 if(state.tab==="releases")renderScenarioReleases(panel,p);
 if(state.tab==="deployments")renderScenarioDeployments(panel,p);
 updateScenarioTabNotifications(p.project_id);
}

function renderScenarioWorkspace(content,p,state) {
 const machines=[...[...Object.entries(p.scenario_boxes||{})].map(([key,box])=>({key,box,kind:"vm",label:"VM"})),
  ...Object.entries(p.scenario_containers||{}).map(([key,box])=>({key,box,kind:"container",label:"Container"}))].filter(item=>Number(item.box.team_id)===254);
 const heading=el("div",undefined,"scenario-heading"),intro=el("div");
 intro.append(el("h3","Staging"),el("p","Build and test your scenario here. Publish a release when it is ready for teams.","muted"));
 const tools=el("div",undefined,"scenario-controls");
 tools.append(scenarioValidation(p,machines.length===0),scenarioForm("Capture release","release-capture",{},"quiet"));
 heading.append(intro,tools);content.append(heading,scenarioDeployment(p,state));
 if(stagedPlan(p.project_id))content.append(scenarioPlanReview(p));
 content.append(contextJobHost(p,"workspace"));
 const toolbar=el("div",undefined,"scenario-toolbar"),title=el("h4",`Machines (${machines.length})`);
 const search=el("input");search.type="search";search.id="scenario-search";search.placeholder="Find a machine…";
 search.setAttribute("aria-label","Find a staging machine");search.value=state.query;
 const controls=el("div",undefined,"scenario-controls");controls.append(search,scenarioForm("Add VM","scenario-add",{},"primary"),scenarioForm("Add container","container-add"));
 toolbar.append(title,controls);content.append(toolbar);
 const tableHost=el("div",undefined,"scenario-machines");
 const draw=()=>{
  const query=state.query.trim().toLowerCase(),visible=machines.filter(item=>[item.key,item.label,templateDisplay(item.box.template,item.kind==="vm"?"vm_templates":"container_templates"),`10.${p.project_id}.${item.box.network_id??254}.${item.box.ip_last_octet}`].join(" ").toLowerCase().includes(query));
  tableHost.replaceChildren();
  if(!machines.length){const empty=el("div",undefined,"scenario-empty");empty.append(el("strong","Start with a machine"),el("p","Add a VM or container, choose an OS, and set its resources. Then review and deploy your changes."));tableHost.append(empty);return}
  if(!visible.length){const empty=el("div",undefined,"scenario-empty");empty.append(el("p","No machines match your search."),scenarioButton("Clear search",()=>{state.query="";search.value="";draw();search.focus()}));tableHost.append(empty);return}
  const rows=visible.map(item=>{const {key,box,kind,label}=item,live=machineRuntime(p,kind,key);
   const name=scenarioText(key,label+(box.release_scope==="test"?" · Release test":""));
   return {...item,cells:[name,templateDisplay(box.template,kind==="vm"?"vm_templates":"container_templates"),`10.${p.project_id}.${box.network_id??254}.${box.ip_last_octet}`,
    scenarioText(`${box.cores} ${box.cores===1?"core":"cores"}`,scenarioRam(box.memory_mb)),machineNode(p,kind,key,box),live.label]};
  });
  const machineList=machineTable(p,["Machine","OS","IP address","Resources","Node","Status"],rows,item=>{
   const {key,box,kind}=item;
   const edit={name:key,team_id:box.team_id,template:box.template,node:box.node_name,actual_node:appliedNode(p,kind,key,box),cores:box.cores,memory:box.memory_mb,disk:box.disk_gb??"",health:JSON.stringify(box.health_checks||[],null,2),guac_protocols:box.guac_services||[box.guac_protocol||"ssh"],ip:box.ip_last_octet,network:box.network_key||""};
   return [remoteAccessButton(kind,key,box),optionAction("Edit",kind==="vm"?"scenario-edit":"container-edit",edit),machinePowerAction(p,kind,key),
    optionAction("Validate",kind==="vm"?"scenario-health":"container-health",{name:key}),
    optionAction("Remove",kind==="vm"?"scenario-remove":"container-remove",{name:key},"quiet")];
  });
  const plan=stagedPlan(p.project_id),table=machineList.querySelector("table");
  if(table){const body=table.tBodies[0];for(const [index,item] of visible.entries()){const change=scenarioPlannedChange(plan,item.kind==="vm"?"scenario_box":"scenario_container",item.key)||scenarioSavedChange(p,item.kind==="vm"?"scenario_box":"scenario_container",item.key);if(change&&change!=="unknown")markScenarioRow(body.rows[index],change)}
   for(const item of pendingScenarioDeletions(plan,p)){const row=el("tr",undefined,"scenario-change-delete"),identity=el("td",undefined,"scenario-cell"),actions=el("td"),edit=el("button","Edit","quiet");identity.append(el("span",item.name),scenarioChangeMarker("delete"));actions.classList.add("row-actions");edit.type="button";edit.disabled=true;edit.title="This machine is queued for removal and cannot be edited until the reviewed changes are applied.";actions.append(edit);row.append(el("td"),identity,el("td",item.label),el("td",`10.${p.project_id}.${item.network??254}.${item.ip??"—"}`),el("td","—"),el("td",item.disk_gb?`${item.disk_gb} GiB`:"—"),el("td","—"),el("td","Pending removal"),actions);body.append(row)}
  }
  tableHost.append(machineList);
 };
 search.oninput=()=>{state.query=search.value;draw()};draw();content.append(tableHost);
 const runtime=projectRuntime.get(Number(p.project_id));
 if(runtime?.error)content.append(el("p","Live machine status could not be read. Refresh to try again.","scenario-warning"));
 renderScenarioNetworks(content,p);

}

function scenarioSavedChange(p,kind,name){const value=p.change_states?.[kind]?.[name];return ({added:"create",edited:"update",deleted:"delete"})[value]||""}
function scenarioPlanChangeKind(change){const value=String(change||"");if(/delete/i.test(value)&&/create/i.test(value))return "update";if(/delete/i.test(value))return "delete";if(/create/i.test(value))return "create";if(/update/i.test(value))return "update";return ""}
function scenarioPlannedChange(plan,type,name){if(!plan||plan.stale||plan.applied)return "";const escaped=String(name).replace(/[.*+?^${}()|[\]\\]/g,"\\$&"),pattern=new RegExp(`^module\\.${type}\\["${escaped}"\\]`);const item=plan.changes.find(row=>pattern.test(row.resource));return item?scenarioPlanChangeKind(item.change):""}
function pendingScenarioDeletions(plan,p){const rows=[],seen=new Set();for(const [kind,label] of [["scenario_box","VM"],["scenario_container","Container"]])for(const [name,state] of Object.entries(p.change_states?.[kind]||{}))if(state==="deleted"){const old=p.applied_definitions?.[kind]?.[name]||{};rows.push({name,kind:kind==="scenario_box"?"vm":"container",label,network:old.network_id??old.team_id,ip:old.ip_last_octet});seen.add(`${kind}:${name}`)}if(!plan||plan.stale||plan.applied)return rows;for(const item of plan.changes){if(scenarioPlanChangeKind(item.change)!=="delete")continue;const match=item.resource.match(/^module\.(scenario_box|scenario_container)\["([^"\]]+)"\]/);if(!match)continue;const kind=match[1]==="scenario_box"?"vm":"container",declared=(kind==="vm"?p.scenario_boxes:p.scenario_containers)?.[match[2]];if(declared||seen.has(`${match[1]}:${match[2]}`))continue;const address=item.resource;const applied=p.applied_placements?.[`${kind}:${match[2]}`]||{};rows.push({name:match[2],kind,label:kind==="vm"?"VM":"Container",network:applied.network_id,ip:applied.ip_last_octet,address})}return rows}
function pendingScenarioNetworkDeletions(plan,p){const names=new Set(Object.entries(p.change_states?.custom_network||{}).filter(([,state])=>state==="deleted").map(([name])=>name));if(plan&&!plan.stale&&!plan.applied)for(const item of plan.changes){const match=item.resource.match(/^module\.custom_router\["([^"\]]+)"\]/);if(match&&scenarioPlanChangeKind(item.change)==="delete")names.add(match[1])}return [...names].filter(name=>!(p.custom_networks||{})[name]).map(name=>({name,network:p.applied_definitions?.custom_network?.[name]})).filter(item=>item.network)}
function scenarioDisabledEdit(){const button=scenarioButton("Edit",()=>{},"quiet");button.disabled=true;button.title="This resource is queued for removal and cannot be edited until the reviewed changes are applied.";return button}
function scenarioChangeMarker(kind){const labels={create:"Added",update:"Edited",delete:"Pending deletion"},marker=el("span",labels[kind]||"Changed",`scenario-change-marker scenario-change-${kind}`);marker.setAttribute("aria-label",labels[kind]||"Changed");return marker}
function scenarioChangeCell(value){const cell=el("span",undefined,"scenario-change-cell");cell.append(el("span",value));const kind=scenarioPlanChangeKind(value);if(kind)cell.append(scenarioChangeMarker(kind));return cell}
function markScenarioRow(row,kind){if(!row||!kind)return;row.classList.add(`scenario-change-${kind}`);const first=row.cells?.[0];if(first&&!first.querySelector(".scenario-change-marker"))first.append(scenarioChangeMarker(kind))}

function scenarioValidation(p,empty) {
 const machines=workflowStagingMachines(p),deployed=!empty&&machines.length>0&&machines.every(item=>workflowMachineDeployed(p,item.kind,item.name));
 const button=scenarioButton("Run health checks",async()=>{
  button.disabled=true;
  try{for(const item of machines)await queueJob(item.kind==="vm"?"scenario-health":"container-health",{name:item.name},p.project_id)}
  catch(_){/* The job notification retains the error. */}
  finally{button.disabled=!deployed}
 });
 button.disabled=!deployed||jobs.some(j=>j.project===Number(p.project_id)&&["scenario-health","container-health"].includes(j.operation)&&["queued","running"].includes(j.status));
 button.dataset.help=button.title=deployed?"Run the declared health checks for staging machines.":"Machines must be deployed.";return button;
}
function workflowDeployAction(p,plan=stagedPlan(p.project_id),operation=view==="scenario"?"stage-deploy":"apply"){
 const destructive=plan?.changes.some(item=>item.change.includes("delete")&&!(plan.network_foundation?.length&&item.resource==="proxmox_sdn_applier.apply"));
 return destructive?scenarioForm("Deploy changes",operation,{plan_id:plan.id},"primary"):
  fixedAction("Deploy changes",plan?.changes.length?operation:"apply",{plan_id:plan?.id,allow_destroy:false,allow_replace:false},"primary",p.project_id);
}
function workflowReviewButton(p,operation=view==="scenario"?"stage-changes":"plan"){return scenarioButton("Review changes",()=>openWorkflowReview(p,operation))}
function openWorkflowReview(p,operation="stage-changes"){
 const previous=document.activeElement,d=resourceDialog("Review changes"),plan=stagedPlan(p.project_id),review=scenarioPlanReview(p);
 d.form.classList.add("scenario-test-plan-form");review.open=true;review.id="workflow-review-plan";d.dialog.setAttribute("aria-label","Review changes");d.form.append(review);d.form.onsubmit=event=>event.preventDefault();d.dialog.tabIndex=-1;
 const controls=el("div",undefined,"scenario-controls"),restage=scenarioForm("Restage changes",operation),deploy=workflowDeployAction(p,plan,operation==="plan"?"apply":"stage-deploy");
 for(const button of [restage,deploy]){const callback=button.onclick;button.onclick=event=>{d.overlay.remove();callback(event)}}
 controls.style.justifyContent="flex-end";controls.append(restage,deploy);d.form.append(controls);
 const close=()=>{d.overlay.remove();previous?.focus({preventScroll:true})};d.dialog.querySelector(".panel-heading button").onclick=close;
 d.dialog.addEventListener("keydown",event=>{if(event.key==="Escape"){event.preventDefault();close()}if(event.key==="Tab"){const buttons=[...d.dialog.querySelectorAll('button:not(:disabled),[href],input:not(:disabled),summary')];const first=buttons[0],last=buttons.at(-1);if(event.shiftKey&&document.activeElement===first){event.preventDefault();last?.focus()}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first?.focus()}}});d.dialog.focus();
}

function scenarioDeployment(p,state) {
 const plan=stagedPlan(p.project_id),latest=scenarioLatest(p,["stage-changes","plan","stage-deploy","apply"]);
 const busy=latest&&["queued","running","waiting-for-node","reconcile-required"].includes(latest.status);
 const failed=latest&&["failed","interrupted"].includes(latest.status);
 const ready=plan&&!plan.stale&&!plan.applied&&!plan.stagingBlocked;
 let title=workflowHasChanges(p)?"Changes ready to stage":"No pending changes",description=workflowHasChanges(p)?"Stage your changes, then review the infrastructure plan before deploying.":"Edit a machine or network to prepare an update.",tone=workflowHasChanges(p)?"action":"neutral";
 if(busy){title=["stage-changes","plan"].includes(latest.operation)?"Staging changes…":"Deploying staging…";description=jobPhase(latest);tone="info"}
 else if(failed){title=["stage-changes","plan"].includes(latest.operation)?"Staging failed":"Deployment needs attention";description=String(latest.output||"").split("\n").filter(line=>line.startsWith("crange:")).pop()?.replace(/^crange:\s*/,"")||"Open the job to see what failed, then review your changes again.";tone="bad"}
 else if(plan?.stagingBlocked){title="Changes outside staging";description="This plan includes team or shared resources. Review it in Project Overview.";tone="warning"}
 else if(plan?.applied){title=plan.network_foundation?.length?"Network deployed":"Staging deployed";description=plan.network_foundation?.length?"The network and router are deployed. Verify the network, then review changes again to deploy its machines.":"Run health checks, then capture a release. Review again after making changes.";tone="good"}
 else if(plan?.stale){title="Changes need restaging";description="The plan expired or the scenario changed. Review again before deploying.";tone="warning"}
 else if(ready){title=plan.changes.length?`${plan.changes.length} planned ${plan.changes.length===1?"change":"changes"}`:"No infrastructure changes";description=plan.changes.length?"Inspect the plan, then apply it to staging.":"Accept the reviewed settings to finish this update.";tone="action"}
 const noticeKey=tone==="good"?`scenario-deployed:${p.project_id}:${plan.id}`:!busy&&!failed&&plan?.stale&&!plan.stagingBlocked?`scenario-stale:${p.project_id}:${plan.id}:${p.desired_revision||""}`:null;
 if(noticeKey&&dismissedJobNotices.has(noticeKey)){
  const controls=el("div",undefined,"scenario-controls scenario-deployment-actions"),review=scenarioForm("Stage changes","stage-changes");
  controls.append(review);return controls;
 }
 const bar=el("section",undefined,`scenario-deployment scenario-deployment-${tone}`),body=el("div",undefined,"scenario-deployment-copy"),buttons=el("div",undefined,"scenario-controls");
 body.append(el("strong",title),el("p",description,"muted"));if(latest)body.append(jobMetadata(latest));bar.append(body,buttons);
 bar.dataset.deploymentJob=latest?.id||"";
 if(busy){bar.setAttribute("aria-busy","true");buttons.append(scenarioJob(latest));return bar}
 if(failed)buttons.append(scenarioJob(latest));
 const review=scenarioForm(ready||plan?.stale?"Restage changes":failed?"Retry staging":"Stage changes","stage-changes",{},ready?"quiet":"primary");buttons.append(review);
 if(ready&&!failed)buttons.prepend(workflowReviewButton(p));
 const deploy=workflowDeployAction(p,plan);if(failed){deploy.disabled=true;deploy.title=deploy.dataset.help="Restage changes before retrying deployment."}buttons.append(deploy);
 if(tone==="good")buttons.append(scenarioValidation(p,!workflowStagingMachines(p).length),scenarioForm("Capture release","release-capture"));
 if(noticeKey){
  const dismiss=scenarioButton("Dismiss",()=>{
   dismissJobNotice(noticeKey);
   for(const job of jobs)if(job.project===Number(p.project_id)&&["stage-changes","plan","stage-deploy","apply"].includes(job.operation)&&job.status==="succeeded"&&(job.plan_id===plan.id||(job.output||"").includes(`Plan ID: ${plan.id}`)))dismissJobNotice(job.id);
   render();document.querySelector(".scenario-deployment-actions button")?.focus({preventScroll:true});
  });
  dismiss.setAttribute("aria-label",`Dismiss ${title} notification`);buttons.append(dismiss);
 }
 return bar;
}

function scenarioPlanReview(p) {
 const plan=stagedPlan(p.project_id),section=rememberedDetails(`scenario-plan-${p.project_id}`,"Infrastructure changes");section.classList.add("scenario-plan");section.id="scenario-plan";
 const body=el("div",undefined,"scenario-plan-body");section.append(body);
 if(!plan){body.append(el("p","Stage changes to generate an infrastructure plan.","muted"));return section}
 body.append(el("p",`Reviewed ${scenarioDate(plan.created*1000)}. ${plan.stale?"This plan needs refreshing.":plan.applied?"This plan was applied.":"Valid for one hour, while the scenario remains unchanged."}`,"muted"));
 if(plan.changes.length){const changes=table(["Resource","Change"],plan.changes.map(item=>[resourceName(item.resource),scenarioChangeCell(item.change)]));
  for(const [index,item] of plan.changes.entries()){const kind=scenarioPlanChangeKind(item.change);if(kind)markScenarioRow(changes.querySelector("tbody").rows[index],kind)}body.append(changes)}
 else body.append(el("p","No machines or networks will be changed."));
 const placement=placementReview(p,plan.distribution||plan);
 if(placement){const details=rememberedDetails(`scenario-placement-${p.project_id}`,"Placement and capacity details");details.append(placement);body.append(details)}
 return section;
}

function renderScenarioNetworks(content,p) {
 const rows=Object.entries(p.custom_networks||{}).filter(([,network])=>Number(network.owner_team_id)===254);
 const section=el("section",undefined,"scenario-networks"),heading=el("div",undefined,"scenario-toolbar"),controls=el("div",undefined,"scenario-controls");
 const add=scenarioForm("Add network","network-add");add.dataset.noTooltip="true";add.setAttribute("aria-label","Add network");add.classList.add("icon-button");add.replaceChildren(actionIcon("plus"));controls.append(add,actionMenu("More actions",[scenarioForm("Verify network","network-verify")],{iconOnly:true,context:"Networks"}));heading.append(el("h4","Networks"),controls);section.append(heading);
 const plan=stagedPlan(p.project_id);
 const networks=[{name:"Staging network",standard:true,cells:["Staging network",`10.${p.project_id}.254.0/24`,`10.${p.project_id}.254.1`,"Default",appliedNode(p,"team","254",{})]},...rows.map(([name,network])=>({name,network,cells:[name,teamSubnet(p.project_id,network.network_id),`10.${p.project_id}.${network.network_id}.1`,"Custom",appliedNode(p,"network",name,network)]})),...pendingScenarioNetworkDeletions(plan,p).map(({name,network})=>({name,network,deleted:true,cells:[name,teamSubnet(p.project_id,network.network_id),`10.${p.project_id}.${network.network_id}.1`,"Custom","Pending removal"]}))];
 const networkTable=actionTable(["Network","Subnet","Gateway","Type","Router node"],networks,item=>item.standard?[]:item.deleted?[scenarioDisabledEdit()]:[
  optionAction("Edit","network-edit",{name:item.name,team_id:item.network.owner_team_id,id:item.network.network_id,node:item.network.node_name,actual_node:appliedNode(p,"network",item.name,item.network),uplink:item.network.uplink_last_octet,cores:item.network.cores??1,memory:item.network.memory_mb??512}),optionAction("Remove","network-remove",{name:item.name},"quiet")]);
 const table=networkTable.querySelector("table");if(table)for(const [index,item] of networks.entries()){if(item.standard)continue;const change=item.deleted?"delete":scenarioPlannedChange(plan,"custom_router",item.name)||scenarioSavedChange(p,"custom_network",item.name);if(change&&change!=="unknown")markScenarioRow(table.tBodies[0].rows[index],change)}
 section.append(networkTable);content.append(section);
}

function scenarioReleaseTestJob(p,release,operation="release-test-check"){
 const checks=jobs.filter(job=>Number(job.project)===Number(p.project_id)&&job.operation===operation&&
  (!(job.release_version||uiJobContexts.get(job.id)?.version)||(job.release_version||uiJobContexts.get(job.id)?.version)===release.version)&&
  (!release.test_prepared_at||job.created>=release.test_prepared_at)).sort((a,b)=>b.created-a.created);
 return checks.find(job=>["queued","running"].includes(job.status))||checks[0];
}
function scenarioTestFinished(p,release){
 if(!release.test_active)return false;
 const check=scenarioReleaseTestJob(p,release);
 if(check&&["queued","running"].includes(check.status))return false;
 return release.test_current_status==="passed"||Boolean(check&&["succeeded","failed","interrupted"].includes(check.status)&&
  release.test_prepared_at&&check.created>=release.test_prepared_at);
}

function renderScenarioReleases(content,p) {
 const releases=[...(p.releases||[])].sort((a,b)=>String(b.created_at).localeCompare(String(a.created_at)));
 const heading=el("div",undefined,"scenario-heading"),intro=el("div"),controls=el("div",undefined,"scenario-controls");
 intro.append(el("h3","Saved releases"),el("p","Capture staging, test fresh copies, then deploy the version you choose to teams.","muted"));
 controls.append(scenarioForm("Capture release","release-capture",{},"primary"),actionMenu("Release tools",[...(Number(p.project_id)===100?[scenarioForm("Publish service update","release-recipe")]:[]),scenarioForm("Inspect retained capture","capture-inspect")]));heading.append(intro,controls);content.append(heading,contextJobHost(p,"releases"));
 const active=releases.find(r=>r.test_active),cleanup=releases.filter(r=>r.cleanup_pending);
 const preparation=release=>jobs.find(job=>job.project===Number(p.project_id)&&['release-test-stage','release-test-deploy'].includes(job.operation)&&job.release_version===release.version&&['queued','running'].includes(job.status));
 scenarioMaybeShowTestPlan(p,releases);
 if(active||cleanup.length){
  const note=el("div",undefined,"scenario-release-notice"),preparing=active&&preparation(active),check=active&&scenarioReleaseTestJob(p,active),checking=check&&["queued","running"].includes(check.status);
  const removal=cleanup.map(release=>scenarioReleaseTestJob(p,release,"release-test-cleanup")).find(job=>job&&["queued","running"].includes(job.status));
  const message=!active?(removal?`Test cleanup ${removal.status}: ${jobPhase(removal)}`:`Test cleanup for ${cleanup.map(r=>r.version).join(", ")} still has ${cleanup.reduce((total,r)=>total+(r.cleanup_remaining||0),0)} test resources to remove. Finish cleanup to review and apply their removal automatically.`):
   preparing?`Release ${active.version}: ${jobPhase(preparing)}`:
   checking?`Release ${active.version} check ${check.status}: ${jobPhase(check)}`:
   active.test_current_status==="passed"?`Release ${active.version} test passed. Clean up its test copies before deploying to teams.`:
   scenarioTestFinished(p,active)?`Release ${active.version} test did not pass. Open the task output to review the failure, then retry after fixing it.`:
   active.test_ready?`Release ${active.version} has ${active.test_total} deployed test copies. Check them, then clean them up before rollout.`:
   active.test_review?`Release ${active.version}: review the test plan, then deploy its networks and machines.`:`Release ${active.version}: prepare the test plan to deploy its networks and machines.`;
  note.append(el("p",message),preparing?scenarioJob(preparing,"View test progress"):checking?scenarioJob(check,"View test progress"):removal?scenarioJob(removal,"View cleanup progress"):!active?scenarioForm("Finish cleanup","release-test-cleanup",{version:cleanup[0].version}):active.test_ready?scenarioForm("Check test","release-test-check",{version:active.version}):active.test_review?scenarioButton("Review test plan",()=>scenarioTestPlan(p,active)):scenarioPrepareTest(p,active,"Prepare test plan"));content.append(note);
 }
 if(!releases.length){const empty=el("div",undefined,"scenario-empty");empty.append(el("strong","No releases yet"),el("p","Deploy and validate your staging machines, then capture their configuration and disks as a versioned release."));content.append(empty);return}
 content.append(actionTable(["Version","Captured","Fresh-copy test","Deployed to"],releases.map(release=>{
  const teams=Object.entries(p.release_rollouts||{}).filter(([,r])=>r.applied?.release_id===release.release_id).map(([team])=>team);
  const test=el("div",undefined,"scenario-cell"),preparing=preparation(release),check=scenarioReleaseTestJob(p,release),checking=release.test_active&&check&&["queued","running"].includes(check.status);
  const label=release.test_active?(preparing?(preparing.operation==="release-test-stage"?"Reviewing test":"Deploying test"):checking?(check.status==="queued"?"Queued":"Checking"):release.test_current_status==="passed"?"Passed":scenarioTestFinished(p,release)?"Failed":release.test_ready?"Ready to check":release.test_review?"Ready to deploy test":"Awaiting deployment"):release.test_status==="passed"?"Passed":"Not tested";
  test.append(statusBadge(label)||el("span",label));
  if(release.test_active){test.append(el("small",`${release.test_deployed||0} / ${release.test_total||0} test machines deployed`,"muted"));if(release.test_status==="passed"&&release.test_current_status!=="passed")test.append(el("small","Earlier test passed","muted"));}
  else if(release.cleanup_pending){const removal=scenarioReleaseTestJob(p,release,"release-test-cleanup");test.append(el("small",removal&&["queued","running"].includes(removal.status)?"Removing test copies…":"Cleanup pending","scenario-warning"));}
  if(release.tested_at)test.title="Last passed: "+scenarioDate(release.tested_at);
  const deployed=el("span",undefined,"entity-links");teams.forEach(team=>deployed.append(teamLink(p,team)));return {release,cells:[entityLink(release.version,()=>{const button=scenarioForm("","release-show",{version:release.version});button.click()}),scenarioDate(release.created_at),test,teams.length?deployed:"—"]};
 }),({release})=>{
  const finished=scenarioTestFinished(p,release),check=scenarioReleaseTestJob(p,release),checking=release.test_active&&check&&["queued","running"].includes(check.status);
  const passed=release.test_current_status==="passed";
  const preparing=preparation(release);
  const test=preparing?scenarioJob(preparing,"View test progress"):checking?scenarioJob(check,"View test progress"):release.test_active&&!release.test_ready?(release.test_review?scenarioButton("Review test plan",()=>scenarioTestPlan(p,release)):scenarioPrepareTest(p,release,"Prepare test plan")):release.test_active?scenarioForm(finished&&!passed?"Retry test":"Check test","release-test-check",{version:release.version}):scenarioPrepareTest(p,release);
  test.disabled=!preparing&&!checking&&!release.test_active&&Boolean(active||cleanup.length);
  if(release.test_active&&!release.test_ready)test.dataset.help="Deploy all test machines in Workspace before checking this release.";
  const deploy=scenarioForm("Deploy to teams","release-deploy",{version:release.version});
  deploy.disabled=release.test_status!=="passed"||Boolean(active||cleanup.length);
  if(deploy.disabled)deploy.dataset.help="Pass a fresh-copy test and finish test cleanup before deploying a release.";
  const clean=scenarioForm("Clean up test","release-test-cleanup",{version:release.version},passed?"primary":"quiet");clean.disabled=!(release.test_active||release.cleanup_pending)||Boolean(checking)||Boolean(preparing);
  const inspect=scenarioForm("Inspect capture","capture-inspect",{version:release.version});
  return passed?[clean,test,deploy,inspect]:[test,deploy,clean,inspect];
 }));
}

function scenarioReleaseChooser(p) {
 const releases=[...(p.releases||[])].sort((a,b)=>String(b.created_at).localeCompare(String(a.created_at)));
 const pending=releases.some(release=>release.test_active||release.cleanup_pending);
 const choices=releases.map(release=>{
  const reason=pending?"Finish test cleanup first":release.test_status!=="passed"?"Fresh-copy test required":"Ready to deploy";
  const button=scenarioForm("Deploy release","release-deploy",{version:release.version});button.textContent=`${release.version} · ${reason}`;
  button.disabled=pending||release.test_status!=="passed";
  return button;
 });
 if(!choices.length){const empty=scenarioButton("No saved releases",()=>{});empty.disabled=true;choices.push(empty)}
 return actionMenu("Deploy release",choices,{iconOnly:false});
}

function renderScenarioDeployments(content,p) {
 const heading=el("div",undefined,"scenario-heading"),intro=el("div"),controls=el("div",undefined,"scenario-controls");
 intro.append(el("h3","Team deployments"),el("p","Choose a tested release and the teams to update. Review the rollout before applying it.","muted"));
 controls.append(scenarioReleaseChooser(p),actionMenu("Deployment tools",[
  scenarioForm("Stage changes","release-stage"),scenarioForm("Deploy changes","release-apply"),scenarioForm("Upgrade teams","release-upgrade"),scenarioForm("Roll back teams","release-rollback"),scenarioForm("Check team services","release-check")]));heading.append(intro,controls);content.append(heading,contextJobHost(p,"deployments"));
 const entries=Object.entries(p.release_rollouts||{}),pending=entries.filter(([,r])=>r.desired?.release_id!==r.applied?.release_id);
 if(pending.length){const notice=el("div",undefined,"scenario-release-notice"),teams=pending.map(([id])=>Number(id));notice.append(el("p",`${teams.length} ${teams.length===1?"team has":"teams have"} a release selected for deployment.`),scenarioForm("Stage changes","release-stage",{teams},"primary"),scenarioForm("Deploy changes","release-apply",{teams}));content.append(notice)}
 const teams=(p.team_ids||[]).map(String).sort((a,b)=>Number(a)-Number(b));
 if(!teams.length){content.append(el("p","Create teams from the Teams page before deploying a release.","scenario-empty"));return}
 content.append(actionTable(["Team","Selected release","Deployed release","Service checks"],teams.map(team=>{const record=p.release_rollouts?.[team]||{};return {team,record,cells:[teamLink(p,team),record.desired?.version?scenarioLink(p,record.desired.version,"releases"):"—",record.applied?.version?scenarioLink(p,record.applied.version,"releases"):"Not deployed",record.health?.status==="passed"?"Passed":record.health?.status||"Not checked"]}}),({team,record})=>{
  const buttons=[scenarioForm("Check services","release-check",{teams:[team]})];buttons[0].disabled=!record.applied?.release_id;
  if(record.applied?.release_id&&record.applied.release_id===record.desired?.release_id&&record.health?.status!=="passed")buttons.unshift(fixedAction("Continue configuration","release-configure",{teams:[Number(team)]},"quiet"));
  if(record.desired?.release_id)buttons.push(scenarioForm("Upgrade","release-upgrade",{teams:[team]}),scenarioForm("Roll back","release-rollback",{teams:[team]}));
  return buttons;
 }));
 const events=entries.flatMap(([team,r])=>(r.history||[]).map(event=>({team,...event}))).sort((a,b)=>String(b.at).localeCompare(String(a.at)));
 if(events.length){const history=rememberedDetails(`scenario-history-${p.project_id}`,"Deployment history");history.append(table(["When","Team","Release","Action","Result"],events.map(event=>[scenarioDate(event.at),event.team,event.version,event.event,event.status||event.action||"—"])));content.append(history)}
}

const pendingTestReviews=new Set(),openedTestReviews=new Set();
function scenarioPrepareTest(p,release,label="Test release"){
 const button=scenarioButton(label,async()=>{
  const key=`${p.project_id}:${release.version}`;
  if(pendingTestReviews.has(key)||jobs.some(job=>job.project===Number(p.project_id)&&job.operation==='release-test-stage'&&job.release_version===release.version&&['queued','running'].includes(job.status)))return;
  pendingTestReviews.add(key);button.disabled=true;
  try{const result=await queueJob('release-test-stage',{version:release.version},Number(p.project_id));showJob(result.job_id,'release-test-stage')}
  catch(_){pendingTestReviews.delete(key)}finally{button.disabled=false}
 });return button;
}
function scenarioTestPlan(p,release){
 const review=release.test_review;
 if(!review)return;
 const returnFocus=document.activeElement,d=resourceDialog(`Test plan · ${release.version}`);d.dialog.setAttribute('aria-label',`Test plan · ${release.version}`);d.dialog.tabIndex=-1;d.form.classList.add('scenario-test-plan-form');
 d.form.append(el('p',`Review the disposable resources for release ${release.version}. Deploy test creates its isolated networks, checks their paths, then deploys these fresh guests.`));
 const guests=table(['Machine','Type','IP address','CPU / RAM','Node'],review.machines.map(machine=>[
  machine.name,machine.kind,machine.address,`${machine.cores} cores / ${scenarioRam(machine.memory_mb)}`,machine.node==='auto'?'Automatic':machine.node
 ]));guests.classList.add('scenario-test-guests');d.form.append(el('h4','Test guests'),guests);
 if(review.networks.length){const networks=table(['Network','Subnet'],review.networks.map(network=>[network.name,`10.${p.project_id}.${network.network_id}.0/24`]));networks.classList.add('scenario-test-networks');d.form.append(el('h4','Isolated test networks'),networks)}
 const plan=el('details');plan.open=true;plan.append(el('summary','Network acceptance and guest deployment'));
 if(review.foundation.length)plan.append(el('p',`The job prepares and verifies ${review.foundation.length} network foundation resource${review.foundation.length===1?'':'s'} before guest deployment.`,'muted'));
 if(review.changes.length)plan.append(table(['Resource','Planned actions'],review.changes.map(change=>[resourceName(change.address),change.actions.join(' → ')])));
 plan.append(el('p',review.foundation.length?'Deploy test runs network acceptance, reviews the guest plan, and applies the guests after that plan is safe.':'Deploy test checks the network paths, reviews the guest plan, and applies the guests after that plan is safe.','muted'));
 plan.append(el('p','The job stops if a later plan changes resources outside this release test. Check test stays a separate action after deployment.','muted'));d.form.append(plan);
 const close=()=>{d.overlay.remove();if(returnFocus?.isConnected)returnFocus.focus({preventScroll:true})};
 d.overlay.querySelector('.panel-heading button').onclick=close;
 d.overlay.addEventListener('keydown',event=>{
  if(event.key==='Escape'){event.preventDefault();close();return}
  if(event.key!=='Tab')return;
  const focusable=[...d.dialog.querySelectorAll('button:not(:disabled),[href],input:not(:disabled),select:not(:disabled),textarea:not(:disabled),[tabindex]:not([tabindex="-1"])')].filter(item=>!item.hidden);
  if(!focusable.length){event.preventDefault();d.dialog.focus();return}
  const first=focusable[0],last=focusable.at(-1);
  if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus()}
  else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus()}
 });
 if(review.stale){d.error.textContent='This review expired or the release definitions changed. Refresh the review before deploying.';const refresh=resourceSubmit(d.form,'Refresh review');d.form.onsubmit=event=>{event.preventDefault();refresh.disabled=true;pendingTestReviews.delete(`${p.project_id}:${release.version}`);close();scenarioPrepareTest(p,release,'Refresh review').click()};refresh.focus();return}
 const submit=resourceSubmit(d.form,'Deploy test');
 d.form.onsubmit=async event=>{event.preventDefault();submit.disabled=true;try{
  const result=await queueJob('release-test-deploy',{version:release.version,review_id:review.review_id},Number(p.project_id));close();showJob(result.job_id,'release-test-deploy');
 }catch(error){d.error.textContent=error.message;submit.disabled=false}};
 submit.focus();
}
function scenarioMaybeShowTestPlan(p,releases){
 for(const release of releases){
  const key=`${p.project_id}:${release.version}`,review=release.test_review;
  const job=jobs.find(job=>job.project===Number(p.project_id)&&job.operation==='release-test-stage'&&(job.release_version||uiJobContexts.get(job.id)?.version)===release.version);
  if(job&&['failed','interrupted'].includes(job.status))pendingTestReviews.delete(key);
  if(!pendingTestReviews.has(key)||!review||review.phase!=='reviewed'||openedTestReviews.has(review.review_id))continue;
  if(job?.status!=='succeeded')continue;
  pendingTestReviews.delete(key);openedTestReviews.add(review.review_id);
  queueMicrotask(()=>{if(view!=='scenario'||scenarioState(p).tab!=='releases')return;$('job-overlay').hidden=true;scenarioTestPlan(p,release)});
 }
}
