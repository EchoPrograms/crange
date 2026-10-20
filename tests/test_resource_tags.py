import copy
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from crange_resource_tags import candidates, migrate, write_journal
from crange_resource_identity import automation_config


class ResourceTagMigrationTests(unittest.TestCase):
    def setUp(self):
        self.row = {'node': 'proxmox6', 'vmid': 153, 'type': 'qemu', 'pool': 'crange-iac',
                    'name': 'crange-replica-153', 'tags': 'crange-cache-153;crange-image'}
        self.before = {'name': self.row['name'], 'tags': self.row['tags'], 'digest': 'before',
                       'template': 1, 'scsi0': 'local-lvm:base-153-disk-0,size=20G'}

    def test_offline_nodes_remain_pending_and_returning_nodes_are_selected(self):
        nodes = [{'node': 'proxmox6', 'status': 'offline'}]
        self.assertEqual(candidates(nodes, [self.row]), ([], [self.row]))
        nodes[0]['status'] = 'online'
        self.assertEqual(candidates(nodes, [self.row]), ([self.row], []))
        foreign = {**self.row, 'pool': 'foreign'}
        self.assertEqual(candidates(nodes, [foreign]), ([], []))

    def test_migration_uses_digest_and_keeps_all_logical_configuration(self):
        original = copy.deepcopy(self.before)
        records, calls = [], []
        owner = self
        class API:
            def request(self, method, path, fields=None):
                calls.append((method, fields))
                if method == 'PUT':
                    owner.assertEqual(records[-1]['state'], 'prepared')
                    owner.assertEqual(fields['digest'], 'before')
                    owner.assertEqual(set(fields), {'tags', 'description', 'digest'})
                    owner.before.update(fields)
                    owner.before['digest'] = 'after'
                    return None
                return automation_config(copy.deepcopy(owner.before))
        result = migrate(API(), self.row, save=records.append)
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(records[-1]['state'], 'verified')
        self.assertEqual(automation_config(self.before), {**original, 'digest': 'after'})
        self.assertEqual(len([c for c in calls if c[0] == 'PUT']), 1)

    def test_changed_identity_never_receives_a_write(self):
        self.before['name'] = 'foreign'
        owner = self
        class API:
            def request(self, method, path, fields=None):
                owner.assertEqual(method, 'GET')
                return owner.before
        with self.assertRaisesRegex(ValueError, 'identity changed'):
            migrate(API(), self.row, save=lambda _: self.fail('No mutation journal should be written'))

    def test_journal_is_private_and_updates_atomically(self):
        import tempfile, json, stat
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for state in ('prepared','verified'):
                write_journal(root,self.row,{'state':state})
                target=root/'proxmox6-153.json'
                self.assertEqual(json.loads(target.read_text()),{'state':state})
                self.assertEqual(stat.S_IMODE(target.stat().st_mode),0o600)
                self.assertEqual(list(root.glob('*.tmp')),[])
