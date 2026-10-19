"""One-client hypervisor console relay; no guest listening ports or API secrets in clients."""
import socket
import ssl
import struct
import threading
import time
from urllib.parse import urlencode,urlsplit,urlunsplit

GUAC_HOST='10.60.0.10'
CONTROLLER_HOST='10.60.0.20'


def vnc_response(password,challenge):
    from cryptography.hazmat.primitives.ciphers import Cipher,modes
    try:
        from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
    except ImportError:
        from cryptography.hazmat.primitives.ciphers.algorithms import TripleDES
    key=password.encode('utf-8')[:8].ljust(8,b'\0')
    key=bytes(int(f'{byte:08b}'[::-1],2) for byte in key)
    return Cipher(TripleDES(key*3),modes.ECB()).encryptor().update(challenge)


class WebSocketStream:
    def __init__(self,ws):self.ws=ws;self.buffer=b''
    def read(self,size):
        while len(self.buffer)<size:
            part=self.ws.recv()
            if not isinstance(part,bytes) or not part:raise ConnectionError('Console stream closed.')
            self.buffer+=part
        result,self.buffer=self.buffer[:size],self.buffer[size:]
        return result
    def write(self,value):self.ws.send_binary(value)


def read_exact(client,size):
    result=b''
    while len(result)<size:
        part=client.recv(size-len(result))
        if not part:raise ConnectionError('Console client closed.')
        result+=part
    return result


def authenticate_rfb(client,stream,password):
    if stream.read(12)!=b'RFB 003.008\n':raise ValueError('Unsupported hypervisor console protocol.')
    client.sendall(b'RFB 003.008\n')
    version=read_exact(client,12)
    if version not in (b'RFB 003.008\n',b'RFB 003.003\n'):
        raise ValueError('Unsupported console client protocol.')
    stream.write(b'RFB 003.008\n')
    count=stream.read(1)[0]
    if count==0:raise ConnectionError('Hypervisor console security negotiation failed.')
    methods=stream.read(count)
    method=2 if 2 in methods else 1 if 1 in methods else None
    if method is None:raise ValueError('Unsupported hypervisor console authentication.')
    stream.write(bytes([method]))
    if method==2:stream.write(vnc_response(password,stream.read(16)))
    if stream.read(4)!=b'\0'*4:raise ConnectionError('Hypervisor console authentication failed.')
    # Only the trusted Guacamole server can reach this one-client socket. PVE
    # authentication terminates here; the PVE ticket is never sent to Guacamole.
    if version==b'RFB 003.003\n':client.sendall(struct.pack('!I',1))
    else:
        client.sendall(b'\1\1')
        if read_exact(client,1)!=b'\1':raise ValueError('Unexpected console security selection.')
        client.sendall(b'\0'*4)


def launch(api,node,vmid,*,kind='vm',lifetime=3600):
    if node not in {f'proxmox{i}' for i in range(1,7)} or type(vmid) is not int or vmid<100:
        raise ValueError('Invalid hypervisor console target.')
    if kind not in ('vm','jumpbox','container'):
        raise ValueError('Invalid hypervisor console guest type.')
    prefix='lxc' if kind=='container' else 'qemu'
    status=api.container_status if kind=='container' else api.status
    if status(node,vmid)!='running':raise ValueError('Start the box before opening its console.')
    import websocket
    listener=socket.socket();listener.bind((CONTROLLER_HOST,0));listener.listen(2)
    port=listener.getsockname()[1]
    def serve():
        client=ws=None
        phase='waiting for Guacamole'
        try:
            expires=time.monotonic()+45
            while time.monotonic()<expires:
                listener.settimeout(max(.1,expires-time.monotonic()))
                candidate,peer=listener.accept()
                if peer[0]!=GUAC_HOST:candidate.close();continue
                client=candidate;break
            if client is None:return
            listener.close();client.settimeout(20)
            phase='requesting hypervisor console'
            proxy=api.request('POST',f'/nodes/{node}/{prefix}/{vmid}/vncproxy',{'websocket':1})
            endpoint=urlsplit(api.websocket_endpoint() if getattr(api, 'compat_enabled', False) is True else api.endpoint)
            url=urlunsplit(('wss',endpoint.netloc,endpoint.path+f'/nodes/{node}/{prefix}/{vmid}/vncwebsocket',
                           urlencode({'port':proxy['port'],'vncticket':proxy['ticket']}),''))
            insecure=api.ssl_context.verify_mode==ssl.CERT_NONE
            phase='opening hypervisor WebSocket'
            ws=websocket.create_connection(url,header=['Authorization: PVEAPIToken='+api.token],
                subprotocols=['binary'],timeout=20,sslopt={'cert_reqs':ssl.CERT_NONE if insecure else ssl.CERT_REQUIRED})
            phase='authenticating hypervisor RFB'
            stream=WebSocketStream(ws);authenticate_rfb(client,stream,proxy['ticket'])
            phase='relaying console'
            client.settimeout(lifetime);ws.settimeout(lifetime)
            if stream.buffer:client.sendall(stream.buffer);stream.buffer=b''
            def upstream():
                try:
                    while True:
                        data=client.recv(65536)
                        if not data:break
                        ws.send_binary(data)
                except Exception:
                    pass
                finally:
                    ws.close()
            threading.Thread(target=upstream,daemon=True).start()
            while True:
                data=ws.recv()
                if not isinstance(data,bytes) or not data:break
                client.sendall(data)
        except Exception as error:
            # Never log WebSocket URLs/headers, PVE tickets, or stream contents.
            print('crange: console relay failed during '+phase+': '+type(error).__name__,flush=True)
        finally:
            listener.close()
            if client:client.close()
            if ws:ws.close()
    threading.Thread(target=serve,daemon=True).start()
    return {'protocol':'vnc','parameters':{'hostname':CONTROLLER_HOST,'port':str(port)},'expires_in':45}
