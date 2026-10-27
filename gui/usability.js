"use strict";

function projectResourceSummary(p) {
 const usage=p.quota_usage||p.usage||{};
 return `${Number(usage.cores||0).toLocaleString()} / ${Number(p.quota?.cores||0).toLocaleString()} cores · ${scenarioRam(usage.memory_mb||0)} / ${scenarioRam(p.quota?.memory_mb||0)} RAM`;
}
function renderPlatformOverview(content) {
 const heading=el("div",undefined,"scenario-heading"),intro=el("div");
 intro.append(el("h3","Your projects"),el("p","Open a scenario, manage teams, or check the latest project activity.","muted"));
 heading.append(intro);if(user.admin)heading.append(action("Create project","project-init","primary"));content.append(heading);
 if(!projects.length){content.append(el("p","Create a project to start building an isolated range.","scenario-empty"));return}
 content.append(actionTable(["Project","Teams","Allocated resources","Latest job"],projects.map(p=>{
  const job=jobs.find(j=>j.project===Number(p.project_id)),latest=job?scenarioText(job.operation.replaceAll("-"," "),job.status):"No recent jobs";
  return {p,cells:[projectDisplay(p),(p.team_ids||[]).length,projectResourceSummary(p),latest]};
 }),({p})=>[navigate("Open scenario","scenario",p.project_id,"primary"),navigate("Teams","teams",p.project_id),navigate("Project overview","project-overview",p.project_id)]));
 const active=jobs.filter(j=>["queued","running","waiting-for-node","reconcile-required"].includes(j.status));
 const activity=el("div",undefined,"scenario-toolbar platform-activity-heading");activity.append(el("h3",active.length?`Active jobs (${active.length})`:"Recent activity"),navigate("All jobs","jobs"));content.append(activity);
 const recent=active.length?active:jobs.slice(0,5);
 if(!recent.length)content.append(el("p","No recent jobs. Actions and their results will appear here.","muted"));
 else content.append(actionTable(["Action","Project","Status","Started by","Runtime","Created"],recent.map(job=>({job,cells:[job.operation.replaceAll("-"," "),job.project?`p${String(job.project).padStart(3,"0")}`:"Platform",job.status,job.actor||"Unknown",jobRuntimeNode(job),scenarioDate(job.created*1000)]})),({job})=>[scenarioJob(job,"View output")]));
}

