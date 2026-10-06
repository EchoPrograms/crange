import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from crange_ct_read_relay import validate_request, validate_guest, dispatch


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.request = {'node': 'proxmox2', 'vmid': 8840, 'command': 'machine-id'}
        self.policy = {'peer_uid': 101000, 'allowed_guests': {'proxmox2': {'8840': 'crange-job-source'}},
                       'node_addresses': {'proxmox2': '10.0.0.102'}}
        self.guest = {'tags': 'crange-image;crange-job-source', 'unprivileged': 1,
                      'net0': 'name=eth0,bridge=crprobe1450,link_down=1', 'template': 0}
        self.members = [{'type': 'lxc', 'node': 'proxmox2', 'vmid': 8840}]

    def test_authentication_scope_and_commands_are_exact(self):
        validate_request(self.request, self.policy, 101000)
        for changes, uid in [({}, 0), ({'vmid': 8841}, 101000), ({'vmid': True}, 101000),
                ({'node': 'proxmox1'}, 101000), ({'command': 'sh'}, 101000),
                ({'argv': ['/bin/sh']}, 101000), ({'node': 'proxmox2;id'}, 101000)]:
            with self.subTest(changes=changes, uid=uid), self.assertRaises(ValueError):
                validate_request({**self.request, **changes}, self.policy, uid)

    def test_pool_ownership_privilege_mounts_and_isolation_fail_closed(self):
        validate_guest(self.guest, self.members, 'proxmox2', 8840, 'crange-job-source')
        for mutation in [{'unprivileged': 0}, {'template': 1}, {'tags': 'crange-image'},
                {'mp0': '/root,mp=/data'}, {'dev0': '/dev/foo'}, {'lxc.mount.entry': '/etc'},
                {'net0': 'name=eth0,bridge=craccess,link_down=1'},
                {'net1': 'name=eth1,bridge=crprobe1450'}, {'net0': ''}]:
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                validate_guest({**self.guest, **mutation}, self.members, 'proxmox2', 8840, 'crange-job-source')
        for members in [[], self.members * 2, [{**self.members[0], 'type': 'qemu'}], [{**self.members[0], 'node': 'proxmox1'}]]:
            with self.assertRaises(ValueError):
                validate_guest(self.guest, members, 'proxmox2', 8840, 'crange-job-source')

    def test_container_exec_uses_bounded_unix_transport(self):
        import socket
        import tempfile
        import threading
        from unittest.mock import patch
        from crange_distribution import SSHContainerExec
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'relay.sock'
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(path)); listener.listen(1)
            seen = []
            def server():
                conn, _ = listener.accept()
                with conn:
                    seen.append(json.loads(conn.recv(4096)))
                    conn.sendall(json.dumps({'value': 'a' * 32}).encode() + b'\n')
            thread = threading.Thread(target=server); thread.start()
            try:
                with patch('crange_distribution.Path', return_value=path):
                    result = SSHContainerExec()('proxmox2', 8840, ['/bin/cat', '/etc/machine-id'])
                self.assertEqual(result, 'a' * 32)
                self.assertEqual(seen, [self.request])
            finally:
                thread.join(timeout=3); listener.close()

    def test_dispatch_uses_fixed_remote_program_and_never_forwards_arbitrary_shell(self):
        run = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps({'value': 'a'*32}), ''))
        self.assertEqual(dispatch(self.request, self.policy, 101000, run)['value'], 'a'*32)
        argv = run.call_args.args[0]
        self.assertIn('root@10.0.0.102', argv)
        self.assertIn('validate_guest', argv[-1])
        run.reset_mock()
        with self.assertRaises(ValueError):
            dispatch({**self.request, 'command': 'machine-id;id'}, self.policy, 101000, run)
        run.assert_not_called()
