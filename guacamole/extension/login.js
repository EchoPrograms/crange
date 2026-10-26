(function () {
    'use strict';

    // Keep Guacamole's authentication directive and dynamic credential fields.
    // Only its view is replaced; continuation and authentication errors remain native.
    var template = [
        '<div class="login-ui cr-login" ng-class="{error: loginError, continuation: isContinuation(), initial: !isContinuation()}">',
        '  <div class="cr-login-card">',
        '    <form class="login-form cr-login-form" ng-submit="login()">',
        '      <header class="cr-login-brand"><img class="cr-login-logo" src="app/ext/crange-theme/assets/remote-access.svg?v=3" alt="" width="36" height="36"><h1>CRANGE Access</h1></header>',
        '      <p class="cr-login-help" ng-show="helpText" translate="{{helpText.key}}" translate-values="{{helpText.variables}}"></p>',
        '      <div class="login-fields"><guac-form namespace="\'LOGIN\'" content="remainingFields" model="enteredValues" focused="relevantField.name" data-disabled="submitted"></guac-form></div>',
        '      <p class="cr-login-error" role="alert" ng-show="loginError" translate="{{loginError.key}}" translate-values="{{loginError.variables}}"></p>',
        '      <input type="submit" name="login" class="login cr-login-primary" ng-disabled="submitted" value="{{\'LOGIN.ACTION_LOGIN\' | translate}}">',
        '      <input type="submit" name="login" class="continue-login cr-login-primary" ng-disabled="submitted" value="{{\'LOGIN.ACTION_CONTINUE\' | translate}}">',
        '    </form>',
        '    <footer class="cr-login-footer"><a href="https://crdocs.brogano.dev/" target="_blank" rel="noopener noreferrer">Documentation</a><a href="https://crange.brogano.dev/">CRANGE Control<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true"><path d="M4 10h12m-5-5 5 5-5 5"/></svg></a></footer>',
        '  </div>',
        '</div>'
    ].join('\n');

    var key = 'app/login/templates/login.html';
    angular.module('login').config(['$provide', function ($provide) {
        $provide.decorator('$templateCache', ['$delegate', function (cache) {
            var put = cache.put;
            cache.put = function (name, value) {
                return put.call(cache, name, name === key ? template : value);
            };
            return cache;
        }]);
    }]).run(['$templateCache', function (cache) {
        cache.put(key, template);
    }]);
}());