function renderProjectOverview(content,p) {
 const heading=el("div",undefined,"scenario-toolbar project-overview-toolbar"),controls=el("div",undefined,"scenario-controls");
 controls.append(navigate("Open scenario","scenario",p.project_id,"primary"),navigate("Manage teams","teams",p.project_id),projectUsersButton(p),projectJobsButton(p));if(user?.admin&&Number(p.project_id)===100)controls.append(actionMenu("More actions",[fixedAction("Review removal of deployed team machines","deployment-clear-review",{},"quiet",100)],{iconOnly:true,context:"Project deployment"}));heading.append(controls);content.append(heading);
 const teams=p.team_ids||[],machines=[...Object.values(p.scenario_boxes||{}),...Object.values(p.scenario_containers||{})];
 content.append(stats([["Teams",teams.length],["Staging machines",machines.filter(b=>Number(b.team_id)===254).length],["Team machines",machines.filter(b=>Number(b.team_id)!==254).length],["Jump boxes",Object.keys(p.jump_boxes||{}).length]]));
 content.append(el("h3","Resource allocation"),el("p","Declared resources, including project, team, and custom routers.","muted"));
 const usage=p.quota_usage||p.usage||{},quota=p.quota||{};
 content.append(table(["Resource","Allocated","Limit"],[["CPU",`${usage.cores??0} cores`,`${quota.cores??"—"} cores`],["RAM",scenarioRam(usage.memory_mb??0),scenarioRam(quota.memory_mb??0)]]));
 const rollouts=Object.entries(p.release_rollouts||{});
 if(rollouts.length){const title=el("div",undefined,"scenario-toolbar");title.append(el("h3","Team releases"),scenarioButton("Manage deployments",()=>{scenarioState(p).tab="deployments";navigate("","scenario",p.project_id).click()}));content.append(title,table(["Team","Selected","Deployed","Service checks"],rollouts.map(([team,r])=>[teamLink(p,team),r.desired?.version?scenarioLink(p,r.desired.version,"releases"):"—",r.applied?.version?scenarioLink(p,r.applied.version,"releases"):"Not deployed",r.health?.status==="passed"?"Passed":r.health?.status?.replaceAll("-"," ")||"Not checked"])))}
 const empty=!p.has_state,foundation=p.bootstrap_status;
 const infrastructure=el("section",undefined,"project-infrastructure");
 infrastructure.append(el("h3",empty?"Set up project infrastructure":"Infrastructure and network tools"));
 infrastructure.append(el("p",empty?"Create the project routers and networks before deploying machines.":"Review project-wide router and network changes here. Use Scenario for staging machines.","muted"));
 if(foundation){const receipt=el("details",undefined,"foundation-record");receipt.append(el("summary","Last foundation setup record"),el("p",`${String(foundation.state).replaceAll("_"," ")}. This is a setup record; verify current network readiness before deploying to a new subnet or node.`,"muted"));infrastructure.append(receipt)}
 const plan=stagedPlan(p.project_id),pending=infrastructurePendingState(p.project_id);
 const latestInfrastructure=jobs.filter(job=>job.project===Number(p.project_id)&&["plan","apply","stage-changes","stage-deploy","bootstrap-plan","bootstrap-apply"].includes(job.operation)).sort((a,b)=>b.created-a.created)[0];
 if(pending||latestInfrastructure&&["queued","running","waiting-for-node","reconcile-required"].includes(latestInfrastructure.status)){
  const status=el("div",undefined,`infrastructure-pending infrastructure-pending-${latestInfrastructure&&["queued","running","waiting-for-node","reconcile-required"].includes(latestInfrastructure.status)?"progress":pending?.state||"apply"}`);
  status.setAttribute("role","status");
  if(latestInfrastructure&&["queued","running","waiting-for-node","reconcile-required"].includes(latestInfrastructure.status))status.append(el("strong",`${latestInfrastructure.operation.replaceAll("-"," ")} · ${latestInfrastructure.status}`),el("p",jobPhase(latestInfrastructure)));
  else status.append(el("strong",pending.label),el("p",pending.detail));
  infrastructure.append(status);
 }
 if(plan&&!plan.applied&&plan.changes.some(item=>item.change!=="no-op")){
  const reviewed=el("div",undefined,"infrastructure-plan-review");
  reviewed.append(el("h4","Reviewed infrastructure plan"),table(["Resource","Change"],plan.changes.filter(item=>item.change!=="no-op").map(item=>[resourceName(item.resource),item.change])));
  if(plan.stale)reviewed.append(el("p","This plan is stale. Stage changes again before deploying.","muted"));
  else if(plan.stagingBlocked)reviewed.append(el("p","This plan includes staging changes. Review it from Scenario to avoid applying unrelated infrastructure changes.","muted"));
  else reviewed.append(workflowReviewButton(p),action("Restage changes","plan"),action("Deploy changes", "apply", "primary"));
  infrastructure.append(reviewed);
 }
 const primary=empty?[action("Stage changes","bootstrap-plan","primary")]:[action("Stage changes","plan","primary")];
 const tools=[action("Generate inventory","inventory"),action("Reconcile applied reservations","reconcile-project-reservations"),action("Configure routers and boxes","configure"),action("Configure Windows guests","configure"),action("Verify network","network-verify")];
 if(Number(p.project_id)===100)tools.push(...stagingDomainActions());
 if(foundation&&["configuration_failed","configuration_pending","awaiting_network_acceptance"].includes(foundation.state))tools.push(action("Retry router configuration","bootstrap-configure"));
 infrastructure.append(workflowToolbar(primary,[actionMenu("Project tools",tools)]));content.append(infrastructure);
}

