"use strict";

const machineHealthStates=new Map();
function machineHealthKey(p,item){return `${Number(p.project_id)}:${item.kind}:${item.name}`}
function machineHealthSnapshot(item){return Array.isArray(item.box.health_checks)?item.box.health_checks.map(check=>({...check})):[]}
function machineHealthState(p,item){
 const key=machineHealthKey(p,item),revision=String(item.box.health_revision||"");let state=machineHealthStates.get(key);
 if(!state){state={revision,original:machineHealthSnapshot(item),rows:machineHealthSnapshot(item),error:"",status:"",message:"",jobId:null,resolvedJobId:null,awaitingRevision:false,savedRevision:null,stale:false,conflictSnapshot:null};machineHealthStates.set(key,state)}
 if(state.jobId){
  const job=Array.isArray(jobs)?jobs.find(candidate=>candidate.id===state.jobId):null;
  if(job){
   if(["queued","running","waiting-for-node","reconcile-required"].includes(job.status))state.status=job.status;
   else if(job.status==="succeeded"&&state.resolvedJobId!==job.id){state.status="succeeded";state.message="Health checks saved. A captured release keeps its captured checks; capture a new release to include this edit.";state.resolvedJobId=job.id;state.awaitingRevision=true;state.savedRevision=/New revision: ([a-f0-9]{64})/.exec(String(job.output||""))?.[1]||null}
   else if(["failed","interrupted"].includes(job.status)&&state.resolvedJobId!==job.id){state.status=job.status;state.resolvedJobId=job.id;state.error=job.status==="failed"?"The update failed. Your draft is preserved; review the job output, correct the checks, and try again.":"The update stopped before completion. Your draft is preserved; review the job before retrying."}
  }
 }
 if(revision&&state.status==="succeeded"&&state.awaitingRevision&&state.savedRevision===revision){
  state.revision=revision;state.original=machineHealthSnapshot(item);state.error="";state.stale=false;state.conflictSnapshot=null;state.awaitingRevision=false;
 }else if(revision&&revision!==state.revision){
  state.revision=revision;state.stale=true;state.conflictSnapshot=machineHealthSnapshot(item);state.error="Health checks changed elsewhere. Your draft is preserved; cancel to load the latest checks before saving.";
 }
 return state;
}

