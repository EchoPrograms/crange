'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('gui/app.js', 'utf8');
const start = source.indexOf('function infrastructurePendingState(');
const end = source.indexOf('function updateProjectNavigationNotifications(', start);
assert(start >= 0 && end > start);

const context = vm.createContext({});
vm.runInContext(`
 function jobContextTitle(job) { return job.operation; }
 ${source.slice(start, end)}
`, context);

const pendingReview = context.infrastructurePendingState(7, [
    {project: 7, operation: 'scenario-edit', status: 'succeeded', created: 30}
], null);
assert.equal(pendingReview.state, 'review');

const pendingApply = context.infrastructurePendingState(7, [], {
    applied: false, stale: false, changes: [{}, {}]
});
assert.equal(pendingApply.state, 'apply');
assert.match(pendingApply.label, /2 reviewed infrastructure changes/);
assert.equal(context.infrastructurePendingState(7, [
    {project: 7, operation: 'scenario-edit', status: 'succeeded', created: 30}
], {applied: false, stale: false, changes: [{change: 'update'}]}).state, 'apply');

const stale = context.infrastructurePendingState(7, [], {
    applied: false, stale: true, changes: [{}]
});
assert.equal(stale.state, 'review');
assert.equal(context.infrastructurePendingState(7, [], null), null);
assert.equal(context.infrastructurePendingState(7, [], {
    applied: false, stale: false, changes: [{change: 'no-op'}]
}), null);

const now = 1000;
const baseJobs = [
    {id: 'active', project: 7, operation: 'team-add', status: 'running', created: 10},
    {id: 'success', project: 7, operation: 'scenario-edit', status: 'succeeded', created: 20, finished: 990},
    {id: 'failed', project: 7, operation: 'guac-users', status: 'failed', created: 30, finished: 40},
    {id: 'other', project: 8, operation: 'team-add', status: 'failed', created: 40, finished: 50}
];
const contexts = new Map([
    ['active', {view: 'teams'}], ['success', {view: 'scenario'}],
    ['failed', {view: 'project-users'}], ['other', {view: 'teams'}]
]);
const notified = new Set(['success', 'failed', 'other']);
const states = context.projectNavigationNoticeStates(baseJobs, contexts, new Set(), 7, notified, now);
assert.equal(states.teams.tone, 'pending');
assert.equal(states.scenario.tone, 'success');
assert.equal(states['project-users'].tone, 'error');
assert.equal(states.machines, undefined);
assert.equal(context.projectNavigationNoticeStates(baseJobs, contexts, new Set(['failed']), 7, notified, now)['project-users'], undefined);
assert.equal(context.projectNavigationNoticeStates(baseJobs, contexts, new Set(), 8, notified, now).teams.tone, 'error');
assert.equal(context.projectNavigationNoticeStates([
    {id: 'old', project: 7, operation: 'team-add', status: 'succeeded', created: 1, finished: 900}
], new Map([['old', {view: 'teams'}]]), new Set(), 7, now).teams, undefined);

console.log('Project navigation notices and infrastructure pending state checks passed.');
