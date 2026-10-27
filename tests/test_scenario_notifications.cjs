'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('gui/scenario.js', 'utf8');
const start = source.indexOf('function scenarioTabNoticeStates(');
const end = source.indexOf('\nfunction ', start + 1);
const context = vm.createContext({jobScope: op => op === 'release-apply' ? 'deployments' : op.startsWith('release-') ? 'releases' : 'workspace', jobContextTitle: job => job.operation});
const appSource=fs.readFileSync('gui/app.js','utf8'),helperStart=appSource.indexOf('function workflowJobActionRequired(');
vm.runInContext(appSource.slice(helperStart,appSource.indexOf('\nfunction ',helperStart+1)),context);
vm.runInContext(source.slice(start, end), context);
const jobs = [
    {id: 'review', project: 100, operation: 'stage-changes', status: 'succeeded', created: 10},
    {id: 'deploy', project: 100, operation: 'stage-deploy', status: 'succeeded', created: 20},
    {id: 'capture', project: 100, operation: 'release-capture', status: 'failed', created: 30},
    {id: 'rollout', project: 100, operation: 'release-apply', status: 'running', created: 40},
    {id: 'other-project', project: 200, operation: 'release-apply', status: 'failed', created: 50},
    {id: 'other-view', project: 100, operation: 'release-capture', status: 'failed', created: 60},
];
const contexts = new Map(jobs.map(j => [j.id, {view: j.id === 'other-view' ? 'project-overview' : 'scenario'}]));
const dismissed = new Set(), notified = new Set(['review', 'deploy', 'capture']);
let states = context.scenarioTabNoticeStates(100, jobs, contexts, dismissed, notified);
assert.equal(states.workspace.tone, 'success');
assert.equal(states.releases.tone, 'error');
assert.equal(states.deployments.tone, 'pending');
dismissed.add('deploy');
states = context.scenarioTabNoticeStates(100, jobs, contexts, dismissed, notified);
assert.equal(states.workspace, undefined, 'dismissing the latest result must not reveal an older result');
assert.equal(states.releases.tone, 'error', 'dismissal does not affect another tab');
dismissed.add('capture');
assert.equal(context.scenarioTabNoticeStates(100, jobs, contexts, dismissed, notified).releases, undefined);
const newJob = {id: 'new-deploy', project: 100, operation: 'stage-deploy', status: 'running', created: 70};
contexts.set(newJob.id, {view: 'scenario', scope: 'workspace'});
assert.equal(context.scenarioTabNoticeStates(100, [newJob, ...jobs], contexts, dismissed, notified).workspace.tone, 'pending');
const staged={id:'staged',project:100,operation:'stage-changes',status:'succeeded',created:80};
contexts.set(staged.id,{view:'scenario',scope:'workspace'});notified.add(staged.id);
assert.equal(context.scenarioTabNoticeStates(100,[staged],contexts,dismissed,notified).workspace.tone,'action','a plan waiting for deployment has an orange tab dot');
assert.match(context.scenarioTabNoticeStates(100,[staged],contexts,dismissed,notified).workspace.label,/Action required/);
console.log('Scenario tab notification scoping and dismissal checks passed.');
