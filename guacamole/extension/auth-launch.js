(function () {
    'use strict';

    // JSON launch tickets (including hypervisor consoles) are separate sessions.
    // Guacamole otherwise reads the native token from shared localStorage,
    // revokes it on JSON login, and replaces it for every other open tab.
    angular.module('auth').config(['$provide', function ($provide) {
        $provide.decorator('localStorageService', ['$delegate', '$window', function (storage, window) {
            var isolated = false, token = null;
            var marker = 'crange-ticket-session';
            var key = 'GUAC_AUTH_TOKEN';
            var url = new URL(window.location.href);
            var hashQuery = url.hash.indexOf('?');
            var parameters = new URLSearchParams(hashQuery < 0 ? '' : url.hash.slice(hashQuery + 1));
            var launch = url.searchParams.has('data') || parameters.has('data');
            try {
                if (launch) {
                    window.sessionStorage.setItem(marker, '1');
                    // New windows inherit an opener's sessionStorage snapshot.
                    // Never revoke that opener's temporary session either.
                    window.sessionStorage.removeItem(marker + '-token');
                }
                isolated = launch || window.sessionStorage.getItem(marker) === '1';
            }
            catch (ignored) { isolated = launch; }
            if (!isolated) return storage;
            var scoped = Object.create(storage);
            scoped.getItem = function (name) {
                if (name !== key) return storage.getItem.apply(storage, arguments);
                try { return window.sessionStorage.getItem(marker + '-token'); }
                catch (ignored) { return token; }
            };
            scoped.setItem = function (name, value) {
                if (name !== key) return storage.setItem.apply(storage, arguments);
                token = value;
                try { window.sessionStorage.setItem(marker + '-token', value); } catch (ignored) {}
            };
            scoped.removeItem = function (name) {
                if (name !== key) return storage.removeItem.apply(storage, arguments);
                token = null;
                try { window.sessionStorage.removeItem(marker + '-token'); } catch (ignored) {}
            };
            return scoped;
        }]);
    }]);

    // Launch credentials select the initial session only. Keeping token="" in
    // the URL overrides the newly issued token on the next route authentication
    // and can eventually trigger Guacamole's brute-force protection.
    angular.module('index').run(['$rootScope', '$location', '$window',
        function ($rootScope, $location, $window) {
            $rootScope.$on('guacLogin', function () {
                var parameters = $location.search();
                if (Object.prototype.hasOwnProperty.call(parameters, 'token') ||
                        Object.prototype.hasOwnProperty.call(parameters, 'data')) {
                    var remaining = angular.extend({}, parameters);
                    delete remaining.token;
                    delete remaining.data;
                    $location.search(remaining).replace();
                }

                // Also remove pre-hash launch parameters from the address bar.
                // Leave the route and unrelated query parameters intact.
                var url = new URL($window.location.href);
                if (url.searchParams.has('token') || url.searchParams.has('data')) {
                    url.searchParams.delete('token');
                    url.searchParams.delete('data');
                    $window.history.replaceState($window.history.state, '', url.href);
                }
            });
        }]);
}());
