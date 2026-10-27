"use strict";

// Display-only navigation and fixed, authorized job forms. Native identities and
// authorization are still checked by the existing controller workflows.
let machineFocus=null;
try{machineFocus=JSON.parse(localStorage.getItem("crange.machine")||"null")}catch(_){}
const machineListStates=new Map(),machineSelections=new Map();
function entityLink(label,follow,description){const button=el("button",label,"entity-link");button.type="button";button.dataset.noTooltip="true";if(description)button.setAttribute("aria-label",description);button.onclick=follow;return button}
function copyValueButton(value,label="value"){
 const button=entityLink(value,async()=>{try{await navigator.clipboard.writeText(String(value));notice(`${label} copied`)}catch(_){notice(`Could not copy ${label.toLowerCase()}. Select the text and copy it manually.`,true)}},`Copy ${label.toLowerCase()} ${value}`);button.classList.add("copy-value");return button;
}
function machineEntries(p){return [["jumpbox","jump_boxes"],["vm","scenario_boxes"],["container","scenario_containers"]].flatMap(([kind,field])=>Object.entries(p[field]||{}).map(([name,box])=>({kind,name,key:name,box,resourceKey:`${kind}:${name}`})))}
function machineKind(item){return item.kind==="container"?"Container":"VM"}
function machineTitle(item){return item.box.display_name||item.box.name||item.name}
function machineAddress(p,item){return item.kind==="jumpbox"?jumpAddress(p.project_id,item.box):`10.${p.project_id}.${item.box.network_id??item.box.team_id}.${item.box.ip_last_octet}`}
function openMachine(p,kind,name){machineFocus={project:Number(p.project_id),kind,name};localStorage.setItem("crange.machine",JSON.stringify(machineFocus));view="machine-detail";localStorage.setItem("crange.view",view);render()}
function machineLink(p,item,label=machineTitle(item)){return entityLink(label,()=>openMachine(p,item.kind,item.name))}
function teamLink(p,team){return entityLink(userTeamLabel(team),()=>openUserTeam(p,team))}
function scenarioLink(p,label="Scenario",tab="workspace"){return entityLink(label,()=>{scenarioState(p).tab=tab;navigate("","scenario",p.project_id).click()})}
function machineNodeLink(p,item){const name=machineNode(p,item.kind,item.name,item.box);return user?.admin&&/^proxmox\d+$/.test(name)?entityLink(name,async()=>{expandedClusterNodes.add(name);view="cluster";localStorage.setItem("crange.view",view);render();await refreshCluster();document.getElementById(`cluster-node-${name}`)?.scrollIntoView({block:"start"})}):name}
function machineEditAction(p,item){
 if(item.kind==="jumpbox"||item.box.is_jumpbox)return jumpboxResourcesButton(item);
 const box=item.box;return optionAction("Edit configuration",item.kind==="vm"?"scenario-edit":"container-edit",{name:item.name,team_id:box.team_id,template:box.template,node:box.node_name,actual_node:appliedNode(p,item.kind,item.name,box),cores:box.cores,memory:box.memory_mb,disk:box.disk_gb??(item.kind==='vm'?'':8),health:JSON.stringify(box.health_checks||[],null,2),guac_protocols:box.guac_services||[box.guac_protocol||"ssh"],ip:box.ip_last_octet,network:box.network_key||""});
}
function machineRemoveAction(p,item){
 const button=el("button","Remove machine","quiet danger-action");button.type="button";button.onclick=()=>userModal(`Remove ${machineTitle(item)}`,(form,error,close)=>{
  form.append(el("p","Remove this machine from the project’s declared configuration. Review and apply the resulting infrastructure plan to delete its live machine and disks. This affects everyone assigned to it."));
  const submit=el("button","Remove from configuration","danger-action");submit.type="submit";form.append(submit);form.onsubmit=async event=>{event.preventDefault();submit.disabled=true;try{await queueJob(item.kind==="jumpbox"?"box-remove":item.kind==="vm"?"scenario-remove":"container-remove",{name:item.name},Number(p.project_id));close();view="machines";localStorage.setItem("crange.view",view);render()}catch(e){error.textContent=e.message}finally{submit.disabled=false}};
 });return button;
}
function machinePowerMenu(p,item){
 const state=machineRuntime(p,item.kind,item.name).state;
 return actionMenu("Power",[["Start","start","stopped"],["Shutdown","shutdown","running"],["Restart","restart","running"],["Force stop","stop","running"]].map(([label,action,required])=>{
  const button=el("button",label,action==="stop"?"danger-action":"quiet");button.type="button";button.disabled=state!==required;button.onclick=()=>machineGroupPower(p,[item],action);return button;
 }),{context:machineTitle(item)});
}
function diskProofResumeAction(p,item,sourceJob){
 const button=el("button","Continue disk test","quiet");button.type="button";
 button.dataset.helpOperation="machine-disk-proof-resume";
 button.onclick=async()=>{button.disabled=true;const options={name:item.name,kind:"vm",source_job_id:sourceJob.id};try{
  const result=await api("jobs",{project:100,operation:"machine-disk-proof-resume",options});
  rememberUIJob(result.job_id,"machine-disk-proof-resume",options,100);await fetchJobs();updateTaskIndicator();render();notice("Disk proof continuation queued.");
 }catch(error){notice(error.message,true)}finally{button.disabled=false}};return button;
}
function diskProofStopAction(p,item,sourceJob){
 const button=el("button","Stop retained disk restore","quiet danger-action");button.type="button";
 button.dataset.helpOperation="machine-disk-proof-stop";
 button.onclick=async()=>{button.disabled=true;const options={name:item.name,kind:"vm",source_job_id:sourceJob.id};try{
  const result=await api("jobs",{project:100,operation:"machine-disk-proof-stop",options});
  rememberUIJob(result.job_id,"machine-disk-proof-stop",options,100);await fetchJobs();updateTaskIndicator();render();notice("Restore stop requested. The probe and archive remain retained until task termination is verified.");
 }catch(error){notice(error.message,true)}finally{button.disabled=false}};return button;
}
function diskProofCleanupAction(p,item,sourceJob){
 const button=el("button","Clean cancelled disk test","quiet");button.type="button";
 button.dataset.helpOperation="machine-disk-proof-cleanup";
 button.onclick=async()=>{button.disabled=true;const options={name:item.name,kind:"vm",source_job_id:sourceJob.id};try{
  const result=await api("jobs",{project:100,operation:"machine-disk-proof-cleanup",options});
  rememberUIJob(result.job_id,"machine-disk-proof-cleanup",options,100);await fetchJobs();updateTaskIndicator();render();notice("Disk test cleanup queued. It proceeds only after the restore writes have finished.");
 }catch(error){notice(error.message,true)}finally{button.disabled=false}};return button;
}
function diskMaintenanceReviewAction(item){
 const button=el('button','Review disk test capacity','quiet');button.type='button';button.onclick=()=>queueJob('template-disk-maintenance-review',{name:item.name},0);return button;
}
function diskReplacementReviewAction(item){return fixedAction('Review SSD disk replacement','template-disk-replace-review',{name:item.name},'',0)}
function machineRowControls(p,item,actions=[]){
 const state=machineRuntime(p,item.kind,item.name).state,title=machineTitle(item),known=["running","stopped"].includes(state);
 function iconButton(label,icon,follow,disabled=false){const button=el("button",undefined,"quiet icon-button");button.type="button";button.dataset.noTooltip="true";button.setAttribute("aria-label",`${label} ${title}`);button.append(actionIcon(icon));button.disabled=disabled;button.onclick=follow;return button}
 const connect=iconButton("Connect to","monitor",()=>launchGuacamole(p.project_id,item.resourceKey||`${item.kind}:${item.name}`),state!=="running"),edit=machineEditAction(p,item);
 edit.dataset.noTooltip="true";edit.removeAttribute("title");edit.setAttribute("aria-label",`Edit ${title}`);edit.classList.add("icon-button");edit.replaceChildren(actionIcon("edit"));
 const normal=iconButton(state==="running"?"Shutdown":"Start",state==="running"?"stop":"start",()=>machineGroupPower(p,[item],state==="running"?"shutdown":"start"),!known);normal.classList.add("machine-power-control",state==="running"?"is-running":"is-stopped");
 const restart=el("button","Restart","quiet"),force=el("button","Force shutdown","quiet danger-action");restart.type=force.type="button";restart.disabled=force.disabled=state!=="running";restart.onclick=()=>machineGroupPower(p,[item],"restart");force.onclick=()=>machineGroupPower(p,[item],"stop");
 const other=actions.filter(action=>!["machine-state","box-state"].includes(action.dataset?.helpOperation)&&!action.querySelector?.('.action-menu[aria-label="Power"]')&&!["Resources","Edit","Edit configuration"].includes(action.textContent.trim()));
 const snapshots=el("button","Snapshots","quiet");snapshots.type="button";snapshots.onclick=()=>{openMachine(p,item.kind,item.name);const panel=document.querySelector(".machine-snapshots");if(panel){panel.open=true;panel.scrollIntoView({block:"nearest"})}};
 const remote=remoteAccessButton(item.kind,item.name,item.box);
 const review=optionAction("Review disk usage","machine-disk-review",{name:item.name,kind:item.kind});
 const proof=Number(p.project_id)===100&&item.kind==="vm"&&machineRuntime(p,item.kind,item.name).guest_id===223
  ?(user?.admin?diskMaintenanceReviewAction(item):optionAction("Test disk reduction","machine-disk-proof",{name:item.name,kind:"vm"})):null;
 const failedProof=Number(p.project_id)===100&&item.kind==="vm"
  ?jobs.filter(job=>job.project===100&&job.operation==="machine-disk-proof"&&job.resource_key===`vm:${item.name}`&&["failed","interrupted"].includes(job.status)).sort((a,b)=>b.created-a.created)[0]:null;
 const cancelledRestore=failedProof?.id==="b660cd54bd5e41e185c60bb516484080";
 const resume=failedProof&&!cancelledRestore?diskProofResumeAction(p,item,failedProof):null;
 const cleanup=cancelledRestore?diskProofCleanupAction(p,item,failedProof):null;
 const replacement=user?.admin&&Number(p.project_id)===100&&item.kind==='vm'&&[220,221,222,224,225,226,227,228,229,290,291,306,307].includes(machineRuntime(p,item.kind,item.name).guest_id)?diskReplacementReviewAction(item):null;
 const stopRestore=failedProof?diskProofStopAction(p,item,failedProof):null;
 return [connect,edit,normal,actionMenu("More actions",[force,restart,snapshots,remote,review,...(proof?[proof]:[]),...(replacement?[replacement]:[]),...(resume?[resume]:[]),...(stopRestore?[stopRestore]:[]),...(cleanup?[cleanup]:[]),...other],{iconOnly:true,context:title})];
}
function machineGroupPower(p,items,action){
 const labels={start:"Start",shutdown:"Shutdown",restart:"Restart",stop:"Force shutdown"};if(!labels[action])return;
 const targets=items.filter(item=>machineRuntime(p,item.kind,item.name).state===(action==="start"?"stopped":"running"));if(!targets.length)return;
 userModal(`${labels[action]} ${targets.length===1?machineTitle(targets[0]):`${targets.length} machines`}`,(form,error,close)=>{
  form.append(el("p",action==="start"?"Start the selected stopped machines.":action==="stop"?"Force stop cuts power immediately. Unsaved work can be lost.":`${labels[action]} interrupts active sessions on these machines.`));
  const names=el("ul",undefined,"machine-confirm-list");targets.forEach(item=>names.append(el("li",`${machineTitle(item)} · ${userTeamLabel(item.box.team_id)}`)));form.append(names);
  if(targets.length!==items.length)form.append(el("p",`${items.length-targets.length} selected machine(s) have an incompatible or unverified power state and will be skipped.`,"muted"));
  const submit=el("button",`${labels[action]} ${targets.length===1?"machine":`${targets.length} machines`}`,action==="stop"?"danger-action":"primary");submit.type="submit";form.append(submit);
  form.onsubmit=async event=>{
   event.preventDefault();submit.disabled=true;const accepted=[],failed=[];
   // Record each accepted request before refreshing the page; a failed refresh
   // must never cause an already queued power action to be submitted twice.
   for(const item of targets){try{const options={resource_key:`${item.kind}:${item.name}`,action},result=await api("jobs",{project:Number(p.project_id),operation:"guac-box-control",options});accepted.push(item);rememberUIJob(result.job_id,"guac-box-control",options,p.project_id)}catch(e){failed.push(`${machineTitle(item)}: ${e.message}`)}}
   try{await fetchJobs();updateTaskIndicator();render()}catch(_){}
   if(failed.length){error.textContent=`${accepted.length} queued. ${failed.length} request(s) could not be confirmed: ${failed.join("; ")}. Check Jobs before trying these machines again.`;submit.textContent="Finished submitting"}
   else{close();notice(`${accepted.length} machine action${accepted.length===1?"":"s"} queued`)}
  };
 });
}
function diskSizeLabel(size){return typeof size==="number"&&Number.isFinite(size)&&size>0?`${Number(size.toFixed(2))} GiB`:"—"}
function machineDiskCell(p,item){
 const live=machineRuntime(p,item.kind,item.name),observed=live.observed_disk_gb;
 const planned=live.state==="undeployed"?item.box?.disk_gb:null;
 const cell=el("span",diskSizeLabel(observed??planned));
 cell.title=observed>0?"Disk capacity reported by Proxmox":planned>0?"Declared disk capacity; machine is not deployed":"Disk capacity is unavailable";
 return cell;
}
function machineTable(p,headers,items,makeActions,scope="machines"){
 const diskIndex=headers.includes("Resources")?headers.indexOf("Resources")+1:headers.indexOf("Node")>=0?headers.indexOf("Node"):headers.length;
 const diskHeaders=[...headers];diskHeaders.splice(diskIndex,0,"Disk");
 const id=`${p.project_id}:${scope}`;let selected=machineSelections.get(id)||new Set();
 const section=el("section",undefined,"machine-table"),toolbar=el("div",undefined,"machine-selection-bar"),label=el("span","","muted"),clear=el("button","Clear selection","quiet");clear.type="button";
 const normalized=items.map(item=>({...item,name:item.name??item.key,kind:item.kind==="VM"?"vm":item.kind==="Container"?"container":item.kind}));
 // Normalize before building keys, including legacy team table labels.
 const keys=normalized.map(item=>`${item.kind}:${item.name}`);selected=new Set([...selected].filter(key=>keys.includes(key)));machineSelections.set(id,selected);
 const buttons=[["Start stopped","start"],["Shutdown running","shutdown"],["Restart running","restart"]].map(([text,action])=>{const button=el("button",text,"quiet");button.type="button";button.onclick=()=>machineGroupPower(p,normalized.filter(item=>selected.has(`${item.kind}:${item.name}`)),action);return {button,action}});
 toolbar.append(label,...buttons.map(item=>item.button),clear);section.append(toolbar);
 const checks=[];let all;
 const sync=()=>{toolbar.hidden=!selected.size;label.textContent=`${selected.size} selected`;for(const {button,action} of buttons)button.disabled=!normalized.some(item=>selected.has(`${item.kind}:${item.name}`)&&machineRuntime(p,item.kind,item.name).state===(action==="start"?"stopped":"running"));if(all){all.checked=keys.length>0&&keys.every(key=>selected.has(key));all.indeterminate=selected.size>0&&!all.checked}checks.forEach(({check,key})=>check.checked=selected.has(key))};
 clear.onclick=()=>{selected.clear();sync()};
 const rows=normalized.map((item,index)=>{const key=keys[index],check=document.createElement("input");check.type="checkbox";check.setAttribute("aria-label",`Select ${machineTitle(item)}`);check.onchange=()=>{check.checked?selected.add(key):selected.delete(key);sync()};checks.push({check,key});const cells=[...items[index].cells];cells.splice(diskIndex,0,machineDiskCell(p,item));if(cells[0] instanceof Node){const link=entityLink(undefined,()=>openMachine(p,item.kind,item.name));link.append(cells[0]);cells[0]=link}else cells[0]=machineLink(p,item,cells[0]);return {...item,cells:[check,...cells],source:items[index]}});
 const wrapper=actionTable(["",...diskHeaders],rows,item=>machineRowControls(p,item,makeActions(item.source)),{rawActions:true});const heading=wrapper.querySelector("thead th");if(heading){all=document.createElement("input");all.type="checkbox";all.setAttribute("aria-label","Select all matching machines");all.onchange=()=>{selected.clear();if(all.checked)keys.forEach(key=>selected.add(key));sync()};heading.append(all)}section.append(wrapper);sync();return section;
}
function renderMachines(content,p){
 header("","Machines");refreshProjectRuntime(p);let state=machineListStates.get(p.project_id);if(!state){state={query:"",team:"",kind:""};machineListStates.set(p.project_id,state)}
 const introduction=el("div",undefined,"scenario-heading");introduction.append(el("p","Manage project machines across staging and teams.","muted"),scenarioLink(p,"Add a staging machine"));content.append(introduction);
 const filters=el("div",undefined,"user-list-filters"),searchLabel=el("label","Search"),search=el("input");search.type="search";search.id="machine-search";search.value=state.query;search.placeholder="Name, address, or template…";search.setAttribute("aria-label","Find a machine");searchLabel.append(search);filters.append(searchLabel);
 const team=userSelect(filters,"machine_team","Team",[["","All teams"],[254,"Team 0 · Staging"],...(p.team_ids||[]).map(t=>[t,`Team ${t}`])],state.team),kind=userSelect(filters,"machine_kind","Type",[["","All types"],["vm","VMs"],["container","Containers"],["jumpbox","Jump boxes"]],state.kind);content.append(filters);const results=el("div");content.append(results);
 function draw(){const items=machineEntries(p).filter(item=>(!state.team||String(item.box.team_id)===state.team)&&(!state.kind||(state.kind==="jumpbox"?item.kind==="jumpbox"||item.box.is_jumpbox:item.kind===state.kind))&&[item.name,machineTitle(item),machineAddress(p,item),templateDisplay(item.box.template,item.kind==="container"?"container_templates":"vm_templates")].join(" ").toLowerCase().includes(state.query.trim().toLowerCase()));
  results.replaceChildren();if(!items.length){results.append(el("p",machineEntries(p).length?"No machines match these filters.":"No machines yet. Add a machine in Scenario to get started.","empty"));return}
  results.append(machineTable(p,["Machine","Type","Team","IP address","Resources","Node","Status"],items.map(item=>({...item,cells:[machineTitle(item),machineKind(item),teamLink(p,item.box.team_id),machineAddress(p,item),`${item.box.cores} ${item.box.cores===1?"core":"cores"} · ${scenarioRam(item.box.memory_mb)}`,machineNodeLink(p,item),machineRuntime(p,item.kind,item.name).label]})),item=>[machinePowerMenu(p,item)],"inventory"));
 }
 const update=()=>{state.query=search.value;state.team=team.value;state.kind=kind.value;machineSelections.delete(`${p.project_id}:inventory`);draw()};search.oninput=update;team.onchange=kind.onchange=update;draw();
}
function machineUserHasAccess(p,item,record){
 if((record.connections||[]).includes(item.resourceKey))return true;
 if(item.kind!=="jumpbox"||!(record.teams||[]).map(Number).includes(Number(item.box.team_id)))return false;
 const defaults=Object.entries(p.jump_boxes||{}).filter(([,box])=>Number(box.team_id)===Number(item.box.team_id)).sort(([a,x],[b,y])=>Number(x.jump_number)-Number(y.jump_number)||a.localeCompare(b));
 return defaults[0]?.[0]===item.name;
}
function machineFacts(title,values){const section=el("section",undefined,"machine-facts"),list=el("dl");section.append(el("h3",title));for(const [label,value] of values){const description=el("dd");appendCellValue(description,value);list.append(el("dt",label),description)}section.append(list);return section}
const machineSnapshotStates=new Map();
function machineSnapshotState(p,item){
 const key=`${p.project_id}:${item.resourceKey}`;
 if(!machineSnapshotStates.has(key))machineSnapshotStates.set(key,{key,open:false,rows:null,job:null,starting:false,watching:false,error:"",lastJobId:null,refreshAfter:false});
 return machineSnapshotStates.get(key);
}
function parseMachineSnapshots(output,resourceKey){
 for(const line of String(output||"").split("\n").reverse()){
  let result;try{result=JSON.parse(line)}catch(_){continue}
  if(result?.box!==resourceKey||!Array.isArray(result.snapshots))continue;
  if(result.snapshots.some(row=>!row||typeof row.name!=="string"||!row.name||row.name.length>255||row.created!=null&&(typeof row.created!=="number"||!Number.isFinite(row.created))))throw Error("The snapshot list contains invalid entries.");
  return result.snapshots.map(row=>({name:row.name,description:typeof row.description==="string"?row.description:"",created:row.created??null}));
 }
 throw Error("The task did not return a snapshot list for this machine.");
}
function refreshMachineSnapshotPanel(p,item){
 const state=machineSnapshotState(p,item);
 for(const host of document.querySelectorAll(".machine-snapshots"))if(host.dataset.snapshotKey===state.key)drawMachineSnapshots(host,p,item,state);
}
function machineSnapshotPanelActive(p,item){return view==="machine-detail"&&Number(project()?.project_id)===Number(p.project_id)&&machineFocus?.kind===item.kind&&machineFocus?.name===item.name}
async function watchMachineSnapshotJob(p,item){
 const state=machineSnapshotState(p,item);if(state.watching||!state.job)return;state.watching=true;state.error="";
 try{
  while(state.open&&state.job&&machineSnapshotPanelActive(p,item)){
   const id=state.job.id,{job}=await api(`jobs/${id}`);
   if(!job||job.id!==id||job.project!==Number(p.project_id)||job.operation!=="guac-snapshot"||job.resource_key!==item.resourceKey)throw Error("The snapshot task did not match this machine.");
   if(state.job?.id!==id)return;
   state.job.status=job.status;refreshMachineSnapshotPanel(p,item);
   if(job.status==="succeeded"){
    const action=state.job.action;
    if(action==="list")state.rows=parseMachineSnapshots(job.output,item.resourceKey);
    state.job=null;state.error="";state.refreshAfter=action!=="list";return;
   }
   if(["failed","interrupted"].includes(job.status)){state.job=null;throw Error(String(job.output||"The snapshot task failed. Open its output for details.").slice(-2000))}
   await new Promise(resolve=>setTimeout(resolve,1000));
  }
 }catch(error){state.error=state.job?`Could not read the snapshot task. Retry will resume watching the same task. ${error.message}`:error.message}
 finally{state.watching=false;refreshMachineSnapshotPanel(p,item);if(state.refreshAfter&&state.open&&!state.job&&machineSnapshotPanelActive(p,item)){state.refreshAfter=false;startMachineSnapshotJob(p,item,"list")}}
}
async function startMachineSnapshotJob(p,item,action,snapshot=""){
 const state=machineSnapshotState(p,item);if(state.starting)return;
 if(state.job){watchMachineSnapshotJob(p,item);return}
 state.starting=true;state.error="";refreshMachineSnapshotPanel(p,item);
 try{
  const options={resource_key:item.resourceKey,action,snapshot},result=await api("jobs",{project:Number(p.project_id),operation:"guac-snapshot",options});
  state.job={id:result.job_id,action,status:"queued"};state.lastJobId=result.job_id;rememberUIJob(result.job_id,"guac-snapshot",options,p.project_id);
 }catch(error){state.error=error.message}
 finally{state.starting=false;refreshMachineSnapshotPanel(p,item)}
 if(state.job)watchMachineSnapshotJob(p,item);
}
function machineSnapshotDialog(p,item,action,snapshot=""){
 const titles={create:"Add snapshot",restore:"Restore snapshot",delete:"Delete snapshot"};if(!titles[action])return;
 userModal(`${titles[action]}${snapshot?` · ${snapshot}`:""} · ${machineTitle(item)}`,(form,error,close)=>{
  if(action==="restore")form.append(el("p",`Restore this machine to “${snapshot}”. Current changes can be lost. The machine must be stopped.`));
  if(action==="delete")form.append(el("p",`Permanently delete snapshot “${snapshot}”.`));
  const name=action==="create"?userField(form,"snapshot_name","Snapshot name","","text","before-exercise"):null;if(name){name.required=true;name.pattern="[a-z][a-z0-9-]{0,39}";name.maxLength=40}
  const submit=el("button",titles[action],action==="restore"||action==="delete"?"danger-action":"primary");submit.type="submit";form.append(submit);
  form.onsubmit=async event=>{event.preventDefault();submit.disabled=true;try{await startMachineSnapshotJob(p,item,action,name?name.value:snapshot);const state=machineSnapshotState(p,item);if(state.error)throw Error(state.error);close()}catch(e){error.textContent=e.message}finally{submit.disabled=false}};
 });
}
function drawMachineSnapshots(host,p,item,state){
 const body=host.querySelector(".machine-snapshots-body");if(!body)return;body.replaceChildren();
 const live=machineRuntime(p,item.kind,item.name),busy=state.starting||Boolean(state.job),writable=["running","stopped"].includes(live.state)&&!busy&&!state.error;
 const controls=el("div",undefined,"scenario-toolbar"),add=el("button","Add snapshot","quiet"),refresh=el("button",state.error?"Retry":"Refresh snapshots","quiet");add.type=refresh.type="button";
 add.disabled=!writable||(state.rows?.length??0)>=20;add.onclick=()=>machineSnapshotDialog(p,item,"create");refresh.disabled=state.starting||state.watching;refresh.onclick=()=>state.job?watchMachineSnapshotJob(p,item):startMachineSnapshotJob(p,item,"list");controls.append(add,refresh);body.append(controls);
 if(state.error){const error=el("p",state.error,"user-error");error.setAttribute("role","alert");body.append(error);if(state.lastJobId)body.append(scenarioButton("View task output",()=>showJob(state.lastJobId,"guac-snapshot")))}
 if(busy){const status=el("p",state.starting?"Submitting snapshot task…":state.job.action==="list"?`Loading snapshots · ${state.job.status}`:`${({create:"Adding",restore:"Restoring",delete:"Deleting"})[state.job.action]} snapshot · ${state.job.status}`,"muted");status.setAttribute("role","status");body.append(status)}
 if(live.state==="undeployed"){body.append(el("p","Deploy this machine before managing snapshots.","empty"));return}
 if(state.rows?.length){
  body.append(actionTable(["Snapshot","Created","Description"],state.rows.map(snapshot=>({snapshot,cells:[snapshot.name,snapshot.created?new Date(snapshot.created*1000).toLocaleString():"—",snapshot.description||"—"]})),({snapshot})=>{
   const supported=/^[a-z][a-z0-9-]{0,39}$/.test(snapshot.name),restore=el("button","Restore","quiet"),remove=el("button","Delete","quiet danger-action");restore.type=remove.type="button";restore.disabled=!writable||live.state!=="stopped"||!supported;remove.disabled=!writable||!supported;restore.setAttribute("aria-label",`Restore snapshot ${snapshot.name}`);remove.setAttribute("aria-label",`Delete snapshot ${snapshot.name}`);restore.onclick=()=>machineSnapshotDialog(p,item,"restore",snapshot.name);remove.onclick=()=>machineSnapshotDialog(p,item,"delete",snapshot.name);return [restore,remove];
  }));
  if(live.state==="running")body.append(el("p","Stop the machine before restoring a snapshot.","muted"));
 }else if(state.rows&&!state.error&&!busy)body.append(el("p","No snapshots yet. Add a snapshot to save this machine’s state.","empty"));
}
function renderMachineSnapshots(p,item){
 const state=machineSnapshotState(p,item),details=el("details",undefined,"machine-snapshots"),summary=el("summary","Snapshots"),body=el("div",undefined,"machine-snapshots-body");
 details.dataset.snapshotKey=state.key;details.open=state.open;details.append(summary,body);drawMachineSnapshots(details,p,item,state);
 details.ontoggle=()=>{const wasOpen=state.open;state.open=details.open;if(state.open&&!wasOpen){if(machineRuntime(p,item.kind,item.name).state!=="undeployed")state.job?watchMachineSnapshotJob(p,item):startMachineSnapshotJob(p,item,"list")}else if(state.open&&state.job)watchMachineSnapshotJob(p,item);else if(state.open&&state.refreshAfter){state.refreshAfter=false;startMachineSnapshotJob(p,item,"list")}};
 return details;
}