function stagingDomainActions() {
 const pending=jobs.some(job=>job.project===100&&job.operation.startsWith("mhl-domain-")&&["queued","running"].includes(job.status));
 const controls=[fixedAction("Review staging domain migration","mhl-domain-review",{})];
 const reportOf=job=>{try{const value=JSON.parse(job.output.slice(job.output.indexOf("{")));return value.review||value}catch(_){return null}};
 const progressed=jobs.find(job=>["mhl-domain-prepare","mhl-domain-backup","mhl-domain-execute"].includes(job.operation)&&job.project===100&&job.status==="succeeded"&&reportOf(job)?.plan);
 const progress=progressed&&reportOf(progressed);
 const latest=jobs.find(job=>["mhl-domain-review","mhl-domain-tools"].includes(job.operation)&&job.project===100&&job.status==="succeeded");
 const report=latest&&reportOf(latest);
 const needsTools=report&&/^[a-f0-9]{64}$/.test(report.review_sha256)&&report.current_domain==="mhl.test"&&report.prerequisite_blockers?.includes("Install reviewed AD/GPO administration tools on the member-server control station.");
 if(progress&&/^[a-f0-9]{64}$/.test(progress.plan.target_sha256)){
  const options={plan_sha256:progress.plan.target_sha256};
  if(progress.phase!=="domain_accepted"){
   if(needsTools)controls.push(fixedAction("Prepare domain migration tools","mhl-domain-tools",{review_sha256:report.review_sha256}));
   else if(progress.phase==="instructions_generated")controls.push(fixedAction("Back up staging domain for migration","mhl-domain-backup",options));
   else controls.push(fixedAction(jobs.some(job=>job.project===100&&job.operation==="mhl-domain-execute")?"Continue staging domain migration":"Rename staging domain to mhl.local","mhl-domain-execute",options));
  }
 }else{
  if(report&&/^[a-f0-9]{64}$/.test(report.review_sha256)){
   if(report.prerequisite_blockers?.includes("Install reviewed AD/GPO administration tools on the member-server control station."))controls.push(fixedAction("Prepare domain migration tools","mhl-domain-tools",{review_sha256:report.review_sha256}));
   else if(report.current_domain==="mhl.test"&&report.prerequisite_blockers?.length===0)controls.push(fixedAction("Prepare staging domain migration","mhl-domain-prepare",{review_sha256:report.review_sha256}));
  }
 }
 if(pending)for(const control of controls){control.disabled=true;control.title="A staging domain migration task is running."}
 return controls;
}

