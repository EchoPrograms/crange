'use strict';

const assert = require('assert');
const inventory = require('../guacamole/extension/native-inventory.js');

const catalog = [];
for (let id = 1; id <= 10; id += 1) {
    catalog.push({source: 'mysql', connection: {identifier: String(id), name: 'old-' + id,
        protocol: 'rdp'}, url: inventory.connectionUrl('mysql-connection-' + id)});
}
catalog.push({source: 'ldap', connection: {identifier: '4', name: 'old-4', protocol: 'rdp'},
    url: inventory.connectionUrl('ldap-connection-4')});

const fresh = Object.create(null);
fresh['mysql\u00004'] = {data_source: 'mysql', identifier: '4', hostname: '10.60.0.20',
    guest_type: 'Container', state: 'running', guest_matched: true};
const rows = inventory.verifiedEntries(catalog, fresh);

assert.strictEqual(rows.length, 1);
assert.strictEqual(rows[0].source, 'mysql');
assert.strictEqual(rows[0].connection.identifier, '4');
assert.strictEqual(rows[0].metadata.hostname, '10.60.0.20');
assert.strictEqual(rows[0].metadata.state, 'running');
assert.strictEqual(rows[0].url, inventory.connectionUrl('mysql-connection-4'));
assert.notStrictEqual(rows[0].url, inventory.connectionUrl('ldap-connection-4'));
assert.deepStrictEqual(inventory.verifiedEntries(catalog, null), []);
