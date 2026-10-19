'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function session(url, parameters) {
    let login;
    let replacements = 0;
    const window = {location: {href: url}, history: {state: {existing: true}, replaceState(state, _, href) {
        assert.equal(state.existing, true);
        this.href = href;
    }}};
    const location = {search(value) {
        if (!arguments.length) return parameters;
        parameters = value;
        return this;
    }, replace() { replacements++; return this; }};
    const angular = {extend: Object.assign, module(name) {
        return {config() { return this; }, run(definition) {
            definition.at(-1)({$on(event, callback) {
                assert.equal(event, 'guacLogin'); login = callback;
            }}, location, window);
        }};
    }};
    vm.runInNewContext(fs.readFileSync('guacamole/extension/auth-launch.js', 'utf8'), {angular, URL, URLSearchParams});
    // Initial launch credentials must survive until authentication succeeds.
    assert.equal(window.history.href, undefined);
    login();
    return {parameters: JSON.parse(JSON.stringify(parameters)), replacements, href: window.history.href};
}
for (const data of ['expired-ticket', 'valid-ticket']) {
    const result = session('https://guac.example/?data='+data+'&token=&view=compact#/settings/users',
        {data, token: '', view: 'compact'});
    assert.deepEqual(result.parameters, {view: 'compact'});
    assert.equal(result.href, 'https://guac.example/?view=compact#/settings/users');
    assert.equal(result.replacements, 1);
}
const plain = session('https://guac.example/#/', {view: 'compact'});
assert.equal(plain.replacements, 0);
assert.equal(plain.href, undefined);
assert.deepEqual(plain.parameters, {view: 'compact'});
console.log('Guacamole launch credentials: success-only cleanup, route/query preservation, native login passed.');
