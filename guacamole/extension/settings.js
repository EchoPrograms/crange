/* Native Guacamole administration, with CRANGE navigation and account chrome.
 * Controllers retain permission checks, filtering, editing and audit behavior. */
(function () {
    'use strict';
    var accountIcon = '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true"><circle cx="10" cy="6" r="3"/><path d="M4 18v-2a6 6 0 0 1 12 0v2"/></svg>';
    var chevron = '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true"><path d="m5 8 5 5 5-5"/></svg>';
    var account = `
<div class="user-menu cr-account" ng-show="!isAnonymous()" crange-account-menu>
  <button type="button" class="cr-account-trigger" ng-click="accountMenu.toggle()" aria-label="Account menu for {{username}}" aria-haspopup="menu" aria-expanded="{{accountMenu.open}}">${accountIcon}<span>{{username}}</span>${chevron}</button>
  <div class="cr-account-popover" popover="manual" role="menu" aria-label="Account menu">
    <div class="cr-account-profile" ng-if="fullName"><a ng-if="userURL" ng-href="{{userURL}}">{{fullName}}</a><strong ng-if="!userURL">{{fullName}}</strong><span ng-if="role">{{role}}</span><span ng-if="organization">{{organization}}</span></div>
    <div class="cr-account-local" ng-if="localActions.length"><button type="button" role="menuitem" ng-repeat="action in localActions" ng-click="accountMenu.close(); action.callback()" ng-class="action.className">{{action.name | translate}}</button></div>
    <div class="cr-account-pages" ng-click="accountMenu.close()"><guac-page-list pages="pages"></guac-page-list></div>
    <div class="cr-account-actions"><button type="button" role="menuitem" ng-repeat="action in actions" ng-click="accountMenu.close(); action.callback()" ng-class="action.className">{{action.name | translate}}</button></div>
  </div>
</div>`;
    var settings = `
<div class="settings-view view cr-admin-settings" crange-settings-shell>
  <header class="cr-settings-header">
    <div class="cr-settings-heading"><img src="app/ext/crange-theme/assets/remote-access.svg?v=3" alt="" width="32" height="32"><h1>{{'SETTINGS.SECTION_HEADER_SETTINGS' | translate}}</h1></div>
    <div class="cr-settings-header-actions"><button type="button" class="cr-settings-back" ng-click="settingsBack()"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true"><path d="M16 10H4m5-5-5 5 5 5"/></svg><span>Back to project</span></button><a class="cr-controller-link" href="https://crange.brogano.dev/" target="_blank" rel="noopener noreferrer">CRANGE Control<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true"><path d="M11 3h6v6m0-6-8 8M8 4H4v12h12v-4"/></svg></a><guac-user-menu></guac-user-menu></div>
  </header>
  <div class="cr-settings-layout">
    <nav class="cr-settings-nav" aria-label="Settings sections"><guac-page-list pages="settingsPages"></guac-page-list></nav>
    <main class="cr-settings-content">
      <guac-settings-users ng-if="activeTab === 'users'"></guac-settings-users>
      <guac-settings-user-groups ng-if="activeTab === 'userGroups'"></guac-settings-user-groups>
      <guac-settings-connections ng-if="activeTab === 'connections'"></guac-settings-connections>
      <guac-settings-connection-history ng-if="activeTab === 'history'"></guac-settings-connection-history>
      <guac-settings-sessions ng-if="activeTab === 'sessions'"></guac-settings-sessions>
      <guac-settings-preferences ng-if="activeTab === 'preferences'"></guac-settings-preferences>
    </main>
  </div>
</div>`;
    var templates = {'app/navigation/templates/guacUserMenu.html': account, 'app/settings/templates/settings.html': settings};
    angular.module('navigation').config(['$provide', function ($provide) {
        $provide.decorator('$templateCache', ['$delegate', function (cache) {
            var put = cache.put;
            cache.put = function (key, value) { return put.call(cache, key, templates[key] || value); };
            return cache;
        }]);
    }]).run(['$templateCache', function (cache) {
        Object.keys(templates).forEach(function (key) { cache.put(key, templates[key]); });
    }]).directive('crangeAccountMenu', ['$window', function ($window) {
        return {restrict: 'A', link: function (scope, element) {
            var root=element[0], trigger=root.querySelector('.cr-account-trigger'), menu=root.querySelector('.cr-account-popover');
            var state=scope.accountMenu={open:false};
            function choices(){return Array.prototype.filter.call(menu.querySelectorAll('a[href],button:not(:disabled)'),function(item){return item.getClientRects().length;});}
            state.close=function(focus){state.open=false;if(menu.hidePopover && menu.matches(':popover-open'))menu.hidePopover();menu.style.display='none';if(focus)trigger.focus();};
            state.toggle=function(){
                if(state.open){state.close();return;}
                state.open=true;menu.style.display='block';menu.style.visibility='hidden';
                if(menu.showPopover)menu.showPopover();
                var r=trigger.getBoundingClientRect();
                menu.style.left=Math.max(8,Math.min(r.right-menu.offsetWidth,$window.innerWidth-menu.offsetWidth-8))+'px';
                menu.style.top=Math.max(8,Math.min(r.bottom+6,$window.innerHeight-menu.offsetHeight-8))+'px';
                menu.style.visibility='visible';
            };
            function outside(event){if(state.open&&!root.contains(event.target))scope.$evalAsync(function(){state.close();});}
            function key(event){
                if(!root.contains(event.target))return;
                if(event.key==='Escape'&&state.open){event.preventDefault();event.stopPropagation();scope.$evalAsync(function(){state.close(true);});return;}
                if(['ArrowDown','ArrowUp','Home','End'].indexOf(event.key)<0)return;
                event.preventDefault();event.stopPropagation();
                scope.$evalAsync(function(){
                    if(!state.open)state.toggle();
                    var items=choices(),index=items.indexOf(document.activeElement);
                    if(!items.length)return;
                    index=event.key==='Home'?0:event.key==='End'?items.length-1:event.key==='ArrowUp'?(index<=0?items.length-1:index-1):(index+1)%items.length;
                    items[index].focus();
                });
            }
            function focusout(event){if(state.open&&event.relatedTarget&&!root.contains(event.relatedTarget))scope.$evalAsync(function(){state.close();});}
            function resize(){if(state.open)scope.$evalAsync(function(){state.close();});}
            var observer=new MutationObserver(function(){menu.querySelectorAll('a[href]').forEach(function(link){link.setAttribute('role','menuitem');});});observer.observe(menu,{childList:true,subtree:true});
            menu.style.display='none';document.addEventListener('pointerdown',outside,true);root.addEventListener('keydown',key);root.addEventListener('focusout',focusout);$window.addEventListener('resize',resize);
            scope.$on('$destroy',function(){state.close();observer.disconnect();document.removeEventListener('pointerdown',outside,true);root.removeEventListener('keydown',key);root.removeEventListener('focusout',focusout);$window.removeEventListener('resize',resize);});
        }};
    }]);
    angular.module('settings').directive('crangeSettingsShell', ['$location', '$window', function ($location,$window) {
        return {restrict:'A',link:function(scope){scope.settingsBack=function(){
            try{var project=$window.localStorage.getItem('crange-selected-project');if(/^\d{1,3}$/.test(project))$window.sessionStorage.setItem('crange-return-project',project);}catch(ignored){}
            $location.url('/');
        };}};
    }]);
}());
