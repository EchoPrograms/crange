import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
import ssl
from unittest.mock import Mock, patch
from urllib import error, parse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from crange_proxmox_compat import Adapter, CompatError
from crange import ProxmoxAPI


class ClusterAPIFailoverTests(unittest.TestCase):
    def adapter(self, handler):
        adapter = Adapter.__new__(Adapter)
        adapter.origin = parse.urlsplit('https://10.0.0.101:8006/')
        adapter.gateway_origins = [adapter.origin, parse.urlsplit('https://10.0.0.104:8006'),
                                   parse.urlsplit('https://10.0.0.106:8006')]
        adapter.gateway_lock = threading.RLock(); adapter.gateway_verified_until = 0
        adapter._request = Mock(side_effect=handler)
        return adapter

    def membership(self, origin, name='proxmoxcluster', quorate=1):
        number = int(origin.hostname.rsplit('.', 1)[1]) - 100
        return 200, {}, json.dumps({'data': [
            {'type': 'cluster', 'name': name, 'quorate': quorate},
            *[{'type': 'node', 'name': f'proxmox{i}', 'online': 1, 'local': int(i == number)} for i in range(1, 7)]]}).encode()

    def test_dead_primary_selects_verified_same_cluster_before_one_write(self):
        def request(method, path, headers, body=None, *, origin):
            if origin.hostname.endswith('.101'): raise error.URLError('offline')
            if path.endswith('/cluster/status'): return self.membership(origin)
            return 200, {}, b'{"data":"UPID:created"}'
        adapter = self.adapter(request)
        adapter._gateway_request('POST', '/api2/json/nodes/proxmox6/qemu', {'Authorization': 'PVEAPIToken=test'}, b'vmid=123')
        writes = [call for call in adapter._request.call_args_list if call.args[0] == 'POST']
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0].kwargs['origin'].hostname, '10.0.0.104')

    def test_uncertain_write_never_replayed_to_another_gateway(self):
        def request(method, path, headers, body=None, *, origin):
            if path.endswith('/cluster/status'): return self.membership(origin)
            raise error.URLError('response lost')
        adapter = self.adapter(request)
        with self.assertRaisesRegex(CompatError, 'not replayed'):
            adapter._gateway_request('POST', '/api2/json/nodes/proxmox6/qemu', {'Authorization': 'PVEAPIToken=test'}, b'vmid=123')
        self.assertEqual(sum(call.args[0] == 'POST' for call in adapter._request.call_args_list), 1)

    def test_credential_denial_does_not_try_other_gateways(self):
        adapter = self.adapter(lambda *a, **k: (403, {}, b'{}'))
        with self.assertRaisesRegex(CompatError, 'credentials were denied'):
            adapter._gateway_request('GET', '/api2/json/nodes', {'Authorization': 'PVEAPIToken=test'})
        self.assertEqual(adapter._request.call_count, 1)

    def test_wrong_cluster_or_nonquorate_gateway_never_receives_write(self):
        for name, quorate in [('foreign', 1), ('proxmoxcluster', 0)]:
            adapter = self.adapter(lambda method, path, headers, body=None, *, origin: self.membership(origin, name, quorate))
            with self.subTest(name=name, quorate=quorate), self.assertRaises(CompatError):
                adapter._gateway_request('PUT', '/api2/json/nodes/proxmox6/qemu/123/config', {'Authorization': 'PVEAPIToken=test'}, b'name=owned')
            self.assertTrue(all(call.args[0] == 'GET' for call in adapter._request.call_args_list))

    def test_target_node_error_is_not_retried_as_a_gateway_failure(self):
        def request(method, path, headers, body=None, *, origin):
            return self.membership(origin) if path.endswith('/cluster/status') else (595, {}, b'{"data":null}')
        adapter = self.adapter(request)
        self.assertEqual(adapter._gateway_request('GET', '/api2/json/nodes/proxmox1/status', {'Authorization': 'PVEAPIToken=test'})[0], 595)
        self.assertEqual(adapter._request.call_count, 2)

    def test_safe_read_can_move_after_transport_failure(self):
        failed = set()
        def request(method, path, headers, body=None, *, origin):
            if origin.hostname.endswith('.101'): raise error.URLError('offline')
            if path.endswith('/cluster/status'): return self.membership(origin)
            if origin.hostname.endswith('.104'): failed.add(origin.hostname); raise error.URLError('read lost')
            return 200, {}, b'{"data":[]}'
        adapter = self.adapter(request)
        self.assertEqual(adapter._gateway_request('GET', '/api2/json/nodes', {'Authorization': 'PVEAPIToken=test'})[0], 200)
        self.assertEqual(adapter.origin.hostname, '10.0.0.106')
        self.assertEqual(failed, {'10.0.0.104'})

    def test_worker_api_uses_the_same_protected_loopback_adapter(self):
        with patch.object(Path, 'read_text', return_value='PROXMOX_VE_ENDPOINT=https://10.0.0.101:8006/\nPROXMOX_VE_API_TOKEN=test\n'), \
             patch.object(Path, 'exists', return_value=True), \
             patch('pwd.getpwnam', return_value=SimpleNamespace(pw_uid=1000)), \
             patch('crange_proxmox_compat.load_config', return_value={'port': 18766}) as settings:
            client = ProxmoxAPI()
        self.assertEqual(client.endpoint, 'https://127.0.0.1:18766/api2/json')
        self.assertEqual(settings.call_args.kwargs['owner_uid'], 1000)

    def test_console_uses_real_verified_gateway_and_rejects_foreign_origin(self):
        client = ProxmoxAPI.__new__(ProxmoxAPI)
        client.compat_enabled = True; client.compat_upstream = 'https://10.0.0.101:8006/'
        client.endpoint = 'https://127.0.0.1:18766/api2/json'
        client.ssl_context = ssl._create_unverified_context(); client.request = Mock()
        for host, allowed in [('10.0.0.104', True), ('untrusted.invalid', False)]:
            response = Mock(); response.__enter__ = Mock(return_value=response); response.__exit__ = Mock()
            response.read.return_value = json.dumps({'active_upstream': f'https://{host}:8006', 'recently_verified': True}).encode()
            with self.subTest(host=host), patch('urllib.request.urlopen', return_value=response):
                if allowed: self.assertEqual(client.websocket_endpoint(), 'https://10.0.0.104:8006/api2/json')
                else:
                    with self.assertRaisesRegex(Exception, 'console gateway'): client.websocket_endpoint()
