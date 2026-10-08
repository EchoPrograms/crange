(function () {
    'use strict';

    /* Guacamole has no extension hook for the document favicon. Point the
       browser tab at an extension resource without replacing the login UI. */
    function setCRANGEFavicon() {
        var href = new URL('app/ext/crange-theme/assets/remote-access.svg', window.location.href).href;
        var icon = document.querySelector('link[rel~="icon"]');
        if (!icon) {
            icon = document.createElement('link');
            icon.rel = 'icon';
            document.head.appendChild(icon);
        }
        icon.type = 'image/svg+xml';
        icon.href = href;
    }

    if (document.head) setCRANGEFavicon();
    else document.addEventListener('DOMContentLoaded', setCRANGEFavicon, { once: true });

    var home = [
        '<div class="cr-home" ng-class="{loading: !isLoaded()}">',
        '  <header class="cr-home-header">',
        '    <div><div class="cr-kicker">CYBER RANGE</div><h1>Remote access</h1></div>',
        '    <div class="cr-header-actions"><select class="cr-project-select" aria-label="Project" ng-model="cr.selectedProject" ng-options="project.id as project.label for project in cr.projects"><option value="">All projects</option></select><button type="button" ng-click="cr.refresh()" ng-disabled="cr.loading">Refresh</button><guac-user-menu></guac-user-menu></div>',
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
    }]).directive('crangeBoxList', ['$http', '$interval', 'authenticationService', 'ClientIdentifier', function ($http, $interval, authenticationService, ClientIdentifier) {
        return {
            restrict: 'E',
            scope: { connectionGroups: '=' },
            template: [
                '<div class="cr-box-list">',
                '  <div class="cr-list-toolbar"><label class="cr-search-label" for="cr-box-search">Search servers</label><input id="cr-box-search" type="search" ng-model="search" placeholder="Name, IP, team, or project"></div>',
                '  <p class="cr-error" ng-if="error" role="alert">{{error}}</p>',
                '  <p class="cr-empty" ng-if="loading">Loading servers…</p>',
        '  <p class="cr-empty" ng-if="nativeInventoryLoading">Verifying native connections…</p>',
        '  <p class="cr-error" ng-if="nativeInventoryError" role="alert">{{nativeInventoryError}}</p>',
        '  <p class="cr-empty" ng-if="!loading && !nativeInventoryLoading && !nativeInventoryError && !filteredBoxes().length && !error">{{search ? "No servers match your search." : "No deployed or verified servers are assigned to this account. Apply project remote access or connect a native entry to a live guest."}}</p>',
                '  <section class="cr-project-group" ng-repeat="project in projectGroups track by project.id" ng-if="filteredBoxes(project).length">',
                '    <h2 class="cr-project-heading" ng-if="!cr.selectedProject">{{project.label}}</h2>',
                '  <section class="cr-team-group" ng-repeat="group in project.teams track by group.id" ng-if="filteredBoxes(group).length">',
                '    <button type="button" class="cr-team-heading" ng-click="toggleTeam(group)" aria-expanded="{{group.open || !!search}}"><span aria-hidden="true">{{group.open || search ? "▾" : "▸"}}</span><strong>{{group.team === null ? "Existing connections" : group.team === 0 ? "Team 0 · Staging" : "Team " + group.team}}</strong><span class="cr-team-project" ng-if="group.project">Project {{group.project}}</span><span class="cr-team-count">{{filteredBoxes(group).length}} servers</span></button>',
                '  <div class="cr-server-table" ng-show="group.open || search">',
                '    <div class="cr-server-head"><span>Server</span><span>Address</span><span>Location</span><span>Power</span><span>Connect</span><span aria-label="Management"></span></div>',
                '    <div class="cr-server-row" ng-repeat="box in filteredBoxes(group) track by box.project + \'/\' + box.resource_key">',
                '      <div class="cr-server-name" title="{{box.name}}"><span class="cr-machine-icon" aria-hidden="true">▣</span><span>{{box.name}}</span><small>{{box.kind}}</small></div>',
                '      <div class="cr-server-ip">{{box.ip || "—"}}</div>',
                '      <div class="cr-server-location">{{box.team === null ? "Guacamole" : box.team === 0 ? "Team 0 · Staging" : "Team " + box.team}}<small ng-if="box.project">Project {{box.project}}</small></div>',
                '      <div class="cr-server-power"><span class="cr-status" ng-class="{on: box.state === \'running\'}">{{box.state || "Checking…"}}</span></div>',
                '      <div class="cr-protocols"><a ng-repeat="protocol in box.protocols track by $index" ng-if="protocol.url" class="cr-protocol" ng-class="protocol.protocol" ng-href="{{protocol.url}}" title="Open {{protocol.protocol | uppercase}} to {{box.name}}">{{protocol.protocol | uppercase}}</a><span ng-if="!box.protocols.length">—</span></div>',
                '      <div class="cr-server-manage" ng-class="{open: menuBox === box}" ng-mouseleave="closeMenu()">',
                '        <button type="button" ng-if="!box.native" class="cr-more" ng-click="toggleMenu(box)" aria-label="Manage {{box.name}}" aria-expanded="{{menuBox === box}}" title="Manage this server">⋮</button>',
                '        <div class="cr-management-menu" ng-if="menuBox === box" role="group" aria-label="Server management">',
                '        <button type="button" ng-if="box.state === \'stopped\'" ng-click="submit(box, \'start\')" ng-disabled="box.busy" title="Start this server">Start</button>',
                '        <button type="button" ng-if="box.state === \'running\'" ng-click="submit(box, \'shutdown\')" ng-disabled="box.busy" title="Ask the operating system to shut down safely; active sessions disconnect">Shutdown</button>',
                '        <button type="button" ng-if="box.state === \'running\' || (box.busy && box.job.action === \'shutdown\')" ng-click="submit(box, \'stop\')" ng-disabled="box.busy && box.job.action !== \'shutdown\'" title="Force power off immediately, overriding shutdown. Unsaved data may be lost">Stop</button>',
                '        <button type="button" ng-click="submit(box, \'restart\')" ng-disabled="box.busy" title="Restart this server and disconnect active sessions">Restart</button>',
                '        <button type="button" ng-click="openSnapshots(box)" ng-disabled="box.busy" title="View and manage snapshots">Snapshots</button>',
                '        </div>',
                '      </div>',
                '      <div class="cr-row-job" ng-if="box.job && box.job.action !== \'snapshot-list\' && box.job.status !== \'succeeded\' && box.job.status !== \'interrupted\'">{{box.job.action}}: {{box.job.status}}<pre ng-if="box.job.output">{{box.job.output}}</pre></div>',
                '    </div>',
                '  </div>',
                '  </section>',
                '  </section>',
                '  <div class="cr-modal-backdrop" ng-if="snapshotBox" ng-click="closeSnapshots()">',
                '    <section class="cr-modal" role="dialog" aria-modal="true" aria-label="Manage snapshots" ng-click="$event.stopPropagation()">',
                '      <header><h2>Snapshots · {{snapshotBox.name}}</h2><button type="button" class="cr-close" ng-click="closeSnapshots()" aria-label="Close">×</button></header>',
                '      <p class="cr-help">Snapshots capture this server at a point in time. Restoring one replaces its current state.</p>',
                '      <div class="cr-snapshot-create"><label for="cr-snapshot-name">New snapshot name</label><input id="cr-snapshot-name" ng-model="snapshotName" placeholder="before-change"><button type="button" ng-click="submit(snapshotBox, \'snapshot-create\', snapshotName)" ng-disabled="snapshotBox.busy || !snapshotName">Create</button></div>',
                '      <div class="cr-snapshot-list"><p ng-if="!snapshots.length">No snapshots found.</p><div class="cr-snapshot-row" ng-repeat="snapshot in snapshots track by $index"><span>{{snapshot.name || snapshot}}</span><button type="button" ng-click="submit(snapshotBox, \'snapshot-restore\', snapshot.name || snapshot)" ng-disabled="snapshotBox.busy">Restore</button><button type="button" ng-click="submit(snapshotBox, \'snapshot-delete\', snapshot.name || snapshot)" ng-disabled="snapshotBox.busy">Delete</button></div></div>',
                '      <p class="cr-error" ng-if="snapshotError">{{snapshotError}}</p>',
                '      <div class="cr-row-job" ng-if="snapshotBox.job && snapshotBox.job.action !== \'snapshot-list\' && snapshotBox.job.status !== \'succeeded\' && snapshotBox.job.status !== \'interrupted\'">{{snapshotBox.job.action}}: {{snapshotBox.job.status}}<pre ng-if="snapshotBox.job.output">{{snapshotBox.job.output}}</pre></div>',
                '      <div class="cr-modal-actions"><button type="button" ng-click="submit(snapshotBox, \'snapshot-list\')" ng-disabled="snapshotBox.busy">Refresh snapshots</button><button type="button" ng-click="closeSnapshots()">Close</button></div>',
                '    </section>',
                '  </div>',
                '</div>'
            ].join('\n'),
            link: function (scope, element) {
                var polls = [];
                var activePolls = Object.create(null);
                var api = 'api/ext/crange-control/control';
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
                scope.closeMenu = function () { scope.menuBox = null; };
                scope.toggleMenu = function (box) { scope.menuBox = scope.menuBox === box ? null : box; };
                scope.snapshots = [];
                scope.snapshotBox = null;

                function headers() {
                    return {
                        'Guacamole-Token': authenticationService.getCurrentToken(),
                        'X-Guacamole-Data-Source': authenticationService.getDataSource()
                    };
                }
                function request(payload) {
                    return $http.post(api, payload, {headers: headers()}).then(function (response) { return response.data; });
                }
                function message(error) {
                    var data = error && error.data;
                    return (data && (data.message || data.error)) || (error && error.statusText) || 'Request failed';
                }
                var nativeInventory = null;
                var nativeInventoryRequest = null;
                var destroyed = false;
                scope.nativeInventoryLoading = true;
                scope.nativeInventoryError = '';
                function refreshNativeInventory() {
                    if (nativeInventoryRequest) return nativeInventoryRequest;
                    scope.nativeInventoryLoading = true;
                    nativeInventoryRequest = request({operation: 'native-inventory'}).then(function (data) {
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
                            managed[key] = true;
                            protocol.url = byName[key] || null;
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
                                native: true, name: title, kind: metadata.guest_type || 'Guest',
                                state: metadata.state || 'unknown', protocols: [], ip: metadata.hostname};
                            scope.boxes.push(box);
                        }
                        box.ip = metadata.hostname;
                        box.kind = metadata.guest_type || 'Guest';
                        box.state = metadata.state || 'unknown';
                        box.protocols.push({protocol: connection.protocol, connection_name: connection.name, url: item.url});
                    });
                    groupBoxes();
                    if (nativeInventory === null) refreshNativeInventory();
                }
                function refreshPower(box) {
                    if (box.native) return;
                    request({operation: 'status', project: box.project, resource_key: box.resource_key}).then(function (data) {
                        box.state = data.state || 'unknown';
                    }, function () { box.state = 'unknown'; });
                }
                scope.$watch('connectionGroups', bindLinks);
                var selectedProject = '';
                try { selectedProject = window.localStorage.getItem('crange-selected-project') || ''; } catch (ignored) {}
                scope.cr = scope.$parent.cr = {refresh: function () { scope.refresh(); }, loading: false, projects: [], selectedProject: selectedProject};
                scope.$watch('cr.selectedProject', function (value) {
                    scope.menuBox = null;
                    try { window.localStorage.setItem('crange-selected-project', value || ''); } catch (ignored) {}
                });
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
                        if (!projects[id]) projects[id] = {id: id, label: group.project ? 'Project ' + id : 'Other connections', teams: [], boxes: []};
                        projects[id].teams.push(group);
                        projects[id].boxes = projects[id].boxes.concat(group.boxes);
                    });
                    scope.projectGroups = Object.keys(projects).map(function (id) { return projects[id]; }).sort(function (a, b) { return Number(a.id) - Number(b.id); });
                    scope.cr.projects = scope.projectGroups;
                    if (scope.cr.selectedProject && !projects[scope.cr.selectedProject]) scope.cr.selectedProject = '';
                }
                scope.refresh = function () {
                    refreshNativeInventory();
                    scope.loading = true;
                    scope.$parent.cr.loading = true;
                    scope.error = '';
                    return request({operation: 'list'}).then(function (data) {
                        var previous = Object.create(null);
                        scope.boxes.forEach(function (box) { previous[box.project + '/' + box.resource_key] = box; });
                        scope.boxes = (Array.isArray(data.boxes) ? data.boxes : []).map(function (box) {
                            box.protocols = Array.isArray(box.protocols) ? box.protocols : [];
                            var existing = previous[box.project + '/' + box.resource_key];
                            return existing ? angular.extend(existing, box) : box;
                        });
                        bindLinks();
                        scope.boxes.forEach(function (box) {
                            refreshPower(box);
                            box.busy = !!(box.job && ['queued', 'running'].indexOf(box.job.status) !== -1);
                            if (box.busy) poll(box, box.job.id);
                        });
                    }, function (error) { scope.error = message(error); }).finally(function () {
                        scope.loading = false;
                        scope.$parent.cr.loading = false;
                    });
                };
                scope.filteredBoxes = function (group) {
                    var term = (scope.search || '').toLowerCase().trim();
                    return (group ? group.boxes : scope.boxes).filter(function (box) {
                        if (scope.cr.selectedProject && String(box.project) !== scope.cr.selectedProject) return false;
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
                        else scope.refresh();
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
                    scope.menuBox = null;
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
                    scope.snapshotBox = box; scope.snapshotName = ''; scope.snapshotError = ''; scope.snapshots = [];
                    scope.submit(box, 'snapshot-list');
                };
                scope.closeSnapshots = function () { scope.snapshotBox = null; };
                scope.$on('$destroy', function () { destroyed = true; polls.forEach(function (timer) { $interval.cancel(timer); }); });
                polls.push($interval(function () {
                    scope.boxes.forEach(refreshPower);
                    refreshNativeInventory();
                }, 5000));
                scope.refresh();
            }
        };
    }]);
}());