function enhanceScenarioForm() {
 if(!["scenario-add","scenario-edit","container-add","container-edit"].includes(activeOperation))return;
 const form=$("job-form"),fieldsHost=$("job-fields"),health=form.elements.health;
 if(!health||fieldsHost.querySelector(".health-editor"))return;
 const originalLabel=health.closest("label"),originalHelp=originalLabel?.nextElementSibling;
 if(originalHelp?.tagName==="P")originalHelp.remove();originalLabel.hidden=true;
 const editor=el("fieldset",undefined,"health-editor"),legend=el("legend","Service health checks"),list=el("div",undefined,"health-rows");
 editor.append(legend,el("p","Check that the machine and its services are ready. With no custom checks, CRANGE checks ping.","muted"),list);
 let checks=[];try{checks=JSON.parse(health.value||"[]")}catch(_){originalLabel.hidden=false;return}
 const entries=[];
 const sync=()=>{health.value=JSON.stringify(entries.map(entry=>{
  const result={type:entry.type.value,timeout:Number(entry.timeout.value)};
  if(result.type!=="ping")result.port=Number(entry.port.value);
  if(result.type==="http"){result.path=entry.path.value;result.status=Number(entry.status.value);if(entry.host.value.trim())result.host=entry.host.value.trim()}
  return result;
 }))};
 const add=(check={type:"tcp",port:22,timeout:10})=>{
  const row=el("div",undefined,"health-row"),entry={row};
  const field=(name,label,value,type="number",min,max)=>{const wrapper=el("label",label),input=el("input");input.type=type;input.value=value;input.required=true;if(min!==undefined)input.min=min;if(max!==undefined)input.max=max;input.oninput=sync;wrapper.append(input);row.append(wrapper);entry[name]=input;return input};
  const typeLabel=el("label","Check"),type=el("select");for(const [value,label] of [["ping","Ping"],["tcp","TCP port"],["http","HTTP response"]])type.add(new Option(label,value));type.value=check.type;entry.type=type;typeLabel.append(type);row.append(typeLabel);
  field("port","Port",check.port??22,"number",1,65535);
  field("timeout","Timeout (s)",check.timeout??(check.type==="ping"?5:10),"number",1,60);
  const host=field("host","HTTP hostname",check.host??"","text");host.required=false;host.maxLength=253;host.placeholder="Optional hostname";
  field("path","HTTP path",check.path??"/","text").maxLength=2048;
  field("status","HTTP status",check.status??200,"number",100,599);
  const remove=scenarioButton("Remove check",()=>{entries.splice(entries.indexOf(entry),1);row.remove();sync();addButton.focus()});row.append(remove);
  const update=()=>{for(const name of ["port","host","path","status"]){const visible=name==="port"?type.value!=="ping":type.value==="http";entry[name].disabled=!visible;entry[name].closest("label").hidden=!visible}sync()};
  type.onchange=()=>{if(type.value==="http"&&entry.port.value==="22")entry.port.value="80";update()};
  entries.push(entry);list.append(row);update();
 };
 const addButton=scenarioButton("Add health check",()=>add());editor.append(addButton);
 for(const check of checks)add(check);fieldsHost.append(editor);
 const ram=form.elements.memory;if(ram){const label=ram.closest("label"),hint=el("small",undefined,"muted");label.firstChild.textContent="RAM (MiB)";ram.setAttribute("aria-label","RAM (MiB)");hint.id="machine-ram-equivalent";ram.setAttribute("aria-describedby",hint.id);label.append(hint);const update=()=>hint.textContent=`${Number(ram.value||0)/1024} GiB`;ram.addEventListener("input",update);update()}
 const ip=form.elements.ip,network=form.elements.network;
 if(ip&&network){const label=ip.closest("label"),hint=el("small",undefined,"muted");label.firstChild.textContent="IP address (last number)";ip.setAttribute("aria-label","IP address (last number)");hint.id="machine-ip-preview";ip.setAttribute("aria-describedby",hint.id);label.append(hint);
  const update=()=>{const p=project(),subnet=network.value?p.custom_networks?.[network.value]?.network_id:actionValues.team_id??254;hint.textContent=`10.${p.project_id}.${subnet}.${ip.value||"…"}`};ip.addEventListener("input",update);network.addEventListener("change",update);update();
 }
 const preferred=form.elements.preferred_node;
 if(preferred){const options=el("details",undefined,"machine-placement-options");options.append(el("summary","Placement preference (optional)"),preferred.closest("label"));fieldsHost.append(options);
  const hint=[...fieldsHost.children].find(node=>node.tagName==="P"&&node.textContent.startsWith("Suggested nodes are optional."));if(hint)options.append(hint);
 }
 // Use explicit checkboxes for remote protocols; the submitted values are unchanged.
 const services=form.elements.guac_protocols;
 if(services?.tagName==="SELECT"){
  const group=el("fieldset",undefined,"protocol-choices");group.append(el("legend","Remote access"));
  for(const option of services.options){const label=el("label"),input=el("input");input.type="checkbox";input.name="guac_protocols";input.value=option.value;input.checked=option.selected;label.append(input,document.createTextNode(option.text));group.append(label)}
  services.closest("label").replaceWith(group);
 }
}

