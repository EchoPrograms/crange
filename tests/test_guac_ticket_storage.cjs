'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('guacamole/extension/auth-launch.js', 'utf8');
function load(url, session = new Map(), unavailable = false) {
    const persistent = new Map([['GUAC_AUTH_TOKEN', 'native-token'], ['preferences', 'keep']]);
    const storage = {getItem: k => persistent.get(k) || null,
        setItem: (k,v) => persistent.set(k,v), removeItem: k => persistent.delete(k)};
    const window = {location: {href: url}, sessionStorage: {
        getItem(k) { if(unavailable) throw Error(); return session.get(k) || null; },
        setItem(k,v) { if(unavailable) throw Error(); session.set(k,v); },
        removeItem(k) { if(unavailable) throw Error(); session.delete(k); }
    }};
    let decorated;
    const angular = {module() { return {run() {return this;}, config(def) {
        def.at(-1)({decorator(name, factory) {
            assert.equal(name, 'localStorageService'); decorated = factory.at(-1)(storage, window);
        }}); return this;
    }}; }};
    vm.runInNewContext(source, {angular, URL, URLSearchParams});
    return {decorated, storage, persistent, session};
}
const normal = load('https://guac.example/#/');
assert.equal(normal.decorated, normal.storage);
for(const url of ['https://guac.example/?data=ticket&token=#/client/a',
        'https://guac.example/#/client/a?data=ticket&token=']) {
    const state = load(url, new Map([['crange-ticket-session-token','opener-ticket']]));
    const d = state.decorated;
    assert.equal(d.getItem('GUAC_AUTH_TOKEN'), null, 'launch cannot read/revoke opener token');
    d.setItem('GUAC_AUTH_TOKEN','console-token');
    assert.equal(d.getItem('GUAC_AUTH_TOKEN'),'console-token');
    assert.equal(state.persistent.get('GUAC_AUTH_TOKEN'),'native-token');
    assert.equal(d.getItem('preferences'),'keep');
    d.setItem('preferences','changed');
    assert.equal(state.persistent.get('preferences'),'changed');
    const reloaded = load('https://guac.example/#/client/a',state.session);
    assert.equal(reloaded.decorated.getItem('GUAC_AUTH_TOKEN'),'console-token','reload retains isolated token');
    reloaded.decorated.removeItem('GUAC_AUTH_TOKEN');
    assert.equal(reloaded.persistent.get('GUAC_AUTH_TOKEN'),'native-token','console logout leaves native session intact');
}
const memory = load('https://guac.example/?data=ticket#/',new Map(),true);
memory.decorated.setItem('GUAC_AUTH_TOKEN','memory-token');
assert.equal(memory.decorated.getItem('GUAC_AUTH_TOKEN'),'memory-token');
assert.equal(memory.persistent.get('GUAC_AUTH_TOKEN'),'native-token');
console.log('Guacamole ticket storage isolation, opener inheritance, reload and logout passed.');
