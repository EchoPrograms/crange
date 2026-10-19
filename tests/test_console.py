import sys
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import crange_console as console
import crange_guac_broker as broker
from crange_guac_ticket import TicketError

class ConsoleTests(unittest.TestCase):
    def test_rfb_authentication_keeps_provider_ticket_server_side(self):
        client=Mock();client.recv.side_effect=[b'RFB 003.008\n',b'\1']
        stream=Mock();stream.read.side_effect=[b'RFB 003.008\n',b'\1',b'\2',b'c'*16,b'\0'*4]
        console.authenticate_rfb(client,stream,'test-ticket')
        stream.write.assert_any_call(console.vnc_response('test-ticket',b'c'*16))
        sent=b''.join(call.args[0] for call in client.sendall.call_args_list)
        self.assertNotIn(b'test-ticket',sent)
        self.assertEqual(sent,b'RFB 003.008\n\1\1'+b'\0'*4)

    def test_bad_provider_authentication_never_opens_client(self):
        client=Mock();client.recv.return_value=b'RFB 003.008\n'
        stream=Mock();stream.read.side_effect=[b'RFB 003.008\n',b'\1',b'\1',b'\0\0\0\1']
        with self.assertRaises(ConnectionError):console.authenticate_rfb(client,stream,'test-ticket')
        client.sendall.assert_called_once_with(b'RFB 003.008\n')

    def test_non_admin_cannot_launch_console(self):
        with patch('crange_guac_bridge._project_rows',return_value=[{'resource_key':'vm:box','console_available':False}]), \
             patch.object(console,'launch') as launch:
            with self.assertRaises(TicketError):broker.console_ticket({'project':100,'username':'student','resource_key':'vm:box'},auth=Mock(),root=Path('/tmp'),secret_hex='00'*16)
        launch.assert_not_called()

    def test_console_ticket_is_encrypted_and_does_not_expose_pve_credentials(self):
        api=Mock()
        with patch('crange_guac_bridge._project_rows',return_value=[{'resource_key':'vm:box','console_available':True}]), \
             patch('crange_guac_boxes.resolve',return_value=(None,None,None,api,'vm','proxmox1',128,None)), \
             patch.object(console,'launch',return_value={'protocol':'vnc','parameters':{'hostname':'10.60.0.20','port':'54321'}}):
            result=broker.console_ticket({'project':100,'username':'guacadmin','resource_key':'vm:box'},auth=Mock(),root=Path('/tmp'),secret_hex='00'*16)
        self.assertIn('https://crguac.brogano.dev/#/client/',result['url'])
        from urllib.parse import urlsplit
        self.assertEqual(urlsplit(result['url']).query, '')
        self.assertIn('?data=',urlsplit(result['url']).fragment)
        self.assertNotIn('10.60.0.20',result['url'])
        self.assertNotIn('54321',result['url'])
        self.assertNotIn('PVEVNC',result['url'])

class ContainerConsoleTests(unittest.TestCase):
    def test_container_uses_lxc_proxy_and_existing_server_side_auth_relay(self):
        import ssl
        from types import SimpleNamespace
        api=Mock(endpoint='https://pve.example:8006/api2/json',token='private-provider-token')
        api.ssl_context=SimpleNamespace(verify_mode=ssl.CERT_NONE)
        api.container_status.return_value='running'
        api.request.return_value={'port':5901,'ticket':'private-ticket'}
        listener=Mock();client=Mock();client.recv.return_value=b''
        listener.accept.return_value=(client,(console.GUAC_HOST,1234))
        listener.getsockname.return_value=(console.CONTROLLER_HOST,54321)
        ws=Mock();ws.recv.return_value=b'';websocket=Mock(return_value=ws)
        with patch.object(console.socket,'socket',return_value=listener), \
             patch.object(console.threading,'Thread',side_effect=lambda target,daemon:SimpleNamespace(start=target)), \
             patch.object(console,'authenticate_rfb') as authenticate, \
             patch.dict(sys.modules,{'websocket':SimpleNamespace(create_connection=websocket)}):
            result=console.launch(api,'proxmox1',127,kind='container')
        api.container_status.assert_called_once_with('proxmox1',127)
        api.status.assert_not_called()
        api.request.assert_called_once_with('POST','/nodes/proxmox1/lxc/127/vncproxy',{'websocket':1})
        self.assertIn('/nodes/proxmox1/lxc/127/vncwebsocket?',websocket.call_args.args[0])
        self.assertEqual(authenticate.call_args.args[2],'private-ticket')
        self.assertEqual(result['protocol'],'vnc')
        self.assertNotIn('private',str(result))

    def test_stopped_container_or_unknown_kind_never_allocates_listener(self):
        api=Mock();api.container_status.return_value='stopped'
        with patch.object(console.socket,'socket') as socket:
            with self.assertRaisesRegex(ValueError,'Start the box'):
                console.launch(api,'proxmox1',127,kind='container')
            with self.assertRaisesRegex(ValueError,'guest type'):
                console.launch(api,'proxmox1',127,kind='host')
        socket.assert_not_called()

    def test_admin_container_ticket_and_nonadmin_denial(self):
        api=Mock();data={'project':100,'username':'guacadmin','resource_key':'container:box'}
        with patch('crange_guac_bridge._project_rows',return_value=[{'resource_key':'container:box','console_available':True}]), \
             patch('crange_guac_boxes.resolve',return_value=(None,None,None,api,'container','proxmox1',127,None)), \
             patch.object(console,'launch',return_value={'protocol':'vnc','parameters':{'hostname':console.CONTROLLER_HOST,'port':'54321'}}) as launch:
            result=broker.console_ticket(data,auth=Mock(),root=Path('/tmp'),secret_hex='00'*16)
        launch.assert_called_once_with(api,'proxmox1',127,kind='container')
        self.assertIn('?data=',result['url'])
        with patch('crange_guac_bridge._project_rows',return_value=[{'resource_key':'container:box','console_available':False}]), \
             patch.object(console,'launch') as launch:
            with self.assertRaises(TicketError):broker.console_ticket(data,auth=Mock(),root=Path('/tmp'),secret_hex='00'*16)
        launch.assert_not_called()
