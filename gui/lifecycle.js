"use strict";

function projectLifecycleDialog(p, operation) {
 const restoring=operation==="project-restore",purging=operation==="project-purge";
 const title=restoring?"Restore project":purging?"Permanently delete project":"Delete project";
 userModal(`${title} · ${p.display_name||projectIdText(p)}`,(form,error,close)=>{
  const message=restoring
   ?"Return the saved project to the dashboard. Its definitions and releases will be available for redeployment. Deleted live disks are not recovered."
   :purging
   ?"Permanently remove this project’s configuration, releases, private images and access records. This cannot be undone. Shared platform templates and audit history are retained. Image removal requires all cluster nodes to be reachable and no remaining dependent guests."
   :"Stop and remove all of this project’s VMs, containers, routers, networks and current guest disks. Users will lose their sessions. Configuration and immutable release images are retained so an administrator can restore the project for redeployment. Changes in live disks that were not captured in a release will be lost.";
  form.append(el("p",message));
  const confirmation=userField(form,"confirmation",`Type ${projectIdText(p)} to confirm`,"","text",projectIdText(p));
  confirmation.required=true;confirmation.autocomplete="off";confirmation.spellcheck=false;
  const submit=el("button",title,purging||!restoring?"danger-action":"primary");submit.type="submit";submit.disabled=true;
  confirmation.oninput=()=>submit.disabled=confirmation.value!==projectIdText(p);
  form.append(submit);
  form.onsubmit=async event=>{
   event.preventDefault();if(confirmation.value!==projectIdText(p))return;submit.disabled=true;
   try{await queueJob(operation,{confirmation:confirmation.value},Number(p.project_id));close();if(!restoring){view="projects";localStorage.setItem("crange.view",view)}await load()}
   catch(e){error.textContent=e.message;submit.disabled=confirmation.value!==projectIdText(p)}
  };
 });
}

function renderProjectSettings(content,p){
 header("","Project settings");
 const section=el("section",undefined,"project-settings-section"),form=document.createElement("form");form.className="project-settings-form";
 section.append(el("h3","Project name"));
 const name=userField(form,"display_name","Display name",p.display_name||"","text",`Project ${p.project_id}`);name.maxLength=80;name.required=true;
 form.append(el("p",`Infrastructure retains the ${projectIdText(p)} identifier. The name is also shown in Guacamole.` ,"muted"));
 const save=el("button","Save name","primary"),error=el("p","","user-error");save.type="submit";error.setAttribute("role","alert");form.append(save,error);
 form.onsubmit=async event=>{event.preventDefault();save.disabled=true;try{await api("project-settings",{project:Number(p.project_id),display_name:name.value.trim()});await load();notice("Project name saved")}catch(e){error.textContent=e.message}finally{save.disabled=false}};
 section.append(form);
 const danger=el("section",undefined,"project-settings-section project-deletion"),remove=el("button","Delete project","danger-action");remove.type="button";remove.onclick=()=>projectLifecycleDialog(p,"project-delete");
 danger.append(el("h3","Delete project"),el("p","Remove this project from the dashboard and delete its running infrastructure and current disks. Saved configuration and release images remain available to administrators for restoration."),remove);
 content.append(section,danger);
}

function renderDeletedProjects(content){
 if(!user?.admin||!deletedProjects.length)return;
 const section=el("section",undefined,"deleted-projects");section.append(el("h3","Deleted projects"),el("p","Retained configuration and release images. Restore a project to redeploy it, or permanently remove its private resources.","muted"));
 const labels={deleted:"Deleted",deleting:"Deleting",delete_failed:"Deletion failed",purging:"Permanently deleting",purge_failed:"Permanent deletion failed"};
 const rows=deletedProjects.map(p=>{
  const state=p.lifecycle||{},busy=Boolean(state.pending_operation),actions=el("div",undefined,"action-row");
  const button=(label,op)=>{const b=el("button",label,op==="project-purge"?"quiet danger-action":"quiet");b.type="button";b.disabled=busy;b.onclick=()=>projectLifecycleDialog(p,op);return b};
  if(state.state==="deleted")actions.append(button("Restore","project-restore"));
  if(["deleted","purge_failed"].includes(state.state))actions.append(button("Permanently delete","project-purge"));
  if(["delete_failed","deleting"].includes(state.state)&&!busy)actions.append(button("Retry deletion","project-delete"));
  if(state.job_id){const job=el("button","View job","table-link");job.type="button";job.onclick=()=>showJob(state.job_id,state.pending_operation||"project-delete");actions.append(job)}
  const status=el("span",state.waiting_nodes?.length?"Waiting for "+state.waiting_nodes.join(", "):busy?state.pending_operation.replace("project-","").replaceAll("-"," ")+" · queued/running":labels[state.state]||state.state);
  if(state.error){const detail=el("p",state.error,"user-error");status.append(detail)}
  return[projectTitle(p),status,state.deleted_at?new Date(state.deleted_at*1000).toLocaleString():"—",p.release_count??0,actions];
 });
 section.append(table(["Project","Status","Deleted","Releases","Actions"],rows));content.append(section);
}

const projectLifecycleRender=render;
render=()=>{
 projectLifecycleRender();
 if(view==="projects"){
  if(user?.admin)document.querySelectorAll(".project-list .project-card").forEach((card,index)=>{
   const p=projects[index];if(!p)return;
   const actions=el("div",undefined,"action-row");
   const open=card.querySelector("button.primary");if(open)actions.append(open);
   for(const [label,op] of [["Delete project","project-delete"]]){
    const button=el("button",label,"quiet danger-action");button.type="button";
    button.dataset.help="Delete project infrastructure and hide it from the dashboard. Keep configuration and release images for restoration.";
    button.onclick=()=>projectLifecycleDialog(p,op);actions.append(button);
   }card.append(actions);
  });
  renderDeletedProjects($("content"));
 }
 if(view==="project-settings"){
  const p=project();if(!p||p.participant)return;
  $("content").replaceChildren();renderProjectSettings($("content"),p);
 }
};
