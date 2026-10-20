'use strict';

const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const source = fs.readFileSync('guacamole/extension/ui.js', 'utf8');
const css = fs.readFileSync('guacamole/extension/theme.css', 'utf8');
let homeTemplate = '';
let directiveDefinition;
let listTemplate = '';
const angularModule = {
    config() { return this; },
    run(definition) {
        const fn = definition[definition.length - 1];
        const cache = {put(_key, value) { homeTemplate = value; }};
        fn(cache);
        return this;
    },
    directive(_name, definition) { directiveDefinition = definition[definition.length - 1]; return this; }
};
const sandbox = {
    angular: {module: () => angularModule, forEach(value, callback) {
        if (Array.isArray(value)) value.forEach(callback);
        else Object.keys(value || {}).forEach(key => callback(value[key], key));
    }, extend: Object.assign},
    window: {clearTimeout(){},location: {href: 'https://guac.example/'}, localStorage: {
        value: '', getItem() { return this.value; }, setItem(_key, value) { this.value = value; }
    }, CRangeNativeInventory: {connectionUrl: id => id, verifiedEntries: () => []}},
    document: {head: null, addEventListener() {}},
    URL,
    console
};
vm.runInNewContext(source, sandbox, {filename: 'ui.js'});

// Native markup declares multiple icon sizes; every stock candidate must go.
const removedIcons = [], icons = [{remove() {removedIcons.push(this);}}, {remove() {removedIcons.push(this);}}];
const touch = {setAttribute(key, value) {this[key] = value;}};
const appended = [];
const faviconDocument = {
    head: {appendChild(icon) {appended.push(icon);}},
    querySelectorAll(selector) {return selector.includes('apple-touch-icon') ? [touch] : icons;},
    createElement() {return {setAttribute(key, value) {this[key] = value;}};},
    addEventListener() {}
};
vm.runInNewContext(source, {...sandbox, document: faviconDocument}, {filename: 'ui.js'});
assert.equal(removedIcons.length, 2);
assert.equal(appended.length, 1);
assert.equal(appended[0].sizes, 'any');
assert.equal(appended[0].href, 'https://guac.example/app/ext/crange-theme/assets/remote-access.svg?v=3');
assert.equal(touch.href, 'https://guac.example/app/ext/crange-theme/assets/remote-access-192.png?v=3');

async function renderFor(savedProject, nativePending = false, dataSource = 'mysql', availableSources = [], connectionGroups = [], refreshedGroups = connectionGroups, listBoxes = null) {
    sandbox.window.localStorage.value = savedProject;
    const timers = [];
    const watchers = [];
    const scope = {$parent: {}, connectionGroups, $watch(expression, callback) { watchers.push({expression, callback}); }, $on() {}};
    const http = {post(_url, payload) {
        if (payload.operation === 'list') return Promise.resolve({data: {boxes: listBoxes ? listBoxes() : [
            {project: 100, team: 1, resource_key: 'p100t001/vm', name: 'server', kind: 'VM', protocols: [{protocol: 'ssh', connection_name: 'server-ssh'}]}
        ]}});
        if (payload.operation === 'native-inventory') return nativePending ? new Promise(() => {}) : Promise.resolve({data: {connections: []}});
        if (payload.operation === 'status') return Promise.resolve({data: {state: 'running'}});
        return Promise.reject(new Error('Unexpected test operation'));
    }};
    const interval = (callback, delay) => { const timer = {callback, delay}; timers.push(timer); return timer; };
    interval.cancel = () => {};
    const nativeRequests = [];
    const originalPost = http.post;
    http.post = (url, payload, options) => { if (payload.operation === 'native-inventory') nativeRequests.push(options.headers['X-Guacamole-Data-Source']); return originalPost(url, payload); };
    const auth = {getCurrentToken: () => 'test-token', getDataSource: () => dataSource, getAvailableDataSources: () => availableSources};
    const clientId = {Types: {CONNECTION: 'connection'}, toString: () => 'client-id'};
    const groupRequests = [];
    const groups = {getConnectionGroupTree() {}};
    const dataSources = {apply(operation, sources, root) {
        assert.strictEqual(operation, groups.getConnectionGroupTree);
        assert.strictEqual(root, 'ROOT');
        groupRequests.push(Array.from(sources));
        return Promise.resolve(typeof refreshedGroups === 'function' ? refreshedGroups() : refreshedGroups);
    }};
    const directive = directiveDefinition(http, interval, auth, clientId, undefined, groups, dataSources, {ROOT_IDENTIFIER: 'ROOT'});
    listTemplate = directive.template;
    assert.ok(listTemplate.includes('ng-model="cr.search"'), 'search shares the list model across structural scopes');
    const link = directive.link;
    link(scope, {});
    watchers.filter(w => w.expression === 'connectionGroups').forEach(w => w.callback(scope.connectionGroups));
    const initialSelection = scope.cr.selectedProject;
    const initialOptionIds = Array.from(scope.cr.projects, p => p.id);
    const loadingDuringInitialRender = scope.cr.loading;
    await scope.refresh();
    await Promise.resolve();
    await Promise.resolve();
    return {scope, initialSelection, initialOptionIds, loadingDuringInitialRender, timers, nativeRequests, groupRequests};
}

