import json
import stat
import tempfile
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from crange_change_state import (
    compare_staging_definitions,
    load_applied_definitions,
    record_applied_definitions,
    staging_definitions,
)


def config():
    return {
        'scenario_boxes': {
            'web': {'team_id': 254, 'template': 'linux', 'cores': 2, 'password': 'must-not-store'},
            'team-web': {'team_id': 1, 'template': 'linux'},
        },
        'scenario_containers': {'db': {'team_id': 254, 'template': 'debian'}},
        'custom_networks': {'isolated': {'owner_team_id': 254, 'network_id': 111}},
    }


class ChangeStateTests(unittest.TestCase):
    def test_sanitized_snapshot_only_records_exact_reviewed_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            current = config()
            record_applied_definitions(directory, current, resource_keys={'vm:web'})
            snapshot = load_applied_definitions(directory)
            self.assertEqual(snapshot['scenario_box']['web']['template'], 'linux')
            self.assertNotIn('password', json.dumps(snapshot))
            self.assertEqual(snapshot['unknown']['scenario_box'], [])
            self.assertEqual(snapshot['unknown']['scenario_container'], ['db'])
            states = compare_staging_definitions(current, snapshot)
            self.assertEqual(states['scenario_box'], {'web': 'unchanged'})
            self.assertEqual(states['scenario_container']['db'], 'unknown')
            mode = stat.S_IMODE((Path(directory) / '.crange-applied-definitions.json').stat().st_mode)
            self.assertEqual(mode, 0o640)

    def test_add_edit_delete_and_unrelated_scope_preservation(self):
        with tempfile.TemporaryDirectory() as directory:
            record_applied_definitions(directory, config(), resource_keys={'vm:web', 'container:db', 'router:custom:isolated'})
            changed = config()
            changed['scenario_boxes']['web']['cores'] = 4
            changed['scenario_boxes']['new'] = {'team_id': 254, 'template': 'linux'}
            del changed['scenario_containers']['db']
            states = compare_staging_definitions(changed, load_applied_definitions(directory))
            self.assertEqual(states['scenario_box']['web'], 'edited')
            self.assertEqual(states['scenario_box']['new'], 'added')
            self.assertEqual(states['scenario_container']['db'], 'deleted')
            self.assertEqual(states['custom_network']['isolated'], 'unchanged')
            record_applied_definitions(directory, changed, resource_keys={'vm:web', 'vm:new'})
            states = compare_staging_definitions(changed, load_applied_definitions(directory))
            self.assertEqual(states['custom_network']['isolated'], 'unchanged')

    def test_complete_staging_acceptance_records_metadata_and_clears_deletions(self):
        with tempfile.TemporaryDirectory() as directory:
            record_applied_definitions(directory, config())
            changed = config()
            changed['scenario_boxes']['web']['health_checks'] = [{'type': 'tcp', 'port': 443}]
            del changed['scenario_containers']['db']
            self.assertEqual(compare_staging_definitions(changed, load_applied_definitions(directory))['scenario_box']['web'], 'edited')
            record_applied_definitions(directory, changed)
            states = compare_staging_definitions(changed, load_applied_definitions(directory))
            self.assertEqual(states['scenario_box']['web'], 'unchanged')
            self.assertEqual(states['scenario_container'], {})

    def test_staging_snapshot_excludes_other_teams(self):
        result = staging_definitions(config())
        self.assertEqual(set(result['scenario_box']), {'web'})
        self.assertEqual(result['custom_network']['isolated']['network_id'], 111)


if __name__ == '__main__':
    unittest.main()
