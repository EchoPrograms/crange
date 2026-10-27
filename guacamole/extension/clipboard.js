/* Connection-local text clipboards by default. Browser clipboard access is
 * opt-in, per connection. Guacamole still owns clipboard protocol streams. */
(function () {
    'use strict';
    angular.module('clipboard').config(['$provide', function ($provide) {
        $provide.decorator('clipboardService', ['$delegate', '$injector', '$q', function (service, $injector, $q) {
            var ClipboardData = $injector.get('ClipboardData'), root = $injector.get('$rootScope');
            var nativeGet = service.getClipboard, nativeSet = service.setClipboard, nativeResync = service.resyncClipboard;
            var connections = Object.create(null), active = null;
            function entry(id) { return connections[id] || (connections[id] = {passthrough: false, data: new ClipboardData({type: 'text/plain', data: ''})}); }
            function enabled(id) { return id !== null && id !== undefined && entry(id).passthrough; }
            function remember(id, data) { entry(id).data = data; root.$broadcast('crangeClipboard', id, data); }
            service.setActiveConnection = function (client, passthrough) {
                active = client ? client.id : null;
                if (client) entry(client.id).passthrough = !!passthrough;
            };
            service.getConnectionClipboard = function (client) { return client ? entry(client.id).data : new ClipboardData({type: 'text/plain', data: ''}); };
            service.setConnectionClipboard = function (client, text) {
                if (!client) return;
                var data = new ClipboardData({type: 'text/plain', data: text});
                data.crangeTarget = client.id;
                remember(client.id, data);
                root.$broadcast('guacClipboard', data);
            };
            service.allowConnectionClipboard = function (client, data) {
                if (!data || data.crangeIgnore) return false;
                if (data.crangeTarget !== undefined) return data.crangeTarget === client.id;
                return enabled(client.id) && !!client.clientProperties.focused;
            };
            service.getClipboard = function () {
                if (enabled(active)) return nativeGet.call(service);
                // Native connection initialization must never overwrite the
                // remote clipboard with an unrelated or empty local clipboard.
                var data = new ClipboardData({type: 'text/plain', data: ''}); data.crangeIgnore = true;
                return $q.when(data);
            };
            service.setClipboard = function (data) {
                if (!data) return $q.when();
                var source = data.source;
                if (source !== undefined && source !== null) {
                    remember(source, data);
                    return enabled(source) ? nativeSet.call(service, data) : $q.when();
                }
                return enabled(active) ? nativeSet.call(service, data) : $q.when();
            };
            service.resyncClipboard = function () { return enabled(active) ? nativeResync.call(service) : $q.when(); };
            return service;
        }]);
    }]);
    angular.module('client').config(['$provide', function ($provide) {
        $provide.decorator('ManagedClient', ['$delegate', 'clipboardService', function (ManagedClient, clipboard) {
            var nativeSet = ManagedClient.setClipboard;
            ManagedClient.setClipboard = function (client, data) {
                if (clipboard.allowConnectionClipboard(client, data)) return nativeSet.call(ManagedClient, client, data);
            };
            return ManagedClient;
        }]);
    }]);
}());