function renderMachineDetail(content,p){
 const item=machineEntries(p).find(item=>machineFocus?.project===Number(p.project_id)&&item.kind===machineFocus.kind&&item.name===machineFocus.name);
 if(!item){header("","Machine unavailable");content.append(el("p","This machine is no longer declared in the selected project.","empty"),navigate("All machines","machines",p.project_id));return}
 refreshProjectRuntime(p);const live=machineRuntime(p,item.kind,item.name),box=item.box,placement=p.applied_placements?.[item.resourceKey]||{},network=box.network_id??box.team_id,release=p.release_rollouts?.[String(box.team_id)],staging=Number(box.team_id)===254;
 header("",machineTitle(item));const breadcrumbs=el("div",undefined,"machine-breadcrumbs");breadcrumbs.append(backLink("All machines",()=>navigate("","machines",p.project_id).click()),teamLink(p,box.team_id),scenarioLink(p,staging?"Staging scenario":"Scenario releases",staging?"workspace":"releases"));content.append(breadcrumbs);
 const heading=el("div",undefined,"scenario-heading"),identity=el("div",undefined,"machine-identity");identity.append(statusBadge(live.label)||el("span",live.label),el("span",`${machineKind(item)}${box.is_jumpbox||item.kind==="jumpbox"?" · Jump box":""}`,"muted"));
 const controls=el("div",undefined,"scenario-controls");controls.classList.add("row-actions","machine-controls");controls.append(...machineRowControls(p,item,[machineRemoveAction(p,item)]));heading.append(identity,controls);content.append(heading);
 if(live.power_error)content.append(el("p",live.power_error,"scenario-warning"));
 const facts=el("div",undefined,"machine-detail-grid");facts.append(
  machineFacts("Membership",[["Project",entityLink(p.display_name||`Project ${p.project_id}`,()=>navigate("","project-overview",p.project_id).click())],["Team",teamLink(p,box.team_id)],["Scenario",scenarioLink(p,staging?"Team 0 · Staging":"Team deployment",staging?"workspace":"deployments")],["Selected release",release?.desired?.version?scenarioLink(p,release.desired.version,"releases"):"Not selected"],["Deployed release",release?.applied?.version?scenarioLink(p,release.applied.version,"releases"):"Not recorded"]]),
  machineFacts("Configuration",[["Machine key",copyValueButton(item.name,"Machine key")],[item.kind==="container"?"Recorded container ID":"Recorded VM ID",live.guest_id??(live.state==="undeployed"?"Undeployed":"Not recorded")],["Template",templateDisplay(box.template,item.kind==="container"?"container_templates":"vm_templates")],["CPU",`${box.cores} cores`],["RAM",scenarioRam(box.memory_mb)],["Disk",machineDiskCell(p,item)],["Node",machineNodeLink(p,item)],["Storage",placement.datastore_id||box.resolved?.datastore_id||"Not recorded"]]),
  machineFacts("Network and access",[["IP address",machineAddress(p,item)],["Subnet",teamSubnet(p.project_id,network)],["Gateway",`10.${p.project_id}.${network}.1`],["Network",box.network_key||"Standard team network"],["Protocols",(box.guac_services||[box.guac_protocol||"ssh"]).map(value=>value.toUpperCase()).join(", ")],["Observed power",live.label],["Last observed",live.observed_at?new Date(live.observed_at*1000).toLocaleString():"Not observed"]]));content.append(facts);
 const access=el("section",undefined,"machine-detail-section"),assigned=guacDirectory.filter(record=>machineUserHasAccess(p,item,record));access.append(el("h3","Assigned project users"));
 if(guacDirectoryError)access.append(el("p","User access could not be loaded. Open Project users to retry.","user-error"));else if(assigned.length)access.append(table(["User","Home team","Account"],assigned.map(record=>[entityLink(record.login,()=>openGuacUser(record)),record.home_team==null?"Unassigned":teamLink(p,record.home_team),record.enabled?"Enabled":"Locked"])));else access.append(el("p","No project users are assigned to this machine.","muted"));access.append(projectUsersButton(p),remoteAccessButton(item.kind,item.name,box));if((box.guac_services||[box.guac_protocol||"ssh"]).includes("ssh")&&!/windows|win(?:10|11|2[0-9]{3})|bsd/i.test(`${box.template} ${templateDisplay(box.template,"vm_templates")}`)){const guest=boxUsersButton(item.kind,item.name);guest.textContent="Linux accounts";guest.removeAttribute("title");guest.disabled=live.state!=="running";access.append(guest)}content.append(access);
 const operations=el("section",undefined,"machine-detail-section");operations.append(el("h3","Operations"));const tools=[];
 if(item.kind!=="jumpbox")tools.push(optionAction("Run health checks",item.kind==="vm"?"scenario-health":"container-health",{name:item.name}));
 operations.append(workflowToolbar(tools),renderMachineSnapshots(p,item));content.append(operations);if(item.kind!=="jumpbox")content.append(renderMachineHealthChecks(p,item));
 const history=el("section",undefined,"machine-detail-section");history.append(el("h3","Recent machine jobs"));const relevant=jobs.filter(job=>job.project===Number(p.project_id)&&job.resource_key===item.resourceKey);
 if(relevant.length)history.append(actionTable(["Action","Status","Created"],relevant.map(job=>({job,cells:[job.operation.replaceAll("-"," "),job.status,new Date(job.created*1000).toLocaleString()]})),({job})=>[scenarioJob(job,"View output")]));else history.append(el("p","No machine-specific jobs in the loaded history. Project deployments are listed in Jobs.","muted"));content.append(history);
}

function consumeMachineLink(){
 const url=new URL(location.href);if(!url.searchParams.has("crange_box"))return;
 const projectId=Number(url.searchParams.get("crange_project")),key=url.searchParams.get("crange_box"),p=projects.find(candidate=>Number(candidate.project_id)===projectId);
 url.searchParams.delete("crange_project");url.searchParams.delete("crange_box");history.replaceState(history.state,"",url.pathname+url.search+url.hash);
 if(!p||!user?.admin&&!(user?.managed_projects||[]).map(Number).includes(projectId))return;
 const item=machineEntries(p).find(candidate=>candidate.resourceKey===key);if(!item)return;
 $("project").value=String(projectId);localStorage.setItem("crange.project",String(projectId));machineFocus={project:projectId,kind:item.kind,name:item.name};localStorage.setItem("crange.machine",JSON.stringify(machineFocus));view="machine-detail";localStorage.setItem("crange.view",view);teamFocus=null;
}
