import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import crange_scheduling as scheduling


def _row(disk_gb=None):
    box = {} if disk_gb is None else {'disk_gb': disk_gb}
    return {'kind': 'container', 'resource_key': 'container:web', 'box': box}


def _live(size=16):
    return {'node_name': 'proxmox2', 'disk': [{'datastore_id': 'local-lvm', 'size': size}]}


def test_freeze_preserves_measured_scenario_container_disk_when_omitted():
    frozen = scheduling.freeze(_row(), _live())

    assert frozen['resolved']['disk_gb'] == 16
    overrides = scheduling.tf_overrides({'placements': [frozen], 'requests': []})
    assert overrides['resolved_placements']['container:web']['disk_gb'] == 16


def test_freeze_keeps_explicit_container_growth_and_rejects_shrink():
    frozen = scheduling.freeze(_row(24), _live())
    assert frozen['resolved']['disk_gb'] == 24

    with pytest.raises(scheduling.PlacementError, match='Cannot shrink'):
        scheduling.freeze(_row(12), _live())


@pytest.mark.parametrize('size', [None, 0, -1, True, float('nan'), float('inf'), '16'])
def test_freeze_refuses_unknown_or_invalid_measured_container_disk(size):
    live = {'node_name': 'proxmox2', 'disk': [] if size is None else [
        {'datastore_id': 'local-lvm', 'size': size}]}
    with pytest.raises(scheduling.PlacementError, match='container root disk size'):
        scheduling.freeze(_row(), live)
