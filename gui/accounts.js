"use strict";

function createDashboardAccount(){
 userModal("Create dashboard user",(form,error,close)=>{
  const username=userField(form,"username","Username","","text","cig-lead");username.required=true;username.autocomplete="off";
  const password=userField(form,"password","Temporary password","","password","At least 12 characters");password.required=true;password.minLength=12;password.autocomplete="new-password";
  const access=userField(form,"projects","Project access","","text","100, 101");access.required=true;
  const role=userSelect(form,"role","Project role",[["participant","Participant"],["manager","Manager"]],"participant");
  const submit=el("button","Create user","primary");submit.type="submit";form.append(submit);
  form.onsubmit=async event=>{event.preventDefault();submit.disabled=true;try{
   await api("users",{action:"create",username:username.value.trim().toLowerCase(),password:password.value,projects:parseProjectAccess(access.value),role:role.value,admin:false});password.value="";await load();close();notice("Dashboard user created");
  }catch(e){error.textContent=e.message}finally{submit.disabled=false}};
 });
}

function manageDashboardAccount(account){
 userModal(account.username,(form,error,close)=>{
  form.append(el("p",account.admin?"Platform administrator · access to every project":"Dashboard account. Project Guacamole accounts are managed separately.","muted"));
  const roles=new Map(Object.entries(account.roles||{}));
  const access=account.admin?null:userField(form,"projects","Project access",(account.projects||[]).join(", "),"text","100, 101");
  const roleFields=el("div",undefined,"account-project-roles");
  function showRoles(){
   let ids;try{ids=parseProjectAccess(access.value,true)}catch(_){return}
   roleFields.replaceChildren();
   for(const id of ids){
    const p=projects.find(p=>Number(p.project_id)===id),label=`${p?.display_name||"Project"} (p${String(id).padStart(3,"0")})`;
    const select=userSelect(roleFields,"role_"+id,label,[["participant","Participant"],["manager","Manager"]],roles.get(String(id))||"participant");
    select.onchange=()=>roles.set(String(id),select.value);
   }
  }
  if(access){access.oninput=showRoles;showRoles();form.append(roleFields)}
  const enabled=userField(form,"enabled","Account enabled",account.enabled,"checkbox");enabled.disabled=account.username===user.username;
  const save=el("button","Save changes","primary");save.type="submit";form.append(save);
  form.onsubmit=async event=>{event.preventDefault();save.disabled=true;try{
   if(access){
    const ids=parseProjectAccess(access.value,true),old=(account.projects||[]).map(Number);
    if(JSON.stringify(ids)!==JSON.stringify(old))await api("users",{username:account.username,projects:ids,teams:Object.fromEntries(Object.entries(account.teams||{}).filter(([id])=>ids.includes(Number(id))))});
    for(const id of ids){const role=roles.get(String(id))||"participant";if(role!==(account.roles?.[String(id)]||"participant"))await api("users",{action:"set-role",username:account.username,project:id,role})}
   }
   if(enabled.checked!==account.enabled)await api("users",{action:"set-enabled",username:account.username,enabled:enabled.checked});
   await load();close();notice("Dashboard user updated");
  }catch(e){error.textContent=e.message}finally{save.disabled=false}};
  if(account.username!==user.username){
   const remove=el("button","Delete account","quiet danger-action");remove.type="button";
   remove.onclick=()=>userModal(`Delete ${account.username}`,(confirm,problem,done)=>{
    confirm.append(el("p","Remove this dashboard account and its dashboard sessions. Project Guacamole accounts and guest machines remain."));
    const button=el("button","Delete account","danger-action");button.type="submit";confirm.append(button);
    confirm.onsubmit=async event=>{event.preventDefault();button.disabled=true;try{await api("users",{action:"delete",username:account.username});await load();done();notice("Dashboard user deleted")}catch(e){problem.textContent=e.message;button.disabled=false}};
   });form.append(remove);
  }
 });
}

function renderDashboardAccounts(content){
 header("","CRANGE Users");
 const toolbar=el("div",undefined,"account-toolbar"),count=el("span",`${directory.length} account${directory.length===1?"":"s"}`,"muted"),add=el("button",undefined,"icon-button has-action-icon primary");
 add.type="button";add.setAttribute("aria-label","Create dashboard user");add.dataset.help="Create a dashboard account and assign project access. Guacamole accounts are created within projects.";
 add.append(actionIcon("plus"));add.onclick=createDashboardAccount;toolbar.append(count,add);content.append(toolbar);
 const rows=directory.map(account=>{
  const name=el("button",account.username,"table-link");name.type="button";name.onclick=()=>manageDashboardAccount(account);
  const roles=new Set((account.projects||[]).map(p=>account.roles?.[p]||"participant"));
  const role=account.admin?"Administrator":roles.size>1?"Mixed roles":roles.has("manager")?"Manager":"Participant";
  const access=account.admin?"All projects":(account.projects||[]).map(id=>{const p=projects.find(p=>String(p.project_id)===String(id));return p?`${p.display_name} (p${String(id).padStart(3,"0")})`:`p${String(id).padStart(3,"0")}`}).join(", ")||"No project access";
  const manage=el("button","Manage","quiet");manage.type="button";manage.onclick=()=>manageDashboardAccount(account);
  return[name,role,access,statusBadge(account.enabled?"Enabled":"Disabled")||el("span","Disabled"),manage];
 });
 content.append(table(["Username","Access","Projects","Status",""],rows));
 if(!rows.length)content.append(el("p","No dashboard users.","empty"));
 if(audit.length){const details=el("details",undefined,"account-audit");details.append(el("summary","Access audit"),table(["Action","Actor","Account","Time"],audit.map(e=>[e.action,e.actor,e.target,new Date(e.created*1000).toLocaleString()])));content.append(details)}
}