// Keep operation feedback beside the work that started it. Store identifiers
// and display context only; credentials and arbitrary form values stay out.
const uiJobContexts=new Map();
try{for(const item of JSON.parse(sessionStorage.getItem("crange.job-context")||"[]"))uiJobContexts.set(item.id,item)}catch(_){}
const dismissedJobNotices=new Set();
const notifiedJobNotices=new Set();
try{for(const id of JSON.parse(sessionStorage.getItem("crange.notified-job-notices")||"[]"))notifiedJobNotices.add(id)}catch(_){}
try{for(const id of JSON.parse(sessionStorage.getItem("crange.dismissed-job-notices")||"[]"))dismissedJobNotices.add(id)}catch(_){}
function dismissJobNotice(id){
 dismissedJobNotices.add(id);
 while(dismissedJobNotices.size>100)dismissedJobNotices.delete(dismissedJobNotices.values().next().value);
 try{sessionStorage.setItem("crange.dismissed-job-notices",JSON.stringify([...dismissedJobNotices]))}catch(_){}
 refreshContextJobProgress();
 if(typeof updateProjectNavigationNotifications==="function")updateProjectNavigationNotifications();
}
const deploymentOperations=new Set(["stage-changes","plan","stage-deploy","apply"]);
function jobScope(operation){
 if(["release-stage","release-apply","release-deploy","release-upgrade","release-rollback","release-configure","release-check"].includes(operation))return "deployments";
 if(operation.startsWith("release-")||operation==="capture-inspect")return "releases";
 return "workspace";
}
function rememberUIJob(id,operation,options,projectId){
 const context={id,operation,project:Number(projectId),view,scope:view==="scenario"?jobScope(operation):null,version:options.version,teams:options.teams,name:options.name,resource_key:options.resource_key,action:options.action};
 uiJobContexts.set(id,context);
 while(uiJobContexts.size>60)uiJobContexts.delete(uiJobContexts.keys().next().value);
 try{sessionStorage.setItem("crange.job-context",JSON.stringify([...uiJobContexts.values()]))}catch(_){}
 observedStatuses.set(id,"queued");
 observedActiveJobs.set(id,{id,operation,project:Number(projectId),status:"queued",output:"",created:Date.now()/1000});
 // Move to the area containing the feedback, including actions started in a
 // different Scenario tab (capture and release selection, for example).
 if(view==="scenario"&&project())scenarioState(project()).tab=context.scope;
}
function jobQueueLane(job){return job.operation==="box-users"?"guest accounts":["guac-users","guac-refresh"].includes(job.operation)?"accounts":["guac-box-control","box-state","machine-state"].includes(job.operation)?"power":"infrastructure"}
function jobPhase(job){
 if(job.status==="queued"){
  const lane=jobQueueLane(job),queue=jobs.filter(item=>item.project===job.project&&item.status==="queued"&&jobQueueLane(item)===lane).sort((a,b)=>a.created-b.created||a.id.localeCompare(b.id)),position=queue.findIndex(item=>item.id===job.id)+1;
  if(lane==="guest accounts")return "Queued for this machine’s account controls.";
  if(lane==="power")return "Queued for machine control. Conflicting actions on the same machine finish first.";
  return `${position>0?`Position ${position} in this project’s ${lane} queue.`:`Queued in this project’s ${lane} queue.`}${jobs.some(item=>item.project===job.project&&item.status==="running"&&jobQueueLane(item)===lane)?" Waiting for the current task in this queue to finish.":""}`;
 }
 if(job.status==="waiting-for-node")return "Waiting for the offline node to reconnect. This will resume automatically.";
 if(job.status==="reconcile-required")return "Cleanup is pending. Other available actions can continue.";
 if(["failed","interrupted"].includes(job.status))return String(job.output||"").split("\n").filter(line=>/^crange:/.test(line)).pop()?.replace(/^crange:\s*/,"")||"This action needs attention. Open the terminal for details.";
 if(job.status==="succeeded")return "Completed successfully.";
 const lines=String(job.output||"").split("\n");
 for(let i=lines.length-1;i>=0;i--){
  const line=lines[i].replace(/\u001b\[[0-9;]*m/g,"").trim();
  const task=line.match(/^TASK \[([^\]]+)\]/);
  if(task)return task[1].slice(0,180);
  const resource=line.match(/^(.+): (Still (?:creating|destroying|modifying)|Creating|Destroying|Modifying)\.\.\.(.*)$/);
  if(resource)return `${resourceName(resource[1])}: ${resource[2].toLowerCase()}${resource[3]}`.slice(0,180);
  if(/^(?:Verifying |Preparing |Checking |Capturing |Configuring |Running health|Selecting changed|Reviewing |Waiting for |All health checks|Native HA enabled|Retire native HA)/.test(line))return line.slice(0,180);
 }
 return "In progress. You can keep working while this runs.";
}
function jobContextTitle(job){
 const labels={"stage-changes":"Stage changes","plan":"Stage changes","apply":"Deploy changes","bootstrap-plan":"Stage changes","bootstrap-apply":"Deploy changes","stage-deploy":"Deploy changes","scenario-health":"Check VM services","container-health":"Check container services","release-capture":"Capture release","release-test-stage":"Stage test changes","release-test-deploy":"Deploy release test","release-test-prepare":"Prepare release test","release-test-check":"Check release test","release-test-cleanup":"Clean up release test","release-deploy":"Select team release","release-stage":"Stage release changes","release-apply":"Deploy team release","release-check":"Check team services","release-upgrade":"Upgrade teams","release-rollback":"Roll back teams","capture-inspect":"Inspect capture"};
 const context=uiJobContexts.get(job.id),version=job.release_version||context?.version,target=version?`Release ${version}`:context?.name||"";
 return `${labels[job.operation]||job.operation.replaceAll("-"," ")}${target?` · ${target}`:""}${context?.teams?.length?` · Team${context.teams.length===1?"":"s"} ${context.teams.join(", ")}`:""}`;
}
function contextJobHost(p,scope){
 const host=el("div",undefined,"context-jobs");host.dataset.project=p?.project_id??Number($("project").value);host.dataset.jobScope=scope;host.dataset.jobView=view;if(view==="machine-detail"&&machineFocus?.project===Number(p?.project_id))host.dataset.resourceKey=`${machineFocus.kind}:${machineFocus.name}`;return host;
}
function workflowNextActions(job){
 if(job.status!=="succeeded")return [];
 const p=projects.find(item=>Number(item.project_id)===Number(job.project));if(!p)return [];
 if(Number(project()?.project_id)!==Number(p.project_id))return [navigate("Open project","project-overview",p.project_id)];
 const context=uiJobContexts.get(job.id)||{},version=job.release_version||context.version;
 const release=(p.releases||p.scenario_releases||[]).find(item=>item.version===version);
 if(["stage-changes","plan"].includes(job.operation)){
  const plan=stagedPlan(p.project_id);return plan&&!plan.applied?[workflowReviewButton(p,job.operation),scenarioForm("Restage changes",job.operation),workflowDeployAction(p,plan,job.operation==="plan"?"apply":"stage-deploy")]:[];
 }
 if(job.operation==="bootstrap-plan")return [scenarioForm("Deploy changes","bootstrap-apply")];
 if(job.operation==="bootstrap-apply")return [action("Verify network","network-verify")];
 if(["stage-deploy","apply"].includes(job.operation))return [scenarioValidation(p,!workflowStagingMachines(p).length),scenarioForm("Capture release","release-capture")];
 if(["scenario-health","container-health"].includes(job.operation))return [scenarioForm("Capture release","release-capture")];
 if(job.operation==="release-capture"&&release)return [scenarioPrepareTest(p,release)];
 if(["release-test-stage","release-test-prepare"].includes(job.operation)&&release?.test_review)return [scenarioButton("Review changes",()=>scenarioTestPlan(p,release))];
 if(job.operation==="release-test-deploy"&&release)return [scenarioForm("Check test","release-test-check",{version})];
 if(job.operation==="release-test-check"&&release)return [scenarioReleaseChooser(p),scenarioForm("Clean up test","release-test-cleanup",{version})];
 if(job.operation==="release-deploy")return [scenarioForm("Stage changes","release-stage",context.teams?{teams:context.teams}:{})];
 if(job.operation==="release-stage")return [scenarioForm("Deploy changes","release-apply",context.teams?{teams:context.teams}:{})];
 if(job.operation==="release-apply")return [scenarioForm("Check team services","release-check",context.teams?{teams:context.teams}:{})];
 return [];
}
function renderJobNextActions(job){
 const p=projects.find(item=>Number(item.project_id)===Number(job.project)),note=$("job-dialog").querySelector(":scope > p.muted");if(note)note.hidden=!["queued","running","waiting-for-node","reconcile-required"].includes(job.status);
 const revision=JSON.stringify([job.id,job.status,p?.desired_revision,p?.staged_plans,(p?.releases||[]).map(item=>[item.version,item.test_ready,item.test_status,item.test_review?.review_id])]);
 let controls=$("job-dialog-next-actions");if(!controls){controls=el("div",undefined,"scenario-controls");controls.id="job-dialog-next-actions";$("job-dialog").append(controls)}
 if(controls.dataset.revision===revision)return;controls.dataset.revision=revision;
 const buttons=workflowNextActions(job);for(const button of buttons){const callback=button.onclick;if(callback)button.onclick=event=>{$("close-job").click();callback(event)}}
 controls.replaceChildren(...buttons);controls.hidden=!buttons.length;
}
function contextJobCard(job){
 const busy=["queued","running","waiting-for-node"].includes(job.status),card=el("section",undefined,`context-job context-job-${job.status}`),copy=el("div",undefined,"context-job-copy"),top=el("div",undefined,"context-job-heading");
 const needsAction=workflowJobActionRequired(job,jobs);if(needsAction)card.classList.add("context-job-action-required");
 card.dataset.jobId=job.id;card.setAttribute("aria-label",jobContextTitle(job));card.setAttribute("aria-busy",String(busy));
 top.append(el("strong",jobContextTitle(job)),el("span",needsAction?"Action required":({queued:"Queued",running:"Running",succeeded:"Completed",failed:"Failed",interrupted:"Stopped","waiting-for-node":"Waiting for node","reconcile-required":"Cleanup pending"})[job.status]||job.status,"context-job-status"));
 const phase=el("p",jobPhase(job),"muted");phase.setAttribute("role",busy?"status":"note");
 copy.append(top,phase,jobMetadata(job));const controls=el("div",undefined,"scenario-controls"),terminal=scenarioJob(job);terminal.id="terminal-"+job.id;controls.append(...workflowNextActions(job),terminal);
 if(job.status==="queued"||(job.status==="running"&&!["network-reconcile","network-publish"].includes(job.operation))){
  const stop=scenarioButton("Stop",async()=>{stop.disabled=true;try{await api("jobs",{action:"cancel",job_id:job.id});notice("Stop requested.")}catch(error){notice(error.message,true)}finally{stop.disabled=false}},"quiet");stop.id="stop-"+job.id;controls.append(stop);
 }
 if(["succeeded","failed","interrupted"].includes(job.status)){
  const dismiss=scenarioButton("Dismiss",()=>{dismissJobNotice(job.id);if(view==="scenario")document.querySelector('.scenario-tab[aria-selected="true"]')?.focus({preventScroll:true})},"quiet");
  dismiss.setAttribute("aria-label",`Dismiss ${jobContextTitle(job)} notification`);controls.append(dismiss);
 }
 card.append(copy,controls);return card;
}
function refreshContextJobProgress(){
 for(const host of document.querySelectorAll(".context-jobs")){
  const projectId=Number(host.dataset.project),scope=host.dataset.jobScope,page=host.dataset.jobView;
  const relevant=jobs.filter(job=>{
   const context=uiJobContexts.get(job.id);
   if(page==="machine-detail")return !(job.operation==="guac-snapshot"&&context?.action==="list")&&job.project===projectId&&(job.resource_key||context?.resource_key)===host.dataset.resourceKey;
   if(page==="scenario")return job.project===projectId&&jobScope(job.operation)===scope&&!deploymentOperations.has(job.operation)&&(/^(?:scenario-|container-|network-|release-)/.test(job.operation)||job.operation==="capture-inspect");
   return context?.view===page&&(job.project===projectId||job.project===0);
  });
  const active=relevant.filter(job=>["queued","running","waiting-for-node","reconcile-required"].includes(job.status));
  const recent=relevant.find(job=>!active.includes(job));
  // A result is temporary feedback; the full record remains in Jobs. Choose
  // the latest result first so dismissing it cannot uncover older notices.
  const recentVisible=recent&&!dismissedJobNotices.has(recent.id)&&notifiedJobNotices.has(recent.id);
  const displayed=(active.length?active:recentVisible?[recent]:[]).slice(0,4),focusId=host.contains(document.activeElement)?document.activeElement.id:null;
  const revision=JSON.stringify([displayed.map(job=>[job.id,job.status,jobPhase(job)]),projects.find(p=>Number(p.project_id)===projectId)?.desired_revision,projects.find(p=>Number(p.project_id)===projectId)?.staged_plans]);
  if(host.dataset.revision!==revision){host.replaceChildren(...displayed.map(contextJobCard));host.dataset.revision=revision;if(focusId)document.getElementById(focusId)?.focus({preventScroll:true})}
  host.hidden=!displayed.length;
 }
 const bar=document.querySelector(".scenario-deployment[aria-busy=true]");
 if(bar){const job=jobs.find(item=>item.id===bar.dataset.deploymentJob);if(job)bar.querySelector("p").textContent=jobPhase(job)}
 if(typeof updateScenarioTabNotifications==="function")updateScenarioTabNotifications();
}

