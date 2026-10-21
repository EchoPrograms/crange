const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('gui/app.js', 'utf8');
const start = source.indexOf('const declarations=new Set(');
const end = source.indexOf('async function pollJobDialog', start);
assert(start >= 0 && end > start);

async function completedJob(operation, status = 'succeeded') {
    const context = vm.createContext({});
    vm.runInContext(`
        let jobs = [], activeJobId = null, pinnedJob = null, view = 'scenario';
        let loads = 0, notices = [], projects = [{custom_networks: {}}];
        let templateCatalog = null;
        async function load() {
            loads++;
            projects = [{custom_networks: {test: {network_id: 100}}}];
        }
        function notice(message, error) { notices.push({message, error}); }
        ${source.slice(start, end)}
        updateTaskIndicator = () => {};
    `, context);
    context.operation = operation;
    context.status = status;
    return vm.runInContext(`(async () => {
        await acceptJobs([{id: 'job1', operation, status: 'running'}]);
        const before = loads;
        await acceptJobs([{id: 'job1', operation, status}]);
        const after = loads;
        await acceptJobs([{id: 'job1', operation, status}]);
        return {before, after, repeated: loads,
            testNetwork: projects[0].custom_networks.test?.network_id,
            error: notices[0]?.error};
    })()`, context);
}

(async () => {
    for (const operation of ['release-test-prepare', 'release-test-cleanup',
        'stage-changes', 'stage-deploy', 'release-apply', 'release-configure']) {
        const result = await completedJob(operation);
        assert.equal(result.before, 0, operation);
        assert.equal(result.after, 1, operation);
        assert.equal(result.repeated, 1, operation);
        assert.equal(result.testNetwork, 100, operation);
        assert.equal(result.error, false, operation);
    }
    // Failed configuration can still update checkpoints that must be visible.
    const failed = await completedJob('release-configure', 'failed');
    assert.equal(failed.after, 1);
    assert.equal(failed.error, true);
    // Runtime polling handles power changes without reloading all declarations.
    assert.equal((await completedJob('guac-box-control')).after, 0);
    console.log('Dashboard job completion refresh checks passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
