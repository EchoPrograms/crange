(function () {
    'use strict';

    /* Guacamole has no extension hook for the document favicon. Point the
       browser tab at an extension resource without replacing the login UI. */
    function setCRANGEFavicon() {
        var href = new URL('app/ext/crange-theme/assets/remote-access.svg?v=3', window.location.href).href;
        /* Native Guacamole declares two favicons. Replacing only the first
           lets the browser choose the remaining stock PNG instead. */
        document.querySelectorAll('link[rel~="icon"]').forEach(function (link) { link.remove(); });
        var icon = document.createElement('link');
        icon.rel = 'icon';
        icon.type = 'image/svg+xml';
        icon.setAttribute('sizes', 'any');
        icon.href = href;
        document.head.appendChild(icon);
        document.querySelectorAll('link[rel="apple-touch-icon"]').forEach(function (link) {
            link.href = new URL('app/ext/crange-theme/assets/remote-access-192.png?v=3', window.location.href).href;
            link.type = 'image/png';
            link.setAttribute('sizes', '192x192');
        });
    }

    if (document.head) setCRANGEFavicon();
    else document.addEventListener('DOMContentLoaded', setCRANGEFavicon, { once: true });

    var nextHelpId = 0;
    var home = [
        '<div class="cr-home" ng-class="{loading: !isLoaded()}">',
        '  <header class="cr-home-header">',
        '    <div class="cr-brand"><img class="cr-home-logo" src="app/ext/crange-theme/assets/remote-access.svg?v=3" alt="CRANGE"><div><h1>{{cr.page === "preferences" ? "Preferences" : cr.page === "projects" ? "Projects" : cr.projectTitle()}} <span class="cr-title-loading" ng-class="{active: cr.loading || cr.nativeLoading}" role="status" aria-label="Refreshing" aria-live="polite"><span aria-hidden="true">↻</span></span></h1></div></div>',
        '    <div class="cr-header-actions"><button type="button" ng-click="cr.navigate(\'projects\')" ng-if="cr.page !== \'projects\'">Projects</button><select ng-if="cr.page === \'servers\'" class="cr-project-select" aria-label="Project" ng-model="cr.selectedProject" ng-options="project.id as project.label for project in cr.projects"></select><button type="button" class="cr-icon-button" ng-click="cr.refresh()" ng-disabled="cr.loading" aria-label="Refresh servers" crange-help="Refresh server access and power status"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="M16.7 8a7 7 0 1 1-2.1-3.2M16.8 2.5V7h-4.5"/></svg></button><guac-user-menu></guac-user-menu><a class="cr-controller-link" href="https://crange.brogano.dev/" target="_blank" rel="noopener noreferrer">CRANGE Control<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true"><path d="M11 3h6v6m0-6-8 8M8 4H4v12h12v-4"/></svg></a></div>',
        '  </header>',
        '  <crange-box-list connection-groups="rootConnectionGroups"></crange-box-list>',
        '</div>'
    ].join('\n');

    angular.module('home').config(['$provide', function ($provide) {
        $provide.decorator('$templateCache', ['$delegate', function (cache) {
            var put = cache.put;
            cache.put = function (key, value) {
                return put.call(cache, key, key === 'app/home/templates/home.html' ? home : value);
            };
            return cache;
        }]);
    }]).run(['$templateCache', function ($templateCache) {
        $templateCache.put('app/home/templates/home.html', home);
    }]).directive('crangeHelp', [function () {
        return {
            restrict: 'A',
            link: function (scope, element, attrs) {
                var target = element[0], tooltip, timer;
                var description = target.getAttribute('aria-describedby');
                var tooltipId = 'cr-help-' + (++nextHelpId);
                function hide() {
                    window.clearTimeout(timer);
                    if (tooltip) tooltip.remove();
                    tooltip = null;
                    if (description) target.setAttribute('aria-describedby', description);
                    else target.removeAttribute('aria-describedby');
                }
                function show() {
                    hide();
                    if (!attrs.crangeHelp || document.documentElement.classList.contains('cr-session-dragging')) return;
                    timer = window.setTimeout(function () {
                        if (document.documentElement.classList.contains('cr-session-dragging')) return;
                        tooltip = document.createElement('div');
                        tooltip.className = 'cr-tooltip';
                        tooltip.id = tooltipId;
                        tooltip.setAttribute('role', 'tooltip');
                        tooltip.textContent = attrs.crangeHelp;
                        document.body.appendChild(tooltip);
                        target.setAttribute('aria-describedby', [description, tooltip.id].filter(Boolean).join(' '));
                        var rect = target.getBoundingClientRect();
                        tooltip.style.left = Math.max(8, Math.min(rect.left, window.innerWidth - tooltip.offsetWidth - 8)) + 'px';
                        tooltip.style.top = (rect.bottom + tooltip.offsetHeight + 16 < window.innerHeight ? rect.bottom + 8 : Math.max(8, rect.top - tooltip.offsetHeight - 8)) + 'px';
                    }, 350);
                }
                function keydown(event) { if (event.key === 'Escape') hide(); }
                target.addEventListener('mouseenter', show);
                target.addEventListener('mouseleave', hide);
                target.addEventListener('focus', show);
                target.addEventListener('blur', hide);
                target.addEventListener('click', hide);
                target.addEventListener('pointerdown', hide);
                target.addEventListener('keydown', keydown);
                document.addEventListener('crange-session-drag-start', hide);
                scope.$on('$destroy', function () {
                    hide();
                    target.removeEventListener('mouseenter', show);
                    target.removeEventListener('mouseleave', hide);
                    target.removeEventListener('focus', show);
                    target.removeEventListener('blur', hide);
                    target.removeEventListener('click', hide);
                    target.removeEventListener('pointerdown', hide);
                    target.removeEventListener('keydown', keydown);
                    document.removeEventListener('crange-session-drag-start', hide);
                });
            }
        };
    }]).directive('crangeBoxList', ['$http', '$interval', 'authenticationService', 'ClientIdentifier', 'preferenceService', 'connectionGroupService', 'dataSourceService', 'ConnectionGroup', function ($http, $interval, authenticationService, ClientIdentifier, preferenceService, connectionGroupService, dataSourceService, ConnectionGroup) {
        return {
            restrict: 'E',
            scope: { connectionGroups: '=' },
            template: [
                '<div class="cr-box-list" ng-class="{\'cr-layout-grid\': cr.layout === \'grid\'}">',
                "<crange-recent-connections ng-if=\"cr.page !== 'preferences'\"></crange-recent-connections>",
                "<div class=\"cr-project-cards\" ng-if=\"cr.page === 'projects'\">",
                "  <button type=\"button\" class=\"cr-project-card\" ng-repeat=\"project in projectGroups track by project.id\" ng-click=\"cr.openProject(project.id)\">",
                "    <span class=\"cr-project-card-title\">{{project.label}}</span><span class=\"cr-project-card-state\"><span class=\"cr-status\" ng-class=\"{on: runningCount(project) === project.boxes.length}\">{{runningCount(project)}} / {{project.boxes.length}} running</span></span>",
                "    <span class=\"cr-project-card-meta\">{{project.teams.length}} {{project.teams.length === 1 ? 'network' : 'networks'}} \u00b7 {{project.boxes.length}} {{project.boxes.length === 1 ? 'server' : 'servers'}}</span>",
                "  </button>",
                "</div>",
                "<guac-settings-preferences ng-if=\"cr.page === 'preferences'\"></guac-settings-preferences>",
                "<div ng-if=\"cr.page !== 'preferences'\">",
                "<div class=\"cr-list-toolbar\" ng-if=\"cr.page === 'servers'\"><label class=\"cr-search-label\" for=\"cr-box-search\">Search servers</label><input id=\"cr-box-search\" type=\"search\" ng-model=\"cr.search\" placeholder=\"Name, IP, team, or project\"><div class=\"cr-layout-switch\" role=\"group\" aria-label=\"Machine layout\"><button type=\"button\" ng-click=\"cr.setLayout('grid')\" aria-pressed=\"{{cr.layout === 'grid'}}\">Grid</button><button type=\"button\" ng-click=\"cr.setLayout('list')\" aria-pressed=\"{{cr.layout === 'list'}}\">List</button></div></div>",
                '  <p class="cr-error" ng-if="error" role="alert">{{error}}</p>',
                '  <p class="cr-error" ng-if="nativeInventoryError" role="alert">{{nativeInventoryError}}</p>',
        '  <p class="cr-empty" ng-if="!loading && !nativeInventoryLoading && !nativeInventoryError && !filteredBoxes().length && !error">{{search ? "No servers match your search." : "No deployed or verified servers are assigned to this account. Apply project remote access or connect a native entry to a live guest."}}</p>',
                '  <section class="cr-project-group" ng-repeat="project in projectGroups track by project.id" ng-if="cr.page === \'servers\' && filteredBoxes(project).length">',
        '    <h2 class="cr-project-heading" ng-if="cr.selectedProject === \'all\'">{{project.label}}</h2>',
                '  <section class="cr-team-group" ng-repeat="group in project.teams track by group.id" ng-if="filteredBoxes(group).length">',
                '    <button type="button" class="cr-team-heading" ng-click="toggleTeam(group)" aria-expanded="{{group.open || !!search}}"><span aria-hidden="true">{{group.open || search ? "▾" : "▸"}}</span><strong>{{group.team === null ? "Existing connections" : group.team === 0 ? "Team 0 · Staging" : "Team " + group.team}}</strong><span class="cr-team-project" ng-if="group.project">{{project.label}}</span><span class="cr-team-count">{{filteredBoxes(group).length}} {{filteredBoxes(group).length === 1 ? "server" : "servers"}}</span></button>',
                '  <div class="cr-server-table" ng-show="group.open || search">',
                '    <div class="cr-server-head"><span>Server</span><span>Address</span><span>Location</span><span>Power</span><span>Connect</span><span aria-label="Management"></span></div>',
                '    <div class="cr-server-row" ng-repeat="box in filteredBoxes(group) track by box.project + \'/\' + box.resource_key">',
                '      <div class="cr-server-name" crange-help="{{box.name}} · {{osLabel(box)}}"><span class="cr-os-icon cr-os-{{box.guest_os || \'unknown\'}}" role="img" aria-label="{{osLabel(box)}}"></span><span>{{box.name}}</span><small>{{box.kind}}</small></div>',
                '      <div class="cr-server-ip">{{box.ip || "—"}}</div>',
                '      <div class="cr-server-location">{{box.team === null ? "Guacamole" : box.team === 0 ? "Team 0 · Staging" : "Team " + box.team}}<small ng-if="box.project">{{box.project_name || ("p" + box.project)}}</small></div>',
                '      <div class="cr-server-power"><span class="cr-status" ng-class="{on: box.state === \'running\'}">{{box.state || "Checking…"}}</span></div>',
                '      <div class="cr-protocols"><a ng-repeat="protocol in box.protocols track by $index" ng-if="protocol.url" class="cr-protocol" ng-class="protocol.protocol" ng-href="{{protocol.url}}" crange-help="Open {{protocol.protocol | uppercase}} to {{box.name}}">{{protocol.protocol | uppercase}}</a><button type="button" class="cr-protocol console" ng-if="box.console_available" ng-click="openConsole(box)" ng-disabled="box.consoleBusy || box.state !== \'running\'" crange-help="Open the Proxmox display over VNC, including boot screens; does not require guest RDP">Console</button><span ng-if="!box.protocols.length">—</span></div>',
                '      <div class="cr-server-manage" ng-class="{open: menuBox === box}" ng-mouseleave="leaveMenu($event)" ng-mouseenter="keepMenu()">',
                '        <button type="button" ng-if="!box.native" class="cr-more" ng-click="toggleMenu(box, $event)" aria-label="Manage {{box.name}}" aria-expanded="{{menuBox === box}}" crange-help="Manage this server"><svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true" focusable="false"><circle cx="10" cy="4" r="1.5"/><circle cx="10" cy="10" r="1.5"/><circle cx="10" cy="16" r="1.5"/></svg></button>',
                '        <div class="cr-management-menu" data-resource-key="{{box.project}}/{{box.resource_key}}" popover="manual" ng-if="menuBox === box" role="group" aria-label="Server management">',
                '        <p class="cr-menu-state" ng-if="box.state !== \'running\' && box.state !== \'stopped\'">{{box.powerError ? \'Power status unavailable\' : \'Checking power state…\'}}</p>',
                '        <button type="button" ng-if="box.powerError" ng-click="retryPower(box)">Retry power status</button>',
                '        <button type="button" ng-if="box.state === \'stopped\'" ng-click="submit(box, \'start\')" ng-disabled="box.busy">Start</button>',
                '        <button type="button" ng-if="box.state === \'running\'" ng-click="submit(box, \'shutdown\')" ng-disabled="box.busy" crange-help="Ask the operating system to shut down safely; active sessions disconnect">Shutdown</button>',
                '        <button type="button" ng-if="box.state === \'running\' || (box.busy && box.job.action === \'shutdown\')" ng-click="submit(box, \'stop\')" ng-disabled="box.busy && box.job.action !== \'shutdown\'" class="cr-danger" crange-help="Force power off immediately. Unsaved data may be lost">Force stop</button>',
                '        <button type="button" ng-if="box.state === \'running\'" ng-click="submit(box, \'restart\')" ng-disabled="box.busy" crange-help="Restart this server and disconnect active sessions">Restart</button>',
                '        <button type="button" ng-click="openSnapshots(box)" ng-disabled="box.busy">Snapshots</button>',
                '        </div>',
                '      </div>',
                '      <details class="cr-row-job" ng-class="{failed: box.job.status === \'failed\', active: box.busy}" ng-if="box.job && box.job.action !== \'snapshot-list\' && box.job.status !== \'succeeded\' && box.job.status !== \'interrupted\'"><summary>{{box.job.action}} <span>{{box.job.status}}</span></summary><pre ng-if="box.job.output">{{box.job.output}}</pre><p ng-if="!box.job.output">Waiting for worker output…</p></details>',
                '    </div>',
                '  </div>',
                '  </section>',
                '  </section>',
                '</div>',
                '  <div class="cr-modal-backdrop" ng-if="snapshotBox" ng-click="closeSnapshots()">',
                '    <section class="cr-modal" role="dialog" aria-modal="true" aria-label="Manage snapshots" ng-click="$event.stopPropagation()">',
                '      <header><h2>Snapshots · {{snapshotBox.name}}</h2><button type="button" class="cr-close cr-icon-button" ng-click="closeSnapshots()" aria-label="Close snapshots" crange-help="Close snapshots"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="m5 5 10 10M15 5 5 15"/></svg></button></header>',
                '      <p class="cr-help">Snapshots capture this server at a point in time. Restoring one replaces its current state.</p>',
                '      <div class="cr-snapshot-create"><label for="cr-snapshot-name">New snapshot name</label><input id="cr-snapshot-name" ng-model="snapshotName" placeholder="before-change"><button type="button" class="cr-primary" ng-click="submit(snapshotBox, \'snapshot-create\', snapshotName)" ng-disabled="snapshotBox.busy || !snapshotName">Create</button></div>',
                '      <div class="cr-snapshot-list"><p ng-if="!snapshots.length">No snapshots found.</p><div class="cr-snapshot-row" ng-repeat="snapshot in snapshots track by $index"><span>{{snapshot.name || snapshot}}</span><button type="button" ng-click="submit(snapshotBox, \'snapshot-restore\', snapshot.name || snapshot)" ng-disabled="snapshotBox.busy">Restore</button><button type="button" class="cr-icon-button cr-danger" ng-click="submit(snapshotBox, \'snapshot-delete\', snapshot.name || snapshot)" ng-disabled="snapshotBox.busy" aria-label="Delete snapshot {{snapshot.name || snapshot}}" crange-help="Delete this snapshot"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="M3 5h14M7 5V3h6v2M5 5l1 12h8l1-12M8 8v6m4-6v6"/></svg></button></div></div>',
                '      <p class="cr-error" ng-if="snapshotError">{{snapshotError}}</p>',
                '      <div class="cr-row-job" ng-if="snapshotBox.job && snapshotBox.job.action !== \'snapshot-list\' && snapshotBox.job.status !== \'succeeded\' && snapshotBox.job.status !== \'interrupted\'">{{snapshotBox.job.action}}: {{snapshotBox.job.status}}<pre ng-if="snapshotBox.job.output">{{snapshotBox.job.output}}</pre></div>',
                '      <div class="cr-modal-actions"><button type="button" class="cr-icon-button" ng-click="submit(snapshotBox, \'snapshot-list\')" ng-disabled="snapshotBox.busy" aria-label="Refresh snapshots" crange-help="Refresh snapshots"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="M16.7 8a7 7 0 1 1-2.1-3.2M16.8 2.5V7h-4.5"/></svg></button><button type="button" ng-click="closeSnapshots()">Done</button></div>',
                '    </section>',
                '  </div>',
                '</div>'
            ].join('\n'),
            link: function (scope, element) {
                var polls = [];
                var activePolls = Object.create(null);
                var api = 'api/ext/crange-control/control';
                scope.osLabel = function (box) { return ({linux: 'Linux', windows: 'Windows', bsd: 'BSD'})[box.guest_os] || 'OS not recorded'; };
                scope.boxes = [];
                scope.teamGroups = [];
                scope.projectGroups = [];
                var collapsedTeams = {};
                try { collapsedTeams = JSON.parse(window.localStorage.getItem('crange-collapsed-teams') || '{}') || {}; } catch (ignored) {}
                scope.toggleTeam = function (group) {
                    group.open = !group.open;
                    collapsedTeams[group.id] = !group.open;
                    scope.menuBox = null;
                    try { window.localStorage.setItem('crange-collapsed-teams', JSON.stringify(collapsedTeams)); } catch (ignored) {}
                };
                scope.loading = true;
                scope.error = '';
                scope.search = '';
                scope.menuBox = null;
                var menuTrigger = null, menuPositionTimer, menuLeaveTimer, snapshotFocusTimer, snapshotTrigger;
                scope.closeMenu = function () {
                    window.clearTimeout(menuLeaveTimer);
                    window.clearTimeout(menuPositionTimer);
                    if (element[0]) element[0].querySelectorAll('.cr-management-menu').forEach(function (menu) {
                        menu.style.visibility = 'hidden';
                        if (menu.hidePopover && menu.matches(':popover-open')) menu.hidePopover();
                    });
                    scope.menuBox = null;
                };
                scope.keepMenu = function () { window.clearTimeout(menuLeaveTimer); };
                scope.leaveMenu = function (event) {
                    window.clearTimeout(menuLeaveTimer);
                    var owner = event && event.currentTarget;
                    if (owner && event.relatedTarget && owner.contains(event.relatedTarget)) return;
                    // A small grace period covers pointer movement across the
                    // floating panel boundary without keeping abandoned menus open.
                    menuLeaveTimer = window.setTimeout(function () {
                        scope.$evalAsync(function () { scope.closeMenu(); });
                    }, 160);
                };
                scope.toggleMenu = function (box, event) {
                    var wasOpen = scope.menuBox === box;
                    scope.closeMenu();
                    scope.menuBox = wasOpen ? null : box;
                    menuTrigger = event && event.currentTarget;
                    if (scope.menuBox) refreshPower(box, true);
                    positionMenu();
                };
                function positionMenu() {
                    window.clearTimeout(menuPositionTimer);
                    if (!scope.menuBox || !menuTrigger || !element[0]) return;
                    menuPositionTimer = window.setTimeout(function () {
                        var key = scope.menuBox.project + '/' + scope.menuBox.resource_key;
                        var menu = Array.from(element[0].querySelectorAll('.cr-management-menu')).find(function (item) {
                            return item.getAttribute('data-resource-key') === key && !item.classList.contains('ng-leave');
                        });
                        if (!menu) return;
                        if (menu.showPopover && !menu.matches(':popover-open')) menu.showPopover();
                        var rect = menuTrigger.getBoundingClientRect(), height = menu.offsetHeight;
                        menu.style.left = Math.max(8, Math.min(rect.right - menu.offsetWidth, window.innerWidth - menu.offsetWidth - 8)) + 'px';
                        menu.style.top = (rect.bottom + height + 8 <= window.innerHeight ? rect.bottom : Math.max(8, rect.top - height)) + 'px';
                        menu.style.visibility = 'visible';
                    }, 0);
                };
                function dismissMenus(event) {
                    if (scope.snapshotBox && event.type === 'keydown') {
                        if (event.key === 'Escape') {
                            event.preventDefault();
                            scope.$evalAsync(function () { scope.closeSnapshots(); });
                        }
                        else if (event.key === 'Tab') {
                            var modal = element[0].querySelector('.cr-modal');
                            var controls = modal && modal.querySelectorAll('button:not(:disabled), input:not(:disabled)');
                            if (controls && controls.length) {
                                var first = controls[0], last = controls[controls.length - 1];
                                if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
                                else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
                            }
                        }
                        return;
                    }
                    if (!scope.menuBox) return;
                    if (event.type === 'keydown' && event.key !== 'Escape') return;
                    if (event.type === 'click' && event.target.closest('.cr-server-manage')) return;
                    scope.$evalAsync(function () { scope.closeMenu(); });
                    if (event.type === 'keydown' && menuTrigger) menuTrigger.focus();
                }
                function repositionMenus(event) {
                    if (event && event.target.closest && event.target.closest('.cr-management-menu')) return;
                    if (scope.menuBox) scope.$evalAsync(function () { scope.closeMenu(); });
                }
                if (element[0]) {
                    document.addEventListener('click', dismissMenus);
                    document.addEventListener('keydown', dismissMenus);
                    window.addEventListener('resize', repositionMenus);
                    window.addEventListener('scroll', repositionMenus, true);
                }
                scope.openConsole = function (box) {
                    var popup = window.open('about:blank', '_blank');
                    box.consoleBusy = true;
                    request({operation: 'console', project: box.project, resource_key: box.resource_key}).then(function (data) {
                        if (!data || !data.url || !/^https:\/\//.test(data.url)) throw new Error('Console launch failed');
                        if (popup) {try{popup.sessionStorage.setItem('crange-return-project',String(box.project));}catch(ignored){}popup.location.replace(data.url);} else {try{window.sessionStorage.setItem('crange-return-project',String(box.project));}catch(ignored){}window.location.href = data.url;}
                    }).catch(function () {
                        if (popup) popup.close();
                        scope.error = 'Console could not be opened. Verify that this VM is running.';
                    }).finally(function () { box.consoleBusy = false; });
                };
                scope.snapshots = [];
                scope.snapshotBox = null;

                function headers(source) {
                    return {
                        'Guacamole-Token': authenticationService.getCurrentToken(),
                        'X-Guacamole-Data-Source': source || authenticationService.getDataSource()
                    };
                }
                function request(payload, source) {
                    return $http.post(api, payload, {headers: headers(source)}).then(function (response) { return response.data; });
                }
                function message(error) {
                    var data = error && error.data;
                    return (data && (data.message || data.error)) || (error && error.statusText) || 'Request failed';
                }
                var nativeInventory = null;
                var nativeInventoryRequest = null;
                var connectionGroupsRequest = null, listRequest = null;
                var destroyed = false;
                var powerRequests = 0, powerCursor = 0, powerPending = Object.create(null);
                scope.nativeInventoryLoading = true;
                scope.nativeInventoryError = '';
                function refreshNativeInventory() {
                    if (nativeInventoryRequest) return nativeInventoryRequest;
                    var source = authenticationService.getDataSource();
                    if (source === 'json') {
                        // Dashboard/console tickets are authorized by their JSON
                        // connection tree. Guacamole may advertise database
                        // contexts even when this dashboard identity has no
                        // database account; those are not native logins.
                        nativeInventory = Object.create(null);
                        scope.nativeInventoryLoading = false;
                        scope.nativeInventoryError = '';
                        if (scope.cr) scope.cr.nativeLoading = false;
                        return;
                    }
                    scope.nativeInventoryLoading = true;
                    if (scope.cr) scope.cr.nativeLoading = true;
                    nativeInventoryRequest = request({operation: 'native-inventory'}, source).then(function (data) {
                        if (!data || !Array.isArray(data.connections)) throw new Error('Invalid native inventory');
                        var updated = Object.create(null);
                        (data.connections || []).forEach(function (connection) {
                            if (connection.guest_matched === true && connection.hostname) {
                                updated[String(connection.data_source) + '\u0000' + String(connection.identifier)] = connection;
                            }
                        });
                        nativeInventory = updated;
                        scope.nativeInventoryError = '';
                        if (!destroyed) bindLinks();
                    }).catch(function () {
                        scope.nativeInventoryError = 'Could not verify native connections. Use Refresh to retry.';
                        scope.nativeInventoryLoading = false;
                        if (nativeInventory) {
                            Object.keys(nativeInventory).forEach(function (identifier) {
                                nativeInventory[identifier].state = 'unknown';
                            });
                            if (!destroyed) bindLinks();
                        }
                    }).finally(function () {
                        scope.nativeInventoryLoading = false;
                        if (scope.cr) scope.cr.nativeLoading = false;
                        nativeInventoryRequest = null;
                    });
                    return nativeInventoryRequest;
                }
                function visit(group, source, byName, catalog) {
                    if (!group) return;
                    (group.childConnections || []).forEach(function (connection) {
                        var key = source + '\u0000' + connection.name + '\u0000' + connection.protocol;
                        var id = ClientIdentifier.toString({dataSource: source, type: ClientIdentifier.Types.CONNECTION, id: connection.identifier});
                        if (!byName[key]) byName[key] = '#/client/' + encodeURIComponent(id);
                        catalog.push({source: source, connection: connection,
                            url: window.CRangeNativeInventory.connectionUrl(id)});
                    });
                    (group.childConnectionGroups || []).forEach(function (child) { visit(child, source, byName, catalog); });
                }
                function bindLinks() {
                    var byName = Object.create(null);
                    var catalog = [], managed = Object.create(null), native = Object.create(null);
                    angular.forEach(scope.connectionGroups, function (group, source) { visit(group, source, byName, catalog); });
                    scope.boxes = scope.boxes.filter(function (box) { return !box.native; });
                    var currentSource = authenticationService.getDataSource();
                    scope.boxes.forEach(function (box) {
                        (box.protocols || []).forEach(function (protocol) {
                            var key = currentSource + '\u0000' + protocol.connection_name + '\u0000' + protocol.protocol;
                            protocol.url = byName[key] || null;
                            // A JSON console can coexist with database contexts.
                            // Match the authenticated tree, not just the context
                            // which issued the most recent login token.
                            catalog.forEach(function (item) {
                                if (item.connection.name !== protocol.connection_name || item.connection.protocol !== protocol.protocol) return;
                                managed[item.source + '\u0000' + item.connection.name + '\u0000' + item.connection.protocol] = true;
                                if (!protocol.url) protocol.url = item.url;
                                var live = nativeInventory && nativeInventory[item.source + '\u0000' + item.connection.identifier];
                                if (live && live.guest_matched && live.guest_os) box.guest_os = live.guest_os;
                            });
                        });
                    });
                    // Guacamole's authenticated connection tree is authoritative
                    // for native access. Never invent CRANGE ownership for it.
                    window.CRangeNativeInventory.verifiedEntries(catalog, nativeInventory).forEach(function (item) {
                        var connection = item.connection;
                        if (managed[item.source + '\u0000' + connection.name + '\u0000' + connection.protocol]) return;
                        var metadata = item.metadata;
                        var title = connection.name.replace(/[- ·]+(ssh|rdp|vnc)$/i, '');
                        var key = item.source + '/' + title;
                        var box = native[key];
                        if (!box) {
                            box = native[key] = {project: null, team: null, resource_key: 'native:' + key,
                                native: true, name: title, kind: metadata.guest_type || 'Guest', guest_os: metadata.guest_os || '',
                                state: metadata.state || 'unknown', protocols: [], ip: metadata.hostname};
                            scope.boxes.push(box);
                        }
                        box.ip = metadata.hostname;
                        box.kind = metadata.guest_type || 'Guest';
                        box.guest_os = metadata.guest_os || '';
                        box.state = metadata.state || 'unknown';
                        box.protocols.push({protocol: connection.protocol, connection_name: connection.name, url: item.url});
                    });
                    groupBoxes();
                    if (nativeInventory === null) refreshNativeInventory();
                }
                function refreshConnectionGroups() {
                    if (connectionGroupsRequest) return connectionGroupsRequest;
                    // Reload the current identity's authorized tree. CRANGE's
                    // server list can change after this Guacamole page opens.
                    connectionGroupsRequest = dataSourceService.apply(
                        connectionGroupService.getConnectionGroupTree,
                        authenticationService.getAvailableDataSources(),
                        ConnectionGroup.ROOT_IDENTIFIER
                    ).then(function (groups) {
                        if (!destroyed) {
                            scope.connectionGroups = groups;
                            bindLinks();
                        }
                    }, function (error) {
                        if (!destroyed) scope.error = message(error);
                    }).finally(function () { connectionGroupsRequest = null; });
                    return connectionGroupsRequest;
                }
                scope.retryPower = function (box) { refreshPower(box, true); };
                function refreshPower(box, priority) {
                    var key = box.project + '/' + box.resource_key;
                    if (box.native || (!priority && powerRequests >= 3) || powerPending[key] || destroyed) return;
                    box.powerError = false;
                    powerPending[key] = true; powerRequests++;
                    return $http.post(api, {operation: 'status', project: box.project, resource_key: box.resource_key}, {headers: headers(), timeout: 12000}).then(function (response) {
                        if (!destroyed) { box.state = response.data.state || 'unknown'; box.powerError = !!response.data.power_error; }
                    }, function () { if (!destroyed) { box.state = 'unknown'; box.powerError = true; } }).finally(function () {
                        delete powerPending[key]; powerRequests--;
                        if (scope.menuBox === box) positionMenu();
                    });
                }
                var powerBatchPending = false;
                function refreshPowerBatch() {
                    if (powerBatchPending || destroyed) return;
                    powerBatchPending = true;
                    return request({operation:'power-status'}).then(function (data) {
                        var states = Object.create(null);
                        (data.boxes || []).forEach(function (row) { states[row.project+'/'+row.resource_key] = row; });
                        scope.boxes.forEach(function (box) {
                            if (box.native) return;
                            var status = states[box.project+'/'+box.resource_key];
                            box.state = status ? status.state : 'unknown';
                            box.powerError = !status || !!status.power_error;
                        });
                        if (scope.menuBox) positionMenu();
                    }, function () {
                        scope.boxes.forEach(function (box) { if (!box.native) { box.state = 'unknown'; box.powerError = true; } });
                    }).finally(function () { powerBatchPending = false; });
                }
                scope.$watch('connectionGroups', bindLinks);
                scope.listLoaded = false;
                scope.projectOptionsReady = false;
                var selectedProject = '';
                try { selectedProject = window.localStorage.getItem('crange-selected-project') || 'all'; } catch (ignored) { selectedProject = 'all'; }
                var returningProject=null, returningView=null;
                try{returningProject=window.sessionStorage.getItem('crange-return-project');window.sessionStorage.removeItem('crange-return-project');returningView=window.sessionStorage.getItem('crange-return-view');window.sessionStorage.removeItem('crange-return-view');}catch(ignored){}
                if(returningProject && /^(?:[1-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-5])$/.test(returningProject))selectedProject=returningProject;else returningProject=null;
                var initialProjects = [{id: 'all', label: 'All projects'}];
                if (/^(?:[1-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-5])$/.test(selectedProject))
                    initialProjects.push({id: selectedProject, label: 'Project ' + selectedProject});
                else if (selectedProject === 'other')
                    initialProjects.push({id: 'other', label: 'Other connections'});
                else selectedProject = 'all';
                scope.cr = scope.$parent.cr = {page: returningProject || returningView==='servers' ? 'servers' : 'projects', layout: preferenceService && preferenceService.preferences.crangeMachineLayout === 'list' ? 'list' : 'grid', refresh: function () { scope.refresh(); }, loading: false, nativeLoading: true, projects: initialProjects, selectedProject: selectedProject};
                scope.$watch('cr.search', function (value) { scope.search = value || ''; });
                function rememberHome(){if(scope.cr.page==='preferences')return;try{window.sessionStorage.setItem('crange-home-context',JSON.stringify({page:scope.cr.page,project:scope.cr.selectedProject}));}catch(ignored){}}
                scope.cr.navigate = function (page) { scope.closeMenu(); if(page==='preferences')scope.cr.previousPage=scope.cr.page;scope.cr.page = page;rememberHome(); };
                scope.cr.back = function () { scope.cr.navigate(scope.cr.previousPage || (scope.cr.selectedProject==='all'?'projects':'servers')); };
                scope.cr.openProject = function (id) { scope.cr.selectedProject = String(id); scope.cr.navigate('servers'); };
                scope.cr.projectTitle = function () { var selected = scope.cr.projects.find(function (p) { return p.id === scope.cr.selectedProject; }); return selected ? selected.label : 'Remote access'; };
                scope.cr.setLayout = function (layout) { scope.cr.layout = layout; if (preferenceService) { preferenceService.preferences.crangeMachineLayout = layout; preferenceService.save(); } };
                scope.runningCount = function (project) { return project.boxes.filter(function (box) { return box.state === 'running'; }).length; };
                scope.$watch('cr.selectedProject', function (value) {
                    scope.menuBox = null;
                    try { window.localStorage.setItem('crange-selected-project', value || 'all'); } catch (ignored) {}
                    rememberHome();
                });
                rememberHome();
                function groupBoxes() {
                    var groups = Object.create(null), projects = Object.create(null);
                    scope.boxes.forEach(function (box) {
                        var id = box.project + '/' + box.team;
                        if (!groups[id]) groups[id] = {id: id, project: box.project, team: box.team, open: !collapsedTeams[id], boxes: []};
                        groups[id].boxes.push(box);
                    });
                    scope.teamGroups = Object.keys(groups).map(function (id) { return groups[id]; }).sort(function (a, b) { return a.project - b.project || a.team - b.team; });
                    scope.teamGroups.forEach(function (group) {
                        var id = String(group.project);
                        var projectId = group.project === null ? 'other' : id;
                        if (!projects[projectId]) projects[projectId] = {id: projectId, label: group.project === null ? 'Other connections' : (group.boxes[0].project_name || 'Project ' + id), teams: [], boxes: []};
                        projects[projectId].teams.push(group);
                        projects[projectId].boxes = projects[projectId].boxes.concat(group.boxes);
                    });
                    scope.projectGroups = Object.keys(projects).map(function (id) { return projects[id]; }).sort(function (a, b) { return Number(a.id) - Number(b.id); });
                    var projectOptions = [{id: 'all', label: 'All projects'}].concat(scope.projectGroups.map(function (project) {
                        return {id: String(project.id), label: project.label};
                    }));
                    if (!scope.listLoaded && scope.cr.selectedProject !== 'all' && !projects[scope.cr.selectedProject]) {
                        var savedId = scope.cr.selectedProject;
                        projectOptions.push({id: savedId, label: savedId === 'other' ? 'Other connections' : 'Project ' + savedId});
                    }
                    scope.cr.projects = projectOptions;
                    if (scope.listLoaded && !scope.projectOptionsReady) {
                        if (scope.cr.selectedProject !== 'all' && !projects[scope.cr.selectedProject]) scope.cr.selectedProject = 'all';
                        scope.projectOptionsReady = true;
                    }
                    else if (scope.projectOptionsReady && scope.cr.selectedProject !== 'all' && !projects[scope.cr.selectedProject]) scope.cr.selectedProject = 'all';
                }
                scope.refresh = function () {
                    if (listRequest) return listRequest;
                    refreshNativeInventory();
                    scope.loading = true;
                    scope.$parent.cr.loading = true;
                    scope.error = '';
                    var groups = refreshConnectionGroups();
                    listRequest = request({operation: 'list'}).then(function (data) {
                        var previous = Object.create(null);
                        scope.boxes.forEach(function (box) { previous[box.project + '/' + box.resource_key] = box; });
                        scope.boxes = (Array.isArray(data.boxes) ? data.boxes : []).map(function (box) {
                            box.protocols = Array.isArray(box.protocols) ? box.protocols : [];
                            var existing = previous[box.project + '/' + box.resource_key];
                            return existing ? angular.extend(existing, box) : box;
                        });
                        scope.listLoaded = true;
                        bindLinks();
                        scope.boxes.forEach(function (box) {
                            box.powerError = !!box.power_error;
                            box.busy = !!(box.job && ['queued', 'running'].indexOf(box.job.status) !== -1);
                            if (box.busy) poll(box, box.job.id);
                        });
                    }, function (error) { scope.error = message(error); }).then(function () {
                        return groups;
                    }).finally(function () {
                        scope.loading = false;
                        scope.$parent.cr.loading = false;
                        listRequest = null;
                    });
                    return listRequest;
                };
                scope.filteredBoxes = function (group) {
                    var term = (scope.search || '').toLowerCase().trim();
                    return (group ? group.boxes : scope.boxes).filter(function (box) {
                        if (scope.cr.selectedProject && scope.cr.selectedProject !== 'all' && (scope.cr.selectedProject === 'other' ? box.project !== null : String(box.project) !== scope.cr.selectedProject)) return false;
                        if (!term) return true;
                        return [box.name, box.ip, box.team, box.project, box.kind].join(' ').toLowerCase().indexOf(term) !== -1;
                    });
                };
                function finish(box, job) {
                    box.busy = false;
                    if (box.job.action === 'snapshot-list' && job.status !== 'succeeded') scope.snapshotError = job.output || 'Could not load snapshots.';
                    if (scope.snapshotBox === box && job.status === 'succeeded' && box.job.action === 'snapshot-list') {
                        var output = job.result || job.snapshots || job.output;
                        try { scope.snapshots = typeof output === 'string' ? JSON.parse(output) : (output || []); }
                        catch (ignored) { scope.snapshots = []; scope.snapshotError = output || 'Could not read snapshots'; }
                        if (!Array.isArray(scope.snapshots)) scope.snapshots = scope.snapshots.snapshots || [];
                    }
                    if (job.status === 'succeeded' && box.job.action !== 'snapshot-list') {
                        if (scope.snapshotBox === box && box.job.action.indexOf('snapshot-') === 0) {
                            scope.submit(box, 'snapshot-list');
                        }
                        else refreshPower(box);
                    }
                }
                function poll(box, jobId) {
                    if (activePolls[jobId]) return;
                    activePolls[jobId] = true;
                    var timer = $interval(function () {
                        request({operation: 'job', project: box.project, resource_key: box.resource_key, job_id: jobId}).then(function (data) {
                            if (!box.job || box.job.id !== jobId) { $interval.cancel(timer); delete activePolls[jobId]; return; }
                            var job = data.job || {};
                            box.job.status = job.status || 'running';
                            box.job.output = job.output || '';
                                if (['succeeded', 'failed', 'cancelled', 'interrupted'].indexOf(job.status) !== -1) {
                                $interval.cancel(timer); delete activePolls[jobId];
                                finish(box, job);
                            }
                        }, function (error) {
                            if (!box.job || box.job.id !== jobId) { $interval.cancel(timer); delete activePolls[jobId]; return; }
                            $interval.cancel(timer); delete activePolls[jobId]; box.busy = false; box.job.status = 'failed'; box.job.output = message(error);
                            if (box.job.action === 'snapshot-list') scope.snapshotError = box.job.output;
                        });
                    }, 2000);
                    polls.push(timer);
                }
                scope.submit = function (box, action, snapshot) {
                    if (box.busy && !(action === 'stop' && box.job && box.job.action === 'shutdown')) return;
                    scope.closeMenu();
                    if (['shutdown', 'stop', 'restart', 'snapshot-restore', 'snapshot-delete'].indexOf(action) !== -1 &&
                        !window.confirm((action === 'stop' ? 'Force power off (unsaved data may be lost): ' : action.replace('snapshot-', '') + ' ') + box.name + (snapshot ? ' / ' + snapshot : '') + '?')) return;
                    box.busy = true;
                    var submittedJob = {action: action, status: 'submitting', output: ''};
                    box.job = submittedJob;
                    var payload = {operation: 'submit', project: box.project, resource_key: box.resource_key, action: action};
                    if (snapshot) payload.snapshot = snapshot;
                    request(payload).then(function (data) {
                        if (box.job !== submittedJob) return;
                        if (!data.job_id) throw new Error('No job ID returned');
                        box.job.id = data.job_id; box.job.status = 'queued'; poll(box, data.job_id);
                    }).catch(function (error) {
                        if (box.job !== submittedJob) return;
                        box.busy = false; box.job.status = 'failed'; box.job.output = error.message || message(error);
                        if (action === 'snapshot-list') scope.snapshotError = box.job.output;
                    });
                };
                scope.openSnapshots = function (box) {
                    scope.closeMenu();
                    snapshotTrigger = menuTrigger;
                    scope.snapshotBox = box; scope.snapshotName = ''; scope.snapshotError = ''; scope.snapshots = [];
                    scope.submit(box, 'snapshot-list');
                    snapshotFocusTimer = window.setTimeout(function () {
                        var input = element[0] && element[0].querySelector('#cr-snapshot-name');
                        if (input) input.focus();
                    }, 0);
                };
                scope.closeSnapshots = function () {
                    scope.snapshotBox = null;
                    window.clearTimeout(snapshotFocusTimer);
                    if (snapshotTrigger && document.documentElement.contains(snapshotTrigger)) snapshotTrigger.focus();
                };
                scope.$on('$destroy', function () {
                    destroyed = true;
                    polls.forEach(function (timer) { $interval.cancel(timer); });
                    window.clearTimeout(menuPositionTimer);
                    window.clearTimeout(snapshotFocusTimer);
                    window.clearTimeout(menuLeaveTimer);
                    if (element[0]) {
                        document.removeEventListener('click', dismissMenus);
                        document.removeEventListener('keydown', dismissMenus);
                        window.removeEventListener('resize', repositionMenus);
                        window.removeEventListener('scroll', repositionMenus, true);
                    }
                });
                polls.push($interval(function () {
                    refreshPowerBatch();
                    refreshNativeInventory();
                }, 5000));
                polls.push($interval(function () {
                    if (!document.hidden && scope.cr.page !== 'preferences') scope.refresh();
                }, 15000));
                scope.refresh();
            }
        };
    }]);
}());
