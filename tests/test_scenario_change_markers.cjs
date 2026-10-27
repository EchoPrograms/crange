'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('gui/scenario.js', 'utf8');
const names = ['scenarioPlanChangeKind', 'scenarioPlannedChange', 'pendingScenarioDeletions', 'pendingScenarioNetworkDeletions'];
const context = vm.createContext({});
vm.runInContext(`${names.map(name => source.slice(source.indexOf(`function ${name}(`), source.indexOf('\nfunction ', source.indexOf(`function ${name}(`) + 1))).join('\n')}`, context);
const plan = {stale: false, applied: false, changes: [
  {resource: 'module.scenario_box["web-01"]', change: 'create'},
  {resource: 'module.scenario_container["db"]', change: 'update'},
  {resource: 'module.scenario_box["old"]', change: 'delete'},
  {resource: 'module.custom_router["lan"]', change: 'no-op'}
]};
assert.equal(context.scenarioPlannedChange(plan, 'scenario_box', 'web-01'), 'create');
assert.equal(context.scenarioPlannedChange(plan, 'scenario_container', 'db'), 'update');
assert.equal(context.scenarioPlannedChange(plan, 'custom_router', 'lan'), '');
assert.equal(context.scenarioPlanChangeKind('update → delete'), 'delete');
assert.equal(context.scenarioPlanChangeKind('delete → create'), 'update', 'replacement remains an edited resource, not a pending deletion');
assert.equal(context.scenarioPlannedChange({...plan, stale: true}, 'scenario_box', 'web-01'), '', 'stale review must not classify current desired state');
assert.equal(context.scenarioPlannedChange({...plan, applied: true}, 'scenario_box', 'web-01'), '', 'applied changes must not remain pending');
const deletions = context.pendingScenarioDeletions(plan, {project_id: 7, scenario_boxes: {}, scenario_containers: {}});
assert.deepEqual(JSON.parse(JSON.stringify(deletions.map(row => [row.name, row.label]))), [['old', 'VM']]);
assert.equal(context.pendingScenarioDeletions(plan, {project_id: 7, scenario_boxes: {old: {}}, scenario_containers: {}}).length, 0, 'deletion preview omits resources still present in desired state');
const removedNetwork = context.pendingScenarioNetworkDeletions({stale: false, applied: false, changes: [{resource: 'module.custom_router[\"lan\"]', change: 'delete'}]}, {custom_networks: {}, applied_definitions: {custom_network: {lan: {network_id: 117}}}});
assert.equal(removedNetwork[0].name, 'lan');
assert.equal(context.pendingScenarioNetworkDeletions({stale: true, changes: [{resource: 'module.custom_router[\"lan\"]', change: 'delete'}]}, {custom_networks: {}, applied_definitions: {}}).length, 0, 'stale plans do not create deletion rows');
console.log('Scenario desired/applied marker classification checks passed.');
