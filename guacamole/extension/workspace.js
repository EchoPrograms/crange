/* Presentation extensions for Guacamole 1.6. Native clients, clipboard, file
 * transfer, authentication and permission checks remain Guacamole-owned. */
(function () {
    'use strict';

    var icons = {
        back: '<path d="M16 10H4m5-5-5 5 5 5"/>',
        home: '<path d="m3 9 7-6 7 6v8h-5v-5H8v5H3z"/>',
        options: '<path d="M3 5h14M3 10h14M3 15h14"/><circle cx="7" cy="5" r="2"/><circle cx="13" cy="10" r="2"/><circle cx="7" cy="15" r="2"/>',
        network: '<rect x="7" y="2" width="6" height="5" rx="1"/><rect x="2" y="13" width="6" height="5" rx="1"/><rect x="12" y="13" width="6" height="5" rx="1"/><path d="M10 7v3M5 13v-3h10v3"/>',
        close: '<path d="m5 5 10 10M15 5 5 15"/>',
        fullscreen: '<path d="M7 3H3v4m10-4h4v4M3 13v4h4m10-4v4h-4"/>',
        drag: '<path d="M7 4h0m6 0h0M7 10h0m6 0h0M7 16h0m6 0h0" stroke-width="3"/>',
        copy: '<rect x="7" y="7" width="10" height="10" rx="1"/><path d="M13 7V3H3v10h4"/>'
    };
    function icon(name) { return '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + icons[name] + '</svg>'; }
    function button(name, label, click) { return '<button type="button" class="cr-session-icon" aria-label="' + label + '" crange-help="' + label + '" ng-click="' + click + '">' + icon(name) + '</button>'; }

    var preferences = `
<div class="cr-preferences" crange-preference-fields>
  <button type="button" class="cr-preferences-back" ng-click="backFromPreferences()">${icon('back')}<span>Back</span></button>
  <div class="cr-preference-grid">
    <section class="cr-setting-group"><h2>Appearance</h2>
      <p class="cr-help">CRANGE dark · Monochrome</p>
      <label>Machine layout<select ng-model="presentation.machineLayout"><option value="grid">Grid</option><option value="list">List</option></select></label>
      <label>Default display scale<input type="number" min="0.1" max="3" step="0.1" ng-model="presentation.displayScale"></label>
      <p class="cr-help">1 is the original size. Fit to window is available during a session.</p>
    </section>
    <section class="cr-setting-group"><h2>Session</h2>
      <label>Timezone<input ng-model="preferences.timezone" placeholder="e.g. America/Chicago"></label>
      <div class="cr-native-language"><guac-form content="languageField" model="preferences" namespace="'SETTINGS_PREFERENCES'"></guac-form></div>
      <label>Input method<select ng-model="preferences.inputMethod"><option value="none">Direct keyboard</option><option value="text">Text input / IME</option><option value="osk">On-screen keyboard</option></select></label>
      <label>Touch input<select ng-model="preferences.emulateAbsoluteMouse" ng-options="choice.value as choice.label for choice in mouseChoices"></select></label>
      <label>Home button<select ng-model="presentation.exitBehavior"><option value="home">Return to project, keep session connected</option><option value="disconnect">Disconnect and return to project</option></select></label>
    </section>
    <section class="cr-setting-group"><h2>Account</h2>
      <p class="cr-help" ng-if="canUpdateSelf === null">Checking account permissions…</p>
      <p class="cr-help" ng-if="canUpdateSelf === false">Password changes are managed by your project administrator.</p>
      <form ng-if="canUpdateSelf" ng-submit="updatePassword()">
        <label>Current password<input type="password" ng-model="$parent.oldPassword" autocomplete="current-password" required></label>
        <label>New password<input type="password" ng-model="$parent.newPassword" autocomplete="new-password" minlength="6" required></label>
        <label>Confirm new password<input type="password" ng-model="$parent.newPasswordMatch" autocomplete="new-password" minlength="6" required></label>
        <button type="submit" ng-disabled="!newPassword || !oldPassword || newPassword !== newPasswordMatch">Update password</button>
      </form>
      <details ng-if="canUpdateSelf && attributes.length"><summary>Additional account settings</summary><guac-form namespace="'USER_ATTRIBUTES'" content="attributes" model="user.attributes"></guac-form><button type="button" ng-click="saveUser()">Save account settings</button></details>
    </section>
  </div>
  <div class="cr-preference-footer"><p role="status" ng-class="{'cr-error': preferenceError}">{{preferenceMessage}}</p><button type="button" class="cr-primary" ng-click="savePresentation()">Save preferences</button></div>
</div>`;

    // Transform the native templates instead of replacing their controllers.
    // Keeping the original client markup also retains optional extensions.
    function transform(key, value) {
        if (key === 'app/settings/templates/settingsPreferences.html') return preferences;
        if (key === 'app/client/templates/client.html' && value.indexOf('<crange-session-toolbar') === -1) {
            return value.replace('<guac-viewport>', '<guac-viewport><crange-session-toolbar></crange-session-toolbar>')
                .replace('ng-class="{open: menu.shown}" id="guac-menu"', 'ng-class="{open: menu.shown && crSession.advanced}" id="guac-menu"')
                .replace('ng-if="menu.shown" guac-touch-drag="menuDrag"', 'ng-if="menu.shown && crSession.advanced" guac-touch-drag="menuDrag"');
        }
        return value;
    }
    angular.module('client').config(['$provide', function ($provide) {
        $provide.decorator('$templateCache', ['$delegate', function (cache) {
            var put = cache.put;
            cache.put = function (key, value) { return put.call(cache, key, transform(key, value)); };
            return cache;
        }]);
    }]).run(['$templateCache', function (cache) {
        ['app/client/templates/client.html', 'app/settings/templates/settingsPreferences.html'].forEach(function (key) {
            var existing = cache.get(key);
            if (typeof existing === 'string') cache.put(key, existing);
        });
    }]);

    angular.module('settings').directive('crangePreferenceFields', ['preferenceService', 'languageService', '$location', function (preferenceService, languageService, $location) {
        return {restrict: 'A', link: function (scope) {
            var p = preferenceService.preferences;
            scope.backFromPreferences = function () {
                var parent=scope;
                while(parent){if(parent.cr && typeof parent.cr.back === 'function'){parent.cr.back();return;}if(typeof parent.settingsBack==='function'){parent.settingsBack();return;}parent=parent.$parent;}
                $location.url('/');
            };
            languageService.getLanguages().then(function (languages) { if (!languages[p.language]) { var base = String(p.language || 'en').split('_')[0]; if (languages[base]) p.language = base; } });
            scope.languageField = [{type: 'LANGUAGE', name: 'language'}];
            scope.presentation = {
                machineLayout: p.crangeMachineLayout === 'list' ? 'list' : 'grid',
                displayScale: Number(p.crangeDisplayScale) || 1,
                exitBehavior: p.crangeExitBehavior === 'disconnect' ? 'disconnect' : 'home',
            };
            scope.mouseChoices = [{value: true, label: 'Touchscreen'}, {value: false, label: 'Touchpad'}];
            scope.savePresentation = function () {
                var values = scope.presentation;
                scope.preferenceError = true;
                if (!Number.isFinite(values.displayScale) || values.displayScale < 0.1 || values.displayScale > 3) { scope.preferenceMessage = 'Display scale must be between 0.1 and 3.'; return; }
                try { new Intl.DateTimeFormat('en', {timeZone: p.timezone}); }
                catch (error) { scope.preferenceMessage = 'Enter a valid timezone, for example America/Chicago.'; return; }
                p.crangeMachineLayout = values.machineLayout;
                p.crangeDisplayScale = values.displayScale;
                p.crangeExitBehavior = values.exitBehavior;
                preferenceService.save();
                scope.preferenceError = false;
                scope.preferenceMessage = 'Preferences saved for this browser.';
            };
        }};
    }]);

    var toolbar = `
<div class="cr-session-shell" ng-class="{'align-right': crSession.alignment === 'right', 'is-collapsed': !crSession.visible}">
  <div class="cr-session-toolbar" ng-show="crSession.visible" role="toolbar" aria-label="Remote session controls">
    <button type="button" class="cr-session-icon cr-session-drag" aria-label="Collapse session controls" aria-expanded="true" crange-help="Click to collapse; drag to move. Arrow keys move the toolbar; Home resets its position">${icon('drag')}</button>
    ${button('home', 'Return to project', 'crSession.home()')}
    <button type="button" class="cr-session-disconnect" ng-click="crSession.disconnect()" crange-help="Disconnect the current remote session. The server keeps running">Disconnect</button>
    <button type="button" class="cr-session-name cr-session-selector" ng-click="crSession.toggle('connections')" aria-expanded="{{crSession.panel === 'connections'}}" aria-label="Select connections"><span>{{focusedClient.name || getName(clientGroup)}}</span><span class="cr-protocol-label" ng-if="focusedClient.protocol">{{focusedClient.protocol | uppercase}}</span><span class="cr-session-count" ng-if="clientGroup.clients.length > 1">{{clientGroup.clients.length}} selected</span><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="m5 8 5 5 5-5"/></svg></button>
    <div class="cr-session-power" ng-if="crSession.box"><button type="button" ng-click="crSession.toggle('power')" aria-expanded="{{crSession.panel === 'power'}}"><span class="cr-status" ng-class="{on: crSession.box.state === 'running'}">{{crSession.box.state || 'Checking…'}}</span><span aria-hidden="true">⌄</span></button></div>
    ${button('network', 'Network map', "crSession.toggle('network')")}
    ${button('fullscreen', 'Toggle fullscreen', 'crSession.fullscreen()')}
    <button type="button" ng-click="crSession.toggle('options')" aria-expanded="{{crSession.panel === 'options'}}">${icon('options')}<span>Options</span></button>
    ${button('close', 'Hide session toolbar (Ctrl+Alt+Shift to toggle)', 'crSession.hide()')}
  </div>
  <button type="button" class="cr-session-reopen" ng-show="!crSession.visible" aria-label="Show session controls" aria-expanded="false" crange-help="Click to expand; drag along the screen edge. Arrow keys move the tab"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true"><path d="m7 4 6 6-6 6"/></svg></button>
  <section class="cr-session-popover cr-session-connections" ng-if="crSession.panel === 'connections'" aria-label="Select connections">
    <header><h2>Connections</h2>${button('close', 'Close connection selector', 'crSession.close()')}</header>
    <p class="cr-help">Select connections to view together. Unchecking removes a connection from this view.</p>
    <div ng-if="rootConnectionGroups">
      <guac-group-list-filter connection-groups="rootConnectionGroups" filtered-connection-groups="filteredRootConnectionGroups" placeholder="'Search connections'" connection-properties="filteredConnectionProperties" connection-group-properties="filteredConnectionGroupProperties"></guac-group-list-filter>
      <guac-group-list connection-groups="filteredRootConnectionGroups" context="connectionListContext" templates="{'connection':'app/client/templates/connection.html','connection-group':'app/client/templates/connectionGroup.html'}" page-size="15"></guac-group-list>
    </div>
    <p class="cr-help" ng-if="!rootConnectionGroups">The current connection is the only connection available in this session.</p>
  </section>
  <section class="cr-session-popover cr-session-options" ng-if="crSession.panel === 'options'" aria-label="Session options">
    <header><h2>Session options</h2>${button('close', 'Close session options', 'crSession.close()')}</header>
    <label>Clipboard<guac-clipboard></guac-clipboard></label>
    <div class="cr-session-fields">
      <label>Display scale<input type="number" min="0.1" max="3" step="0.1" ng-model="crSession.displayScale" ng-change="crSession.setScale()"></label>
      <label>Input method<select ng-model="menu.inputMethod"><option value="none">Direct keyboard</option><option value="text">Text input / IME</option><option value="osk">On-screen keyboard</option></select></label>
      <label class="cr-check"><input type="checkbox" ng-model="focusedClient.clientProperties.autoFit">Fit to window</label>
      <label>Touch input<select ng-model="menu.emulateAbsoluteMouse" ng-options="choice.value as choice.label for choice in crSession.mouseChoices"></select></label>
    </div>
    <button type="button" ng-click="crSession.openAdvanced()">Advanced controls</button>
    <p class="cr-help">Advanced controls include devices, file transfer, sharing and protocol settings.</p>
  </section>
  <section class="cr-session-popover cr-session-power-menu" ng-if="crSession.panel === 'power'" aria-label="Server power controls">
    <header><h2>Server power</h2>${button('close', 'Close power controls', 'crSession.close()')}</header>
    <button type="button" ng-if="crSession.box.state === 'stopped'" ng-click="crSession.power('start')" ng-disabled="crSession.busy">Start</button>
    <button type="button" ng-if="crSession.box.state === 'running'" ng-click="crSession.power('shutdown')" ng-disabled="crSession.busy">Shut down safely</button>
    <button type="button" ng-if="crSession.box.state === 'running' || crSession.action === 'shutdown'" ng-click="crSession.power('stop')" ng-disabled="crSession.busy && crSession.action !== 'shutdown'" class="cr-danger">Force stop</button>
    <button type="button" ng-if="crSession.box.state === 'running'" ng-click="crSession.power('restart')" ng-disabled="crSession.busy">Restart</button>
    <p class="cr-help">Power actions affect the server and all connected users. Snapshot management is available on its project page.</p>
  </section>
  <section class="cr-session-popover cr-session-network" ng-if="crSession.panel === 'network'" aria-label="Network map">
    <header><h2>Network map</h2>${button('close', 'Close network map', 'crSession.close()')}</header>
    <p class="cr-help" ng-if="crSession.networkLoading" role="status">Loading authorized network details…</p>
    <p class="cr-help" ng-if="crSession.box">{{crSession.box.project_name}} · {{crSession.box.team === 0 ? 'Staging' : 'Team ' + crSession.box.team}}</p>
    <p class="cr-help" ng-if="!crSession.networkLoading && !crSession.box">No team is associated with this connection.</p>
    <div class="cr-network-row" ng-repeat="box in crSession.network track by box.resource_key">
      <div class="cr-network-host"><button type="button" class="cr-network-name" ng-click="crSession.editHost(box)">{{box.name}}</button><small ng-if="box.notes">{{box.notes}}</small></div>
      <code>{{box.ip || '—'}}</code><button type="button" class="cr-session-icon" ng-disabled="!box.ip" ng-click="crSession.copy(box.ip)" aria-label="Copy IP address for {{box.name}}">${icon('copy')}</button>
    </div>
    <button type="button" class="cr-network-add" ng-if="crSession.box && !crSession.hostDraft" ng-click="crSession.addHost()">Add host</button>
    <form class="cr-network-editor" ng-if="crSession.hostDraft" ng-submit="crSession.saveHost()">
      <h3>{{crSession.hostDraft.isNew ? 'Add host' : 'Edit host'}}</h3>
      <label>Name<input ng-model="crSession.hostDraft.name" maxlength="120" required></label>
      <label>IP address<input ng-model="crSession.hostDraft.ip" ng-readonly="!crSession.hostDraft.custom" placeholder="10.100.1.20" required></label>
      <p class="cr-help" ng-if="!crSession.hostDraft.custom">Managed host · its address updates automatically and it cannot be removed.</p>
      <label>Notes<textarea ng-model="crSession.hostDraft.notes" maxlength="2000" rows="3" placeholder="Services, findings, or reminders"></textarea></label>
      <p class="cr-error" ng-if="crSession.hostError" role="alert">{{crSession.hostError}}</p>
      <div class="cr-network-editor-actions"><button type="submit">Save host</button><button type="button" ng-click="crSession.hostDraft = null">Cancel</button><button type="button" class="cr-danger" ng-if="crSession.hostDraft.custom && !crSession.hostDraft.isNew" ng-click="crSession.deleteHost()">Delete host</button></div>
    </form>
    <p class="cr-help" ng-if="crSession.box">Names, notes and added hosts stay in this browser for your account. They do not change the lab.</p>
  </section>
  <div class="cr-session-advanced-close" ng-if="crSession.advanced">${button('close', 'Close advanced controls', 'crSession.close()')}</div>
  <p class="cr-session-feedback" ng-if="crSession.message" ng-class="{'failed': crSession.failed}" role="status">{{crSession.message}}</p>
</div>`;

    angular.module('client').directive('crangeSessionToolbar', ['$http', '$interval', '$location', 'authenticationService', 'preferenceService', 'guacFullscreen', function ($http, $interval, $location, auth, preferenceService, fullscreen) {
        return {restrict: 'E', scope: false, template: toolbar, link: function (scope, element) {
            var prefs = preferenceService.preferences, destroyed = false, boxes = [], poll = null, appliedScale = Object.create(null);
            var s = scope.crSession = {visible: false, alignment: 'left', panel: null, advanced: false, box: null, networkLoading: true, displayScale: Number(prefs.crangeDisplayScale) || 1, network: [], mouseChoices: [{value: true, label: 'Touchscreen'}, {value: false, label: 'Touchpad'}]};
            function request(payload) {
                return $http.post('api/ext/crange-control/control', payload, {headers: {'Guacamole-Token': auth.getCurrentToken(), 'X-Guacamole-Data-Source': auth.getDataSource()}}).then(function (r) { return r.data; });
            }
            function feedback(message, failed) { s.message = message; s.failed = !!failed; }
            var username = typeof auth.getCurrentUsername === 'function' ? auth.getCurrentUsername() : 'anonymous';
            var userKey = 'crange-session/' + encodeURIComponent(auth.getDataSource() + '/' + username);
            var shell = element[0].querySelector('.cr-session-shell'), handle = element[0].querySelector('.cr-session-drag'), reopen=element[0].querySelector('.cr-session-reopen'), drag = null, suppressClick=false;
            var activeToolbarKey = null, position = null, dock = {edge:'bottom',offset:0};
            function toolbarKey(client) {
                if (!client || client.id === undefined || client.id === null) return null;
                return userKey + '/connection/' + encodeURIComponent(auth.getDataSource() + '/' + String(client.id)) + '/toolbar';
            }
            function validPosition(value) { return value && Number.isFinite(value.x) && Number.isFinite(value.y); }
            function validDock(value) { return value && ['left','right','top','bottom'].indexOf(value.edge) >= 0 && Number.isFinite(value.offset); }
            function saveToolbarState() {
                if (activeToolbarKey) saveLocal(activeToolbarKey, {position:position,dock:dock,hidden:!s.visible});
            }
            function loadToolbarState(client) {
                var key = toolbarKey(client);
                if (!key || key === activeToolbarKey) return;
                if (s.close) s.close();
                activeToolbarKey = key;
                var saved = stored(key, null);
                position = saved && validPosition(saved.position) ? saved.position : null;
                dock = saved && validDock(saved.dock) ? saved.dock : {edge:'bottom',offset:0};
                s.visible = !!(saved && saved.hidden === false);
                window.requestAnimationFrame(function () {
                    if (!position) position = {x:10,y:Math.max(10,window.innerHeight-(shell.offsetHeight||44)-10)};
                    clampToolbar();
                });
            }
            var mapKey = null, mapData = {overrides: {}, custom: []};
            function stored(key, fallback) { try { return JSON.parse(window.localStorage.getItem(key)) || fallback; } catch (ignored) { return fallback; } }
            function saveLocal(key, value) { try { window.localStorage.setItem(key, JSON.stringify(value)); return true; } catch (ignored) { feedback('Browser storage is unavailable. Changes will last only for this session.', true); return false; } }
            function networkRows() {
                if (!s.box) { s.network = []; return; }
                var next = userKey + '/network/' + s.box.project + '/' + s.box.team;
                if (mapKey !== next) {
                    mapKey = next; mapData = stored(mapKey, {overrides: {}, custom: []});
                    if (!mapData || typeof mapData.overrides !== 'object' || !mapData.overrides || !Array.isArray(mapData.custom)) mapData = {overrides: {}, custom: []};
                    s.hostDraft = null;
                }
                s.network = boxes.filter(function (box) { return box.project === s.box.project && box.team === s.box.team; }).map(function (box) {
                    var custom = mapData.overrides[box.resource_key] || {};
                    return {resource_key: box.resource_key, name: custom.name || box.name, notes: custom.notes || '', ip: box.ip, custom: false};
                }).concat(mapData.custom.filter(function (row) { return row && typeof row.resource_key === 'string' && row.resource_key.indexOf('custom:') === 0; }).map(function (row) { return Object.assign({}, row, {custom: true}); }));
            }
            function validIP(value) {
                if (/^\d{1,3}(\.\d{1,3}){3}$/.test(value)) return value.split('.').every(function (part) { return Number(part) <= 255 && String(Number(part)) === part; });
                if (value.indexOf(':') === -1 || !/^[0-9a-f:]+$/i.test(value)) return false;
                try { return !!new URL('http://[' + value + ']/').hostname; } catch (ignored) { return false; }
            }
            s.addHost = function () { s.hostError = ''; s.hostDraft = {isNew: true, custom: true, name: '', ip: '', notes: ''}; };
            s.editHost = function (box) { s.hostError = ''; s.hostDraft = Object.assign({}, box); };
            s.saveHost = function () {
                var draft = s.hostDraft; if (!draft || !mapKey) return;
                var name = String(draft.name || '').trim(), notes = String(draft.notes || '').trim(), ip = String(draft.ip || '').trim();
                if (!name || name.length > 120 || notes.length > 2000) { s.hostError = 'Enter a name up to 120 characters and notes up to 2,000 characters.'; return; }
                if (draft.custom) {
                    if (!validIP(ip)) { s.hostError = 'Enter a valid IPv4 or IPv6 address.'; return; }
                    var key = draft.isNew ? 'custom:' + Date.now().toString(36) + Math.random().toString(36).slice(2, 9) : draft.resource_key;
                    var row = {resource_key: key, name: name, notes: notes, ip: ip};
                    var index = mapData.custom.findIndex(function (entry) { return entry.resource_key === key; });
                    if (index < 0) mapData.custom.push(row); else mapData.custom[index] = row;
                } else {
                    if (!s.network.some(function (row) { return row.resource_key === draft.resource_key && !row.custom; })) return;
                    mapData.overrides[draft.resource_key] = {name: name, notes: notes};
                }
                saveLocal(mapKey, mapData); s.hostDraft = null; networkRows();
            };
            s.deleteHost = function () {
                var draft = s.hostDraft; if (!draft || !draft.custom || draft.isNew) return;
                mapData.custom = mapData.custom.filter(function (row) { return row.resource_key !== draft.resource_key; });
                saveLocal(mapKey, mapData); s.hostDraft = null; networkRows();
            };
            function matchClient() {
                var client = scope.focusedClient;
                loadToolbarState(client);
                s.box = client ? boxes.find(function (box) { return (box.protocols || []).some(function (p) { return p.connection_name === client.name && (!client.protocol || p.protocol === client.protocol); }); }) || null : null;
                networkRows();
                if(s.box){try{window.sessionStorage.setItem('crange-return-project',String(s.box.project));}catch(ignored){}}
                if (client && !appliedScale[client.id]) {
                    appliedScale[client.id] = true;
                    var scale = Number(prefs.crangeDisplayScale);
                    if (scale >= 0.1 && scale <= 3) { client.clientProperties.autoFit = false; client.clientProperties.scale = scale; }
                }
            }
            function status() {
                var box = s.box;
                if (!box || destroyed) return;
                request({operation: 'status', project: box.project, resource_key: box.resource_key}).then(function (data) { if (!destroyed) box.state = data.state || 'unknown'; }, function () { box.state = 'unknown'; });
            }
            s.setScale = function () { var client = scope.focusedClient, scale = Number(s.displayScale); if (client && scale >= 0.1 && scale <= 3) { client.clientProperties.autoFit = false; client.clientProperties.scale = scale; } };
            s.close = function () { s.panel = null; s.advanced = false; scope.menu.shown = false; };
            s.toggle = function (panel) { if (s.panel === panel) s.close(); else { s.panel = panel; s.advanced = false; scope.menu.shown = true; } };
            s.show = function () { shortcutOpenedToolbar = false; s.visible = true; saveToolbarState(); window.requestAnimationFrame(function () { if(!position)position={x:10,y:Math.max(10,window.innerHeight-shell.offsetHeight-10)};clampToolbar(); handle.focus(); }); };
            s.hide = function () { shortcutOpenedToolbar = false; s.close(); document.dispatchEvent(new CustomEvent('crange-session-controls-hidden')); s.visible = false;saveToolbarState(); window.requestAnimationFrame(function () { clampToolbar(); reopen.focus(); }); };
            function returnToProject(){try{if(s.box){window.sessionStorage.removeItem('crange-return-view');window.sessionStorage.setItem('crange-return-project',String(s.box.project));window.localStorage.setItem('crange-selected-project',String(s.box.project));}else{window.sessionStorage.removeItem('crange-return-project');window.sessionStorage.setItem('crange-return-view','projects');window.localStorage.setItem('crange-selected-project','all');}}catch(ignored){}$location.url('/');}
            s.home = function () { if (prefs.crangeExitBehavior === 'disconnect') scope.disconnect(); s.close(); returnToProject(); };
            s.disconnect = function () { scope.disconnect(); s.close(); returnToProject(); };
            s.fullscreen = function () { fullscreen.toggleFullscreenMode(); s.close(); };
            s.openAdvanced = function () { s.panel = 'advanced'; s.advanced = true; scope.menu.shown = true; };
            s.copy = function (ip) {
                if (!navigator.clipboard) { feedback('Clipboard requires HTTPS. Select the address to copy it.', true); return; }
                navigator.clipboard.writeText(ip).then(function () { scope.$evalAsync(function () { feedback('IP address copied.'); }); }, function () { scope.$evalAsync(function () { feedback('Clipboard access was denied. Select the address to copy it.', true); }); });
            };
            s.power = function (action) {
                var box = s.box;
                if (!box || (s.busy && !(action === 'stop' && s.action === 'shutdown'))) return;
                if (action !== 'start' && !window.confirm((action === 'stop' ? 'Force power off immediately? Unsaved data may be lost on ' : action === 'restart' ? 'Restart and disconnect all sessions on ' : 'Shut down and disconnect all sessions on ') + box.name + '?')) return;
                var generation = s.generation = (s.generation || 0) + 1;
                s.action = action; s.busy = true; s.failed = false; s.message = action + ' · Queuing…';
                if (poll) $interval.cancel(poll);
                request({operation: 'submit', action: action, project: box.project, resource_key: box.resource_key}).then(function (data) {
                    if (generation !== s.generation) return;
                    var id = data.job_id;
                    if (!id) throw new Error('No job ID was returned.');
                    feedback(action + ' · Queued');
                    poll = $interval(function () {
                        request({operation: 'job', project: box.project, resource_key: box.resource_key, job_id: id}).then(function (result) {
                            if (generation !== s.generation) return;
                            var job = result.job || {};
                            feedback(action + ' · ' + (job.status || 'running'), job.status === 'failed');
                            if (['succeeded', 'failed', 'cancelled', 'interrupted'].indexOf(job.status) !== -1) { $interval.cancel(poll); s.busy = false; status(); if (job.status === 'succeeded') s.message = ''; else feedback(job.output || 'The power action did not complete.', true); }
                        }, function () { if (generation !== s.generation) return; $interval.cancel(poll); s.busy = false; feedback('Could not read task status. Check Jobs in CRANGE.', true); });
                    }, 1500);
                }).catch(function (error) { if (generation !== s.generation) return; s.busy = false; feedback((error.data && (error.data.message || error.data.error)) || error.message || 'Power request failed.', true); });
            };
            scope.$watch('focusedClient.clientProperties.scale', function (scale) { if (scale >= 0.1 && scale <= 3) s.displayScale = Math.round(scale * 100) / 100; });
            scope.$watch('focusedClient.id', function () { matchClient(); status(); });
            scope.$watch('focusedClient.name', function () { matchClient(); status(); });
            scope.$watch('focusedClient.protocol', function () { matchClient(); status(); });
            scope.$watch('menu.shown', function (shown) { if (shown && !s.panel) { if(!s.visible)s.show();s.panel = 'options'; } else if (!shown) { s.panel = null; s.advanced = false; } });
            var shortcutHeld = false, shortcutChangedAt = 0, shortcutOpenedToolbar = false;
            function keydown(event) {
                if (event.ctrlKey && event.altKey && event.shiftKey && ['Control','Alt','Shift'].indexOf(event.key) !== -1) {
                    // Own the session shortcut before Guacamole's native menu
                    // handler, so each complete chord toggles exactly once.
                    event.preventDefault(); event.stopImmediatePropagation();
                    if (!shortcutHeld && !event.repeat) {
                        shortcutHeld = true;
                        if (Date.now() - shortcutChangedAt >= 75) {
                            shortcutChangedAt = Date.now();
                            scope.$evalAsync(function () {
                                if (!s.visible) {
                                    s.show(); s.toggle('options'); shortcutOpenedToolbar = true;
                                } else if (shortcutOpenedToolbar && s.panel === 'options') s.hide();
                                else { shortcutOpenedToolbar = false; s.toggle('options'); }
                            });
                        }
                    }
                } else if (event.key === 'Escape' && s.panel) { event.preventDefault(); scope.$evalAsync(s.close); }
            }
            function keyup(event) { if (!event.ctrlKey || !event.altKey || !event.shiftKey) shortcutHeld = false; }
            window.addEventListener('keydown', keydown, true);
            window.addEventListener('keyup', keyup, true);
            function refreshNetwork() {
                return request({operation: 'list'}).then(function (data) { if (!destroyed) { boxes = Array.isArray(data.boxes) ? data.boxes : []; matchClient(); } }, function () { /* Retain the last authorized map during a transient read failure. */ }).finally(function () { s.networkLoading = false; });
            }
            refreshNetwork();
            var statusTimer = $interval(status, 10000), networkTimer = $interval(refreshNetwork, 30000);
            loadToolbarState(scope.focusedClient);
            function nearestEdge(x,y) {
                var edges=[['left',x],['right',window.innerWidth-x],['top',y],['bottom',window.innerHeight-y]];
                edges.sort(function(a,b){return a[1]-b[1];});
                var edge=edges[0][0], vertical=edge==='left'||edge==='right';
                return {edge:edge,offset:Math.max(0,Math.min(1,(vertical?y:x)/(vertical?window.innerHeight:window.innerWidth)))};
            }
            function clampToolbar() {
                if (!shell) return;
                ['left','right','top','bottom'].forEach(function(edge){shell.classList.toggle('dock-'+edge,!s.visible&&dock.edge===edge);});
                if (!s.visible) {
                    var width=reopen.offsetWidth||18,height=reopen.offsetHeight||56;
                    shell.style.left=Math.round(dock.edge==='left'?0:dock.edge==='right'?Math.max(0,window.innerWidth-width):Math.max(0,Math.min(window.innerWidth-width,dock.offset*window.innerWidth-width/2)))+'px';
                    shell.style.top=Math.round(dock.edge==='top'?0:dock.edge==='bottom'?Math.max(0,window.innerHeight-height):Math.max(0,Math.min(window.innerHeight-height,dock.offset*window.innerHeight-height/2)))+'px';shell.style.right='auto';
                } else if (position && Number.isFinite(position.x) && Number.isFinite(position.y)) {
                    shell.style.left = Math.round(Math.max(6, Math.min(position.x, window.innerWidth - shell.offsetWidth - 6))) + 'px';
                    shell.style.top = Math.round(Math.max(6, Math.min(position.y, window.innerHeight - shell.offsetHeight - 6))) + 'px'; shell.style.right = 'auto';
                }
                var rect = shell.getBoundingClientRect();
                shell.classList.toggle('panels-up', rect.top > window.innerHeight / 2);
                shell.classList.toggle('panels-left', rect.left > window.innerWidth / 2);
            }
            function pointerDown(event) {
                if (event.button !== 0 || drag || event.isPrimary === false) return;
                event.preventDefault(); event.stopPropagation();
                suppressClick=false;
                var rect = shell.getBoundingClientRect(),target=event.currentTarget;
                drag = {id:event.pointerId,target:target,x:event.clientX-rect.left,y:event.clientY-rect.top,startX:event.clientX,startY:event.clientY,moved:false};
                document.documentElement.classList.add('cr-session-dragging');
                document.dispatchEvent(new CustomEvent('crange-session-drag-start'));
                if (target.setPointerCapture) target.setPointerCapture(event.pointerId);
            }
            function pointerMove(event) {
                if (!drag || event.pointerId!==drag.id) return;
                event.preventDefault();event.stopPropagation();
                if(Math.hypot(event.clientX-drag.startX,event.clientY-drag.startY)<5&&!drag.moved)return;
                drag.moved=true;
                if(s.visible)position={x:event.clientX-drag.x,y:event.clientY-drag.y};
                else dock=nearestEdge(event.clientX,event.clientY);
                clampToolbar();
            }
            function finishDrag(event,cancelled) {
                if(!drag || (event && event.pointerId!==undefined && event.pointerId!==drag.id))return;
                var previous=drag;drag=null;suppressClick=previous.moved||cancelled;
                document.documentElement.classList.remove('cr-session-dragging');
                if(previous.target.hasPointerCapture&&previous.target.hasPointerCapture(previous.id))previous.target.releasePointerCapture(previous.id);
                if(s.visible){var rect=shell.getBoundingClientRect();position={x:rect.left,y:rect.top};}
                saveToolbarState();
            }
            function pointerUp(event){finishDrag(event,false);}
            function pointerCancel(event){finishDrag(event,true);}
            function blurred(){shortcutHeld=false;finishDrag(null,true);}
            function toggleControls(event){event.preventDefault();event.stopPropagation();if(suppressClick){suppressClick=false;return;}scope.$evalAsync(s.visible?s.hide:s.show);}
            function moveKeys(event) {
                if (['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Home'].indexOf(event.key) === -1) return;
                event.preventDefault(); event.stopPropagation(); var rect = shell.getBoundingClientRect(), step = event.shiftKey ? 40 : 10;
                if(s.visible)position = event.key === 'Home' ? {x: 10, y: Math.max(10,window.innerHeight-shell.offsetHeight-10)} : {x: rect.left + (event.key === 'ArrowRight' ? step : event.key === 'ArrowLeft' ? -step : 0), y: rect.top + (event.key === 'ArrowDown' ? step : event.key === 'ArrowUp' ? -step : 0)};
                else dock=event.key==='Home'?{edge:'bottom',offset:0}:nearestEdge(rect.left+(reopen.offsetWidth||18)/2+(event.key==='ArrowRight'?step:event.key==='ArrowLeft'?-step:0),rect.top+(reopen.offsetHeight||56)/2+(event.key==='ArrowDown'?step:event.key==='ArrowUp'?-step:0));
                clampToolbar(); saveToolbarState();
            }
            function bind(target,method){[['pointerdown',pointerDown],['pointermove',pointerMove],['pointerup',pointerUp],['pointercancel',pointerCancel],['lostpointercapture',pointerCancel],['keydown',moveKeys],['click',toggleControls]].forEach(function(pair){target[method](pair[0],pair[1]);});}
            bind(handle,'addEventListener');bind(reopen,'addEventListener');
            window.addEventListener('blur',blurred);window.addEventListener('resize', clampToolbar); window.requestAnimationFrame(clampToolbar);
            scope.$on('$destroy', function () {
                destroyed = true; window.removeEventListener('keydown', keydown, true); window.removeEventListener('keyup', keyup, true); window.removeEventListener('resize', clampToolbar);
                blurred();window.removeEventListener('blur',blurred);bind(handle,'removeEventListener');bind(reopen,'removeEventListener');
                $interval.cancel(statusTimer); $interval.cancel(networkTimer); if (poll) $interval.cancel(poll);
            });
        }};
    }]);
}());
