/* Prevent connection navigation from prompting for browser clipboard access.
 * Explicit paste gestures and clipboard permission already granted by the
 * browser continue to use Guacamole's native clipboard service. */
(function () {
    'use strict';

    function closest(element, selector) {
        return element && typeof element.closest === 'function' ? element.closest(selector) : null;
    }

    angular.module('clipboard').config(['$provide', function ($provide) {
        $provide.decorator('clipboardService', ['$delegate', '$injector', '$window', '$document', '$q',
            function ($delegate, $injector, $window, $document, $q) {
                var ClipboardData = $injector.get('ClipboardData');
                var nativeGet = $delegate.getClipboard;
                var nativeSet = $delegate.setClipboard;
                var nativeResync = $delegate.resyncClipboard;
                var cached = new ClipboardData({type: 'text/plain', data: ''});
                var document = $document[0];

                function remember(data) {
                    if (data) cached = data;
                    return cached;
                }

                function permissionGranted() {
                    var permissions = $window.navigator && $window.navigator.permissions;
                    var clipboard = $window.navigator && $window.navigator.clipboard;
                    if (!permissions || typeof permissions.query !== 'function' ||
                            !clipboard || typeof clipboard.readText !== 'function')
                        return $q.when(false);
                    try {
                        return $q.when(permissions.query({name: 'clipboard-read'})).then(function (permission) {
                            return !!permission && permission.state === 'granted';
                        }, function () { return false; });
                    }
                    catch (ignore) { return $q.when(false); }
                }

                function getCachedClipboard() {
                    return $q.when(remember());
                }

                // Only a previously-granted browser permission permits automatic
                // reads; prompt/denied/unsupported states are all fail-closed.
                $delegate.getClipboard = function getClipboard() {
                    return permissionGranted().then(function (granted) {
                        return granted ? nativeGet.call($delegate).then(remember) : getCachedClipboard();
                    });
                };

                $delegate.setClipboard = function setClipboard(data) {
                    remember(data);
                    return nativeSet.call($delegate, data);
                };

                $delegate.resyncClipboard = function resyncClipboard() {
                    return permissionGranted().then(function (granted) {
                        if (granted) return nativeResync.call($delegate);
                    });
                };

                function publishExplicitPaste(data) {
                    if (!data) return;
                    var clipboardData = new ClipboardData({type: 'text/plain', data: data});
                    remember(clipboardData);
                    // Native setClipboard broadcasts to active remote clients and
                    // retains Guacamole's normal remote-copy behavior.
                    nativeSet.call($delegate, clipboardData);
                }

                function onPaste(event) {
                    if (!event.isTrusted || !closest(event.target,
                            '.clipboard, .clipboard-service-target, .client .display')) return;
                    var clipboard = event.clipboardData;
                    if (!clipboard || typeof clipboard.getData !== 'function') return;
                    publishExplicitPaste(clipboard.getData('text/plain'));
                }

                document.addEventListener('paste', onPaste, true);
                return $delegate;
            }]);
    }]);
}());
