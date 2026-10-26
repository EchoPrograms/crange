'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

let config;
const moduleStub = {config(fn) { config = fn; return this; }};
vm.runInNewContext(fs.readFileSync('guacamole/extension/clipboard.js', 'utf8'), {
    angular: {module() { return moduleStub; }, noop() {} }
});

function createHarness(permissionState = 'prompt', permissionsSupported = true, queryFailure = '') {
    const listeners = {};
    const calls = {get: 0, set: [], resync: 0, queries: 0};
    const clipboard = {type: 'text/plain', data: 'cached'};
    const delegate = {
        getClipboard() { calls.get++; return Promise.resolve(clipboard); },
        setClipboard(data) { calls.set.push(data); return Promise.resolve(); },
        resyncClipboard() { calls.resync++; return Promise.resolve(); }
    };
    const navigator = {clipboard: {readText() { return Promise.resolve('native'); }}};
    if (permissionsSupported) navigator.permissions = {query() {
        calls.queries++;
        if (queryFailure === 'throw') throw new Error('Unsupported permission');
        if (queryFailure === 'reject') return Promise.reject(new Error('Unsupported permission'));
        return Promise.resolve({state: permissionState});
    }};
    const document = {addEventListener(name, listener) { listeners[name] = listener; }};
    const provide = {decorator(_name, definition) { this.definition = definition; }};
    config.at(-1)(provide);
    const factory = provide.definition.at(-1);
    const service = factory(delegate, {get() { return function ClipboardData(data) { Object.assign(this, data); }; }},
        {navigator}, [document], {when: Promise.resolve.bind(Promise)});
    return {service, calls, listeners, clipboard, navigator};
}

async function run() {
    for (const state of ['prompt', 'denied']) {
        const h = createHarness(state);
        const data = await h.service.getClipboard();
        assert.equal(data.data, '', 'empty internal clipboard is safe before a copy or paste');
        await h.service.resyncClipboard();
        assert.equal(h.calls.get, 0, `${state} must not read clipboard automatically`);
        assert.equal(h.calls.resync, 0, `${state} must not resync automatically`);
    }

    const unsupported = createHarness('granted', false);
    assert.equal((await unsupported.service.getClipboard()).data, '');
    await unsupported.service.resyncClipboard();
    assert.equal(unsupported.calls.get, 0, 'unsupported Permissions API must fail closed');
    assert.equal(unsupported.calls.resync, 0);

    for (const failure of ['throw', 'reject']) {
        const h = createHarness('granted', true, failure);
        assert.equal((await h.service.getClipboard()).data, '');
        await h.service.resyncClipboard();
        assert.equal(h.calls.get + h.calls.resync, 0, 'permission query errors must never request clipboard access');
    }
    const unavailable = createHarness('granted');
    delete unavailable.navigator.clipboard.readText;
    await unavailable.service.getClipboard();
    await unavailable.service.resyncClipboard();
    assert.equal(unavailable.calls.get + unavailable.calls.resync, 0, 'missing asynchronous clipboard support must not fall back to a prompting read');

    const granted = createHarness('granted');
    await granted.service.getClipboard();
    await granted.service.resyncClipboard();
    assert.equal(granted.calls.get, 1);
    assert.equal(granted.calls.resync, 1);

    const manual = createHarness('prompt');
    const pasteTarget = {closest(selector) { return selector.includes('.clipboard') ? this : null; }};
    manual.listeners.paste({isTrusted: true, target: pasteTarget,
        clipboardData: {getData(type) { assert.equal(type, 'text/plain'); return 'manual text'; }}});
    assert.equal(manual.calls.get, 0, 'manual paste event should use event data rather than prompt read');
    assert.equal(manual.calls.set[0].data, 'manual text');
    assert.equal((await manual.service.getClipboard()).data, 'manual text');

    const remotePaste = createHarness('prompt');
    const remoteTarget = {closest(selector) { return selector.includes('.client .display') ? this : null; }};
    remotePaste.listeners.paste({isTrusted: true, target: remoteTarget,
        clipboardData: {getData() { return 'explicit display paste'; }}});
    assert.equal(remotePaste.calls.get, 0, 'event-provided remote paste data must not trigger a native read prompt');
    assert.equal(remotePaste.calls.set[0].data, 'explicit display paste');

    // A Ctrl+V key event is left completely to Guacamole.Keyboard. Reading
    // asynchronously and replaying it would reorder clipboard and key streams.
    assert.equal(remotePaste.listeners.keydown, undefined);

    const forms = createHarness('prompt');
    const formTarget = {closest(selector) {
        if (selector.includes('input')) return this;
        if (selector.includes('.client-body')) return this;
        return null;
    }};
    forms.listeners.paste({isTrusted: true, target: formTarget,
        clipboardData: {getData() { return 'form paste'; }}});
    assert.equal(forms.calls.get, 0, 'ordinary form paste must not invoke native clipboard read');
    assert.equal(forms.calls.set.length, 0, 'ordinary form paste must not be intercepted');

    const copied = createHarness('prompt');
    await copied.service.setClipboard({type: 'text/plain', data: 'remote copy'});
    assert.equal(copied.calls.set[0].data, 'remote copy');
    assert.equal((await copied.service.getClipboard()).data, 'remote copy');

    console.log('Guacamole clipboard: automatic permission gating and explicit paste paths passed.');
}

run().catch(error => { console.error(error); process.exitCode = 1; });