function projectContextView(){return ["project-overview","scenario","teams","team-detail","project-users","project-user-detail","project-settings","machines","machine-detail"].includes(view)}
function projectUsersButton(p){return navigate("Project users","project-users",p.project_id)}
function projectJobsButton(p){
 return scenarioButton("Project jobs",async()=>{jobHistoryProject=Number(p.project_id);jobHistoryPage=1;view="jobs";localStorage.setItem("crange.view",view);await fetchJobs();render()});
}

function jobDuration(seconds){
 const total=Math.max(0,Math.floor(seconds)),s=total%60,m=Math.floor(total/60)%60,h=Math.floor(total/3600)%24,d=Math.floor(total/86400);
 if(d)return `${d}d ${h}h ${m}m`;
 if(h)return `${h}h ${m}m ${s}s`;
 return m?`${m}m ${s}s`:`${s}s`;
}
function jobRuntimeText(job,now=Date.now()/1000){
 if(job.status==="queued")return `Queued ${jobDuration(now-job.created)}`;
 if(!job.started&&job.status==="interrupted"&&String(job.output||"").includes("Cancelled before starting"))return "Not run";
 const active=["running","waiting-for-node","reconcile-required"].includes(job.status),end=job.finished??(active?now:null);
 if(end===null)return "Runtime unavailable";
 const start=job.started??job.created;
 if(!Number.isFinite(start))return "Runtime unavailable";
 return `Runtime ${job.started?"":"≈ "}${jobDuration(end-start)}`;
}
function jobRuntimeNode(job){
 const node=el("span",jobRuntimeText(job),"job-runtime");node.dataset.jobRuntime=job.id;
 node.title=job.status==="queued"?"Time waiting in this project’s queue.":job.started?"Time since execution started; initial queue time excluded.":"Older task: approximate elapsed time includes its initial queue wait.";
 return node;
}
function jobMetadata(job){
 const meta=el("div",undefined,"context-job-meta");meta.append(el("span",`Started by ${job.actor||"Unknown"}`),jobRuntimeNode(job));return meta;
}
function refreshJobRuntimes(){
 const byId=new Map(jobs.map(job=>[job.id,job]));if(pinnedJob&&!byId.has(pinnedJob.id))byId.set(pinnedJob.id,pinnedJob);
 for(const node of document.querySelectorAll("[data-job-runtime]")){const job=byId.get(node.dataset.jobRuntime);if(job){const text=jobRuntimeText(job);if(node.textContent!==text)node.textContent=text}}
}
setInterval(()=>{if(user&&workspaceReady&&!document.hidden)refreshJobRuntimes()},1000);
