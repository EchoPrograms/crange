"use strict";

// Keep browser history aligned with the existing SPA globals. URLs stay intact,
// so authenticated and externally issued deep links continue to work unchanged.
(()=>{
 const STATE_KEY="crangeNavigation",PROJECT_VIEWS=new Set(["project-overview","teams","team-detail","scenario","machines","machine-detail","project-users","project-user-detail","project-settings"]),ADMIN_VIEWS=new Set(["users","templates","cluster"]),SCENARIO_TABS=new Set(["workspace","releases","deployments"]);
 const safeViews=new Set(["overview","projects","jobs",...ADMIN_VIEWS,...PROJECT_VIEWS]);
 let lastRoute=null,initialized=false,restoring=false,popSequence=0;
 if("scrollRestoration" in history)history.scrollRestoration="manual";

 function currentRoute(){
  const selected=Number($("project").value);
  return {version:1,view,project:Number.isSafeInteger(selected)&&selected>0?selected:null,
   team:teamFocus===null?null:Number(teamFocus),user:typeof guacFocus==="string"?guacFocus:null,
   machine:machineFocus&&typeof machineFocus==="object"?{project:Number(machineFocus.project),kind:String(machineFocus.kind),name:String(machineFocus.name)}:null,
   scenarioTab:project()?scenarioState(project()).tab:null,scrollX:window.scrollX,scrollY:window.scrollY};
 }
 function routeKey(route){return JSON.stringify([route.view,route.project,route.team,route.user,route.machine,route.scenarioTab])}
 function writeState(method,route,state=history.state){
  const previous=state&&typeof state==="object"?state:{};
  history[method]({...previous,[STATE_KEY]:true,route},"",location.href);
 }
 function trackRender(){
  const route=currentRoute();
  if(!initialized){writeState("replaceState",route);lastRoute=route;initialized=true;return}
  if(restoring){lastRoute=route;return}
  if(routeKey(route)!==routeKey(lastRoute))writeState("pushState",route);
  else writeState("replaceState",route);
  lastRoute=route;
 }
 const existingRender=render;
 render=function(...args){const result=existingRender.apply(this,args);trackRender();return result};

 addEventListener("scroll",()=>{
  if(restoring||!history.state?.[STATE_KEY]||!history.state.route)return;
  const route={...history.state.route,scrollX:window.scrollX,scrollY:window.scrollY};
  writeState("replaceState",route);if(lastRoute)lastRoute={...lastRoute,scrollX:route.scrollX,scrollY:route.scrollY};
 },{passive:true});

 function authorizedProject(id){return projects.find(item=>Number(item.project_id)===Number(id))||null}
 function selectProject(id){
  const picker=$("project");picker.value=String(id??"");
  if(id&&picker.value!==String(id))picker.replaceChildren(new Option("No projects available",""));
  if(id)localStorage.setItem("crange.project",String(id));else localStorage.removeItem("crange.project");
 }
 function setView(target){view=target;localStorage.setItem("crange.view",view)}
 function restoreFallback(message){
  guacFocus=null;teamFocus=null;machineFocus=null;localStorage.removeItem("crange.team");localStorage.removeItem("crange.machine");
  if(!authorizedProject($("project").value)&&projects[0])selectProject(projects[0].project_id);
  else if(authorizedProject($("project").value))localStorage.setItem("crange.project",String($("project").value));
  else selectProject(null);
  setView(projects.length?"project-overview":"projects");
  if(message)$("error").textContent=message;
 }
 async function restoreHistoryEntry(event){
  const saved=event.state?.[STATE_KEY]&&event.state.route;
  if(!saved||saved.version!==1)return;
  const sequence=++popSequence;restoring=true;
  let scroll={x:0,y:0},message="";
  try{
   // Recheck current authorization before applying a saved project context.
   const session=await api("session"),records=(await api("projects")).projects;
   if(sequence!==popSequence)return;
   user=session;projects=records.filter(p=>!p.deleted);deletedProjects=records.filter(p=>p.deleted);
   const picker=$("project");picker.replaceChildren(...(projects.length?projects.map(p=>new Option(`${p.display_name||`Project ${p.project_id}`} (p${String(p.project_id).padStart(3,"0")})`,p.project_id)):[new Option("No projects available","")]));
   const targetView=safeViews.has(saved.view)?saved.view:"overview";
   if((ADMIN_VIEWS.has(targetView)&&!user.admin)||(targetView==="jobs"&&!user.admin&&!user.managed_projects?.length)){restoreFallback("That page is no longer available to this account.");message="That page is no longer available to this account."}
   else if(saved.project!==null&&!authorizedProject(saved.project)){
    restoreFallback("That project is no longer available to this account.");message="That project is no longer available to this account.";
   }else{
    const targetProject=saved.project===null?null:authorizedProject(saved.project);
    selectProject(targetProject?.project_id??null);
    setView(PROJECT_VIEWS.has(targetView)&&!targetProject?(projects.length?"project-overview":"projects"):targetView);
    const p=project();teamFocus=saved.team===null?null:Number(saved.team);
    if(teamFocus!==null&&p&&!((teamFocus===254)||p.team_ids?.some(id=>Number(id)===teamFocus))){teamFocus=null;if(view==="team-detail")setView("teams")}
    localStorage.setItem("crange.team",teamFocus===null?"":String(teamFocus));if(teamFocus===null)localStorage.removeItem("crange.team");
    machineFocus=saved.machine&&Number(saved.machine.project)===Number(p?.project_id)?saved.machine:null;
    if(machineFocus)localStorage.setItem("crange.machine",JSON.stringify(machineFocus));else localStorage.removeItem("crange.machine");
    guacFocus=typeof saved.user==="string"?saved.user:null;
    const needsDirectory=Boolean(p&&["project-users","project-user-detail"].includes(view));
    if(needsDirectory)await refreshGuacDirectory();
    if(sequence!==popSequence)return;
    if(view==="project-user-detail"&&!guacDirectory.some(record=>record.username===guacFocus)){guacFocus=null;setView("project-users")}
    if(view==="project-user-detail"){
     userTelemetry={loading:true};await refreshUserTelemetry();
     if(sequence!==popSequence)return;
    }
    if(view==="machine-detail"&&(!machineFocus||!machineEntries(p).some(item=>item.kind===machineFocus.kind&&item.name===machineFocus.name))){machineFocus=null;localStorage.removeItem("crange.machine");setView("machines")}
    if(view==="team-detail"&&teamFocus===null)setView("teams");
    if(view==="scenario"){
     const tab=SCENARIO_TABS.has(saved.scenarioTab)?saved.scenarioTab:"workspace";
     scenarioState(p).tab=tab;
    }
   }
   localStorage.setItem("crange.view",view);
   $("session-user").textContent=user.username;
   document.querySelectorAll("[data-admin-only]").forEach(node=>node.hidden=!user.admin);
   document.querySelectorAll(".resource-overlay,#user-overlay").forEach(node=>node.remove());
   $("action-panel").hidden=true;
   $("job-overlay").hidden=true;activeJobId=null;pinnedJob=null;
   scroll={x:Number.isFinite(saved.scrollX)?saved.scrollX:0,y:Number.isFinite(saved.scrollY)?saved.scrollY:0};
   $("error").textContent=message;
   render();
   const normalized=currentRoute();writeState("replaceState",normalized,event.state);lastRoute=normalized;
   requestAnimationFrame(()=>requestAnimationFrame(()=>{if(sequence===popSequence)window.scrollTo(scroll.x,scroll.y)}));
  }catch(error){
   if(sequence!==popSequence)return;
   projects=[];deletedProjects=[];guacDirectory=[];guacFocus=null;teamFocus=null;machineFocus=null;selectProject(null);setView("projects");
   $("error").textContent=`Could not restore this page: ${error.message}`;render();
   const normalized=currentRoute();writeState("replaceState",normalized,event.state);lastRoute=normalized;
  }finally{if(sequence===popSequence)restoring=false}
 }
 addEventListener("popstate",event=>{restoreHistoryEntry(event)});
})();
