/* Server-held sessions are scoped to the authenticated Guacamole identity.
 * Only live, currently-authorized sessions are shown; no browser history is
 * used as evidence that a session can be resumed. */
(function () {
    'use strict';
    angular.module('home').directive('crangeRecentConnections', ['$http', '$interval', 'authenticationService', 'ClientIdentifier', '$injector', function ($http, $interval, auth, ClientIdentifier, $injector) {
        return {
            restrict: 'E', scope: {},
            template: '<section class="cr-recent-connections" ng-if="sessions.length" aria-label="Recent connections">' +
                '<h2>Recent connections</h2><p class="cr-error" ng-if="error" role="alert">{{error}} <button type="button" ng-click="refresh()">Retry</button></p>' +
                '<div class="cr-recent-scroll"><table><thead><tr><th scope="col">Connection</th><th scope="col">Protocol</th><th scope="col">Started</th><th scope="col">Session</th><th scope="col" aria-label="Actions"></th></tr></thead>' +
                '<tbody><tr ng-repeat="session in sessions track by session.source + \'/\' + session.id"><td>{{session.name}}</td><td>{{session.protocol | uppercase}}</td><td>{{session.started | date:\'short\'}}</td><td><span class="cr-status on">{{session.attached ? \'Connected\' : \'Ready to resume\'}}</span></td>' +
                '<td class="cr-recent-actions"><a class="cr-protocol" ng-href="{{session.url}}" ng-click="resume(session)" aria-label="Resume {{session.name}}">Resume</a><button type="button" ng-click="end(session)" ng-disabled="session.ending" aria-label="End session for {{session.name}}">{{session.ending ? \'Ending…\' : \'End\'}}</button></td></tr></tbody></table></div></section>',
            link: function (scope) {
                var stopped = false, pending = false;
                scope.sessions = []; scope.error = '';
                function request(method, source, id) {
                    return $http({method: method, url: 'api/ext/crange-resume/sessions' + (id ? '/' + encodeURIComponent(id) : ''),
                        headers: {'Guacamole-Token': auth.getCurrentToken(), 'X-Guacamole-Data-Source': source}});
                }
                scope.refresh = function () {
                    if (pending || stopped) return;
                    pending = true;
                    var sources = auth.getAvailableDataSources().filter(function (source) { return !/-shared$/.test(source); });
                    var loads = sources.map(function (source) {
                        return request('GET', source).then(function (response) { return response.data.sessions || []; }, function (error) { if ([401, 403, 404].indexOf(error.status) !== -1) return []; throw error; });
                    });
                    // Keep prior rows on a temporary failure instead of claiming
                    // that still-running remote sessions have disappeared.
                    Promise.all(loads).then(function (groups) {
                        if (stopped) return;
                        scope.$evalAsync(function () {
                            scope.sessions = [].concat.apply([], groups).map(function (session) {
                                var id = ClientIdentifier.toString({dataSource: session.source, type: ClientIdentifier.Types.CONNECTION, id: session.connection});
                                session.clientId = id; session.url = '#/client/' + encodeURIComponent(id); return session;
                            }).sort(function (a, b) { return b.last_used - a.last_used; });
                            scope.error = '';
                        });
                    }, function () {
                        if (!stopped) scope.$evalAsync(function () { scope.error = 'Could not refresh your active sessions.'; });
                    }).finally(function () { pending = false; });
                };
                function forgetClient(session, force) {
                    if (!$injector.has('guacClientManager')) return;
                    var manager = $injector.get('guacClientManager'), client = manager.getManagedClients()[session.clientId];
                    var states = $injector.get('ManagedClientState').ConnectionState;
                    if (client && (force || [states.IDLE, states.CONNECTING, states.WAITING, states.CONNECTED].indexOf(client.clientState.connectionState) === -1))
                        manager.removeManagedClient(session.clientId);
                }
                scope.resume = function (session) { forgetClient(session, false); };
                scope.end = function (session) {
                    if (session.ending) return;
                    session.ending = true;
                    request('DELETE', session.source, session.id).then(function () {
                        forgetClient(session, true);
                        scope.sessions = scope.sessions.filter(function (item) { return item.id !== session.id || item.source !== session.source; });
                    }, function (error) {
                        if (error.status === 404) scope.sessions = scope.sessions.filter(function (item) { return item !== session; });
                        else scope.error = 'Could not end that session. Try again.';
                    }).finally(function () { session.ending = false; });
                };
                var timer = $interval(function () { if (!document.hidden) scope.refresh(); }, 10000);
                scope.$on('$destroy', function () { stopped = true; $interval.cancel(timer); });
                scope.refresh();
            }
        };
    }]);
}());
