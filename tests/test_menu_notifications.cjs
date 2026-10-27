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
assert.equal(states.scenario.tone, 'action');
assert.equal(states['project-users'].tone, 'error');
assert.equal(states.machines, undefined);
assert.equal(context.projectNavigationNoticeStates(baseJobs, contexts, new Set(['failed']), 7, notified, now)['project-users'], undefined);
assert.equal(context.projectNavigationNoticeStates(baseJobs, contexts, new Set(), 8, notified, now).teams.tone, 'error');
assert.equal(context.projectNavigationNoticeStates([
    {id: 'old', project: 7, operation: 'team-add', status: 'succeeded', created: 1, finished: 900}
], new Map([['old', {view: 'teams'}]]), new Set(), 7, now).teams, undefined);

const staged={id:'staged',project:7,operation:'stage-changes',status:'succeeded',created:100,output:'Plan ID: '+ 'a'.repeat(32)};
const deployment={id:'deployed',project:7,operation:'stage-deploy',status:'succeeded',created:110};
assert.equal(context.workflowJobActionRequired(staged,[staged]),true,'staging waits for the user to deploy');
assert.equal(context.workflowJobActionRequired(staged,[deployment,staged]),false,'deployment completes the staging handoff');
assert.equal(context.workflowJobActionRequired(staged,[staged],[{project_id:7,staged_plans:[{plan_id:'a'.repeat(32),applied_at:110}]}]),false,'applied plans do not need further deployment');
assert.equal(context.workflowJobActionRequired({...deployment,status:'running'},[]),false,'running work does not need user action');
assert.equal(context.workflowJobActionRequired({...deployment,status:'reconcile-required'},[]),true,'manual cleanup is an action-required state');
const stagedContexts=new Map([['staged',{view:'scenario'}],['deployed',{view:'scenario'}]]);
assert.equal(context.projectNavigationNoticeStates([staged],stagedContexts,new Set(),7,new Set(['staged'])).scenario.tone,'action');
assert.equal(context.projectNavigationNoticeStates([deployment,staged],stagedContexts,new Set(['deployed']),7,new Set(['staged','deployed'])).scenario,undefined,'dismissal cannot reveal an older completed notification');
console.log('Project navigation action states and infrastructure pending checks passed.');