(async () => {
    sandbox.window.sessionStorage={getItem(){return '100';},removeItem(){}};
    const returned=await renderFor('all',false,'json',['json']);assert.equal(returned.scope.cr.page,'servers');assert.equal(returned.scope.cr.selectedProject,'100');delete sandbox.window.sessionStorage;
    const ticketOnly = await renderFor('', false, 'json', ['json']);
    ticketOnly.scope.cr.openProject('100');ticketOnly.scope.cr.navigate('preferences');ticketOnly.scope.cr.back();assert.equal(ticketOnly.scope.cr.page,'servers');assert.equal(ticketOnly.scope.cr.selectedProject,'100');
    assert.deepStrictEqual(ticketOnly.nativeRequests, [], 'ticket-only sessions do not call unavailable native inventory');
    assert.strictEqual(ticketOnly.scope.nativeInventoryError, '');
    assert.strictEqual(ticketOnly.scope.cr.nativeLoading, false);
    const mixedTicket = await renderFor('', false, 'json', ['json', 'postgresql', 'postgresql-shared']);
    assert.deepStrictEqual(mixedTicket.nativeRequests, [], 'dashboard tickets do not assume a native account even when database contexts are advertised');
    assert.strictEqual(mixedTicket.scope.nativeInventoryError, '');
    const mixedTree = await renderFor('', false, 'json', ['json','mysql'], {
        mysql: {childConnections: [{name: 'server-ssh', protocol: 'ssh', identifier: 'native-1'}]},
        json: {childConnections: [{name: 'Console', protocol: 'vnc', identifier: 'Console'}]}
    });
    assert.ok(mixedTree.scope.boxes[0].protocols[0].url, 'native protocol links survive a mixed-context console login');
    const missingTree = await renderFor('', false, 'json', ['json']);
    assert.strictEqual(missingTree.scope.boxes[0].protocols[0].url, null, 'never invent links outside the authenticated tree');
    const staleTree = await renderFor('', false, 'mysql', ['mysql'], {mysql: {childConnections: []}}, {
        mysql: {childConnections: [{name: 'server-ssh', protocol: 'ssh', identifier: 'new-connection'}]}
    });
    assert.ok(staleTree.scope.boxes[0].protocols[0].url, 'Refresh resolves newly granted connections from the live authenticated tree');
    assert.deepStrictEqual(staleTree.groupRequests[0], ['mysql'], 'tree reload uses only the authenticated contexts');
    const background = staleTree.timers.find(timer => timer.delay === 15000);
    assert.ok(background, 'existing portal sessions discover new boxes without a manual page reload');
    const priorRequests = staleTree.groupRequests.length;
    background.callback();await staleTree.scope.refresh();
    assert.strictEqual(staleTree.groupRequests.length, priorRequests + 1, 'overlapping background/manual refreshes share one request');
    let declared = [], granted = {mysql: {childConnections: []}};
    const deployed = await renderFor('', false, 'mysql', ['mysql'], granted, () => granted, () => declared);
    assert.strictEqual(deployed.scope.boxes.length, 0);
    declared = [{project:100,team:1,resource_key:'jumpbox:new',name:'New jump box',protocols:[{protocol:'ssh',connection_name:'new-ssh'}]}];
    granted = {mysql: {childConnections:[{name:'new-ssh',protocol:'ssh',identifier:'new-grant'}]}};
    deployed.timers.find(timer=>timer.delay===15000).callback();await deployed.scope.refresh();
    assert.strictEqual(deployed.scope.boxes[0].name, 'New jump box', 'a newly deployed box appears in an already open portal');
    assert.ok(deployed.scope.boxes[0].protocols[0].url);
    granted = {mysql: {childConnections:[]}};
    await deployed.scope.refresh();
    assert.strictEqual(deployed.scope.boxes[0].protocols[0].url, null, 'revoked native grants remove connection links');
    const persisted = await renderFor('100');
    assert.strictEqual(persisted.initialSelection, '100', 'valid saved project remains selected while options load');
    assert.deepStrictEqual(persisted.initialOptionIds, ['all', '100'], 'the All projects and saved-project options exist before inventory arrives');
    assert.strictEqual(persisted.loadingDuringInitialRender, true, 'title indicator is active during initial list loading');
    assert.strictEqual(persisted.scope.cr.loading, false, 'title indicator returns to its reserved idle slot when loading ends');
    assert.strictEqual(persisted.scope.projectOptionsReady, true);
    assert.deepStrictEqual(Array.from(persisted.scope.cr.projects, p => p.id), ['all', '100']);
    assert.strictEqual(persisted.scope.cr.selectedProject, '100');

    const invalid = await renderFor('999');
    assert.deepStrictEqual(invalid.initialOptionIds, ['all'], 'out-of-range saved project is discarded before option rendering');
    assert.strictEqual(invalid.scope.projectOptionsReady, true);
    assert.strictEqual(invalid.scope.cr.selectedProject, 'all', 'invalid saved project falls back to All projects');

    const defaulted = await renderFor('');
    assert.strictEqual(defaulted.initialSelection, 'all', 'All projects is the explicit default before inventory arrives');
    assert.ok(defaulted.scope.cr.projects.some(p => p.id === 'all' && p.label === 'All projects'));
    assert.ok(homeTemplate.includes('ng-options="project.id as project.label for project in cr.projects"'));
    assert.ok(homeTemplate.includes('class="cr-home-logo"'));
    assert.ok(homeTemplate.includes('class="cr-title-loading"'));
    assert.ok(homeTemplate.includes('ng-class="{active: cr.loading || cr.nativeLoading}"'));
    assert.ok(!listTemplate.includes('Verifying native connections'), 'native inventory verification has no prominent placeholder');
    assert.ok(!listTemplate.includes('Loading servers'), 'initial server loading stays quiet while the title spinner is active');
    assert.ok(!listTemplate.includes('ng-if="nativeInventoryLoading"'), 'native verification has no prominent placeholder branch');
    const nativePending = await renderFor('', true);
    assert.strictEqual(nativePending.scope.nativeInventoryLoading, true, 'native inventory request remains pending');
    assert.strictEqual(nativePending.scope.cr.nativeLoading, true, 'reserved title spinner indicates native verification');
    assert.ok(nativePending.scope.boxes.some(box => box.name === 'server'), 'verified server rows remain visible during native verification');
    assert.deepStrictEqual(Array.from(nativePending.scope.cr.projects, p => p.id), ['all', '100'], 'real project options replace the initial All projects option');
    assert.ok(listTemplate.includes('box.job.status !== \'succeeded\''), 'running and failed power jobs remain visible');
    assert.ok(listTemplate.includes('box.job.status !== \'interrupted\''), 'interrupted job state remains visible');
    assert.match(css, /\.cr-title-loading\s*\{[^}]*width:\s*1\.15em;[^}]*visibility:\s*hidden;/s);
    assert.match(css, /\.cr-title-loading\.active\s*\{\s*visibility:\s*visible;\s*\}/);
    assert.ok(!source.includes('else scope.refresh();'), 'successful power task does not trigger a full list refresh/modal path');
    process.stdout.write('Guacamole UI selection, loading-slot, and refresh behavior checks passed.\n');
})().catch(error => { process.stderr.write(error.stack + '\n'); process.exitCode = 1; });