function renderMachineHealthChecks(p,item){
 const state=machineHealthState(p,item),section=el("section",undefined,"machine-detail-section machine-health"),heading=el("div",undefined,"machine-health-heading"),title=el("h3","Health checks"),note=el("p",undefined,"muted machine-health-note");
 heading.append(title);section.append(heading);
 const table=document.createElement("table"),head=document.createElement("thead"),body=document.createElement("tbody"),header=document.createElement("tr");
 for(const label of ["Type","Port","Host","Path","Expected HTTP","Timeout","Actions"]){const cell=document.createElement("th");cell.scope="col";cell.textContent=label;header.append(cell)}
 head.append(header);table.append(head,body);section.append(table);
 const empty=el("p","No checks are declared; Run health checks uses the default ping check.","muted machine-health-empty");
 const error=el("p",undefined,"machine-health-error");error.setAttribute("role","alert");error.setAttribute("aria-live","polite");error.textContent=state.error;
 const status=el("p",undefined,"machine-health-status");status.setAttribute("role","status");status.setAttribute("aria-live","polite");
 const controls=el("div",undefined,"machine-health-controls"),add=el("button",undefined,"quiet icon-button"),save=el("button","Save checks","primary"),cancel=el("button","Cancel","quiet");
 add.setAttribute("aria-label","Add check");add.dataset.noTooltip="true";add.append(actionIcon("plus"));heading.append(add);
 add.type=save.type=cancel.type="button";controls.append(save,cancel);section.append(empty,error,status,controls,note);


 function field(label,value,type="text",min,max){const input=document.createElement("input");input.type=type;input.value=value??"";input.setAttribute("aria-label",label);if(min!==undefined)input.min=String(min);if(max!==undefined)input.max=String(max);return input}
 function cellFor(label){const cell=document.createElement("td");cell.dataset.label=label;return cell}
 function draw(){
  const pending=["submitting","queued","running","waiting-for-node","reconcile-required"].includes(state.status),locked=pending||state.awaitingRevision;
  body.replaceChildren();empty.hidden=state.rows.length!==0;error.textContent=state.error;
  state.rows.forEach((row,index)=>{
   const tr=document.createElement("tr"),typeCell=cellFor("Type"),type=document.createElement("select");type.setAttribute("aria-label",`Check ${index+1} type`);
   for(const value of ["ping","tcp","http"]){const option=document.createElement("option");option.value=value;option.textContent=value.toUpperCase();type.append(option)}
   type.value=row.type||"ping";type.onchange=()=>{row.type=type.value;if(row.type!=="ping"&&!row.port)row.port=row.type==="http"?80:22;if(row.type==="http"){row.path=row.path||"/";row.status=row.status||200}draw()};type.disabled=locked;typeCell.append(type);tr.append(typeCell);
   const host=field(`Check ${index+1} HTTP host`,row.host||"");host.maxLength=253;host.placeholder="Optional hostname";host.disabled=locked||row.type!=="http";host.oninput=()=>row.host=host.value;
   const port=field(`Check ${index+1} port`,row.port,"number",1,65535),path=field(`Check ${index+1} path`,row.path||"/"),expected=field(`Check ${index+1} expected HTTP status`,row.status??200,"number",100,599),timeout=field(`Check ${index+1} timeout in seconds`,row.timeout??(row.type==="ping"?5:10),"number",1,60);
   port.step=expected.step=timeout.step="1";port.disabled=locked||row.type==="ping";path.disabled=locked||row.type!=="http";expected.disabled=locked||row.type!=="http";timeout.disabled=locked;path.maxLength=2048;
   port.oninput=()=>row.port=port.value;path.oninput=()=>row.path=path.value;expected.oninput=()=>row.status=expected.value;timeout.oninput=()=>row.timeout=timeout.value;
   const hostCell=cellFor("Host");hostCell.append(host);
   const portCell=cellFor("Port"),pathCell=cellFor("Path"),expectedCell=cellFor("Expected HTTP"),timeoutCell=cellFor("Timeout");
   portCell.append(port);pathCell.append(path);expectedCell.append(expected);timeoutCell.append(timeout);tr.append(portCell,hostCell,pathCell,expectedCell,timeoutCell);
   const actions=cellFor("Actions"),remove=el("button",undefined,"quiet icon-button");remove.type="button";remove.dataset.noTooltip="true";remove.setAttribute("aria-label",`Remove check ${index+1}`);remove.append(actionIcon("trash"));remove.disabled=locked;remove.onclick=()=>{state.rows.splice(index,1);state.error="";draw()};actions.append(remove);tr.append(actions);body.append(tr);
  });
  add.disabled=locked||state.rows.length>=12;save.disabled=locked;save.textContent=state.awaitingRevision?"Reloading saved checks…":pending?"Saving checks…":"Save checks";cancel.disabled=locked;
  status.textContent=state.status==="submitting"?"Submitting health-check update…":pending?"Update queued; waiting for task.":state.status==="succeeded"?state.message:"";
  status.hidden=!status.textContent;
  note.textContent="Saving changes the current project declaration only. Run health checks separately to test them.";
 }
 add.onclick=()=>{if(state.rows.length<12){state.rows.push({type:"ping",timeout:5});state.error="";draw()}};
 cancel.onclick=()=>{const source=state.stale&&state.conflictSnapshot?state.conflictSnapshot:state.original;state.rows=source.map(check=>({...check}));state.original=source.map(check=>({...check}));state.stale=false;state.conflictSnapshot=null;state.error="";draw()};
 save.onclick=async()=>{
  if(["submitting","queued","running","waiting-for-node","reconcile-required"].includes(state.status)||state.awaitingRevision)return;
  if(state.stale){draw();return}
  state.error="";state.message="";
  if(!/^[a-f0-9]{64}$/.test(String(item.box.health_revision||""))){state.error="Health-check revision is unavailable. Refresh the machine details before saving.";draw();return}
  if(state.error.includes("changed elsewhere")){draw();return}
  const checks=state.rows.map(row=>{const check={type:row.type};if(row.type!=="ping"&&row.port!==""&&row.port!==undefined)check.port=Number(row.port);if(row.type==="http"){check.path=row.path||"/";if(row.host?.trim())check.host=row.host.trim();if(row.status!==""&&row.status!==undefined)check.status=Number(row.status)}if(row.timeout!==""&&row.timeout!==undefined)check.timeout=Number(row.timeout);return check});
  state.status="submitting";state.jobId=null;state.resolvedJobId=null;state.awaitingRevision=false;state.savedRevision=null;state.message="";draw();
  try{
   const result=await queueJob("machine-health-update",{kind:item.kind,name:item.name,expected_revision:item.box.health_revision,checks},Number(p.project_id));
   state.jobId=result.job_id;state.status="queued";state.message="";render();
  }catch(cause){state.status="";state.error=cause?.message||"Could not submit the health-check update."}
  draw();
 };
 draw();return section;
}
