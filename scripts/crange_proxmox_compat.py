#!/usr/bin/env python3
"""Pinned-provider compatibility for measured PVE 9.0 CT network schemas.

Loopback only; callers supply their scoped PVE API token. This adapter never
injects credentials or edits hosts. Only explicit false host-managed in netN on
approved old-node CT create/config requests is removed. Everything else retains
its API semantics. No request or response body is logged.
"""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import ssl
import threading
import time
from urllib import error, parse, request


class CompatError(ValueError):
    pass


def config_path():
    return Path(os.environ.get('CRANGE_PROXMOX_COMPAT_CONFIG', Path.home() / '.config/crange/proxmox-compat.json'))


def tls_paths(config, path):
    path = Path(path)
    return (Path(config.get('tls_certificate', path.with_suffix('.crt'))),
            Path(config.get('tls_key', path.with_suffix('.key'))))


def load_config(path=None, *, owner_uid=None):
    path = Path(path or config_path())
    stat = path.stat()
    expected_owner = os.geteuid() if owner_uid is None else owner_uid
    if isinstance(expected_owner, bool) or not isinstance(expected_owner, int) or expected_owner < 0:
        raise CompatError('Invalid expected compatibility settings owner.')
    if owner_uid is not None and os.geteuid() != 0 and expected_owner != os.geteuid():
        raise CompatError('Only the installer may select a different settings owner.')
    if stat.st_mode & 0o022 or stat.st_uid not in (0, expected_owner):
        raise CompatError('Compatibility settings must be controller-owned and not writable by other users.')
    config = json.loads(path.read_text())
    target = parse.urlsplit(config.get('upstream', ''))
    if (target.scheme != 'https' or not target.hostname or target.username or target.password or
            target.query or target.fragment or target.path not in ('', '/', '/api2/json', '/api2/json/')):
        raise CompatError('A fixed HTTPS Proxmox upstream origin is required.')
    if config.get('bind', '127.0.0.1') != '127.0.0.1':
        raise CompatError('Compatibility adapter may bind only IPv4 loopback.')
    if not isinstance(config.get('port'), int) or not 1024 <= config['port'] <= 65535:
        raise CompatError('Invalid compatibility adapter port.')
    if not config.get('revision') or not isinstance(config.get('nodes'), dict):
        raise CompatError('Compatibility revision and measured node schema evidence required.')
    for node, proof in config['nodes'].items():
        if (node not in {f'proxmox{i}' for i in range(1, 7)} or proof.get('version') != '9.0.3' or
                proof.get('host_managed_supported') is not False or not proof.get('job_id') or
                not re.fullmatch(r'[a-f0-9]{64}', proof.get('schema_sha256', ''))):
            raise CompatError('Only explicitly measured unsupported 9.0.3 node schemas are approved.')
    if not 1 <= config.get('concurrency', 4) <= 8 or not 1 <= config.get('timeout_seconds', 20) <= 30:
        raise CompatError('Compatibility timeout/concurrency exceeds bounds.')
    certificate, key = tls_paths(config, path)
    for material, private in ((certificate, False), (key, True)):
        if not material.is_absolute() or material.resolve().parent != path.resolve().parent:
            raise CompatError('Compatibility TLS material must reside beside protected settings.')
        info = material.stat()
        if info.st_uid not in (0, expected_owner) or info.st_mode & (0o077 if private else 0o022):
            raise CompatError('Compatibility TLS material has unsafe ownership or permissions.')
    return config


def fingerprint(path=None):
    path = Path(path or config_path())
    if not path.exists(): return {'enabled': False}
    config = load_config(path)
    code = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    digest = hashlib.sha256(json.dumps(config, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    certificate, _ = tls_paths(config, path)
    return {'enabled': True, 'revision': config['revision'], 'config_sha256': digest,
            'code_sha256': code,
            'tls_certificate_sha256': hashlib.sha256(certificate.read_bytes()).hexdigest(),
            'endpoint': f'https://127.0.0.1:{config["port"]}/'}


def reviewed_identity(path=None):
    """Pin protected settings, adapter code and actual running service identity."""
    expected = fingerprint(path)
    if not expected['enabled']: return expected
    endpoint = expected['endpoint'].rstrip('/') + '/__crange_compat/identity'
    settings_path = Path(path or config_path())
    certificate, _ = tls_paths(load_config(settings_path), settings_path)
    context = ssl.create_default_context(cafile=str(certificate))
    opener = request.build_opener(NoRedirect(), request.HTTPSHandler(context=context), request.ProxyHandler({}))
    try:
        with opener.open(endpoint, timeout=3) as response:
            observed = json.loads(response.read(8192))
    except Exception as exc:
        raise CompatError('Configured compatibility service is unavailable; restage after recovery.') from exc
    if observed != expected:
        raise CompatError('Running compatibility code/settings differ from protected plan inputs; restage.')
    return expected


def validate_path(path):
    url = parse.urlsplit(path)
    decoded = parse.unquote(url.path)
    if (url.scheme or url.netloc or url.fragment or not decoded.startswith('/api2/json/') or
            '..' in decoded.split('/') or '\\' in decoded or '\x00' in decoded or
            any(ord(c) < 32 for c in decoded) or '//' in decoded):
        raise CompatError('Invalid fixed-upstream API path.')
    return url


def rewrite(method, path, content_type, body, config):
    url = validate_path(path)
    if method not in ('GET', 'POST', 'PUT', 'DELETE'):
        raise CompatError('Unsupported provider API method.')
    match = re.fullmatch(r'/api2/json/nodes/(proxmox[1-6])/lxc(?:/([0-9]+)/config)?', url.path)
    if not match or match[1] not in config['nodes'] or method not in ('POST', 'PUT'):
        return body, None
    if match[2] is None and method != 'POST': return body, None
    if content_type.split(';')[0].strip().lower() != 'application/x-www-form-urlencoded':
        raise CompatError('CT schema compatibility requires form-encoded provider requests.')
    try:
        text = body.decode('ascii')
        if re.search(r'%(?![a-fA-F0-9]{2})', text): raise ValueError('invalid encoding')
        pairs = parse.parse_qsl(text, keep_blank_values=True, strict_parsing=True,
                               encoding='utf-8', errors='strict') if text else []
    except (ValueError, UnicodeError) as exc:
        raise CompatError('Invalid CT provider form encoding.') from exc
    changed = False
    output = []
    for key, value in pairs:
        if re.fullmatch(r'net[0-9]+', key):
            tokens = value.split(',')
            retained = [t for t in tokens if t not in ('host-managed=0', 'host-managed=false')]
            changed |= retained != tokens
            value = ','.join(retained)
        output.append((key, value))
    return (parse.urlencode(output).encode(), match[1]) if changed else (body, None)


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl): return None


class Adapter:
    def __init__(self, path=None):
        self.path = Path(path or config_path())
        self.config = load_config(self.path)
        self.identity = fingerprint(self.path)
        self.origin = parse.urlsplit(self.config['upstream'])
        self.gateway_origins = []
        if self.origin.hostname in ('10.0.0.101', '10.0.0.104', '10.0.0.106') and self.origin.port == 8006:
            # Fixed administrator-owned cluster gateways, never caller URLs.
            self.gateway_origins = [self.origin] + [parse.urlsplit(f'https://10.0.0.{n}:8006')
                for n in (104, 106, 101) if self.origin.hostname != f'10.0.0.{n}']
        self.gateway_lock = threading.RLock()
        self.gateway_verified_until = 0
        context = ssl._create_unverified_context() if self.config.get('insecure_tls') is True else ssl.create_default_context()
        self.opener = request.build_opener(NoRedirect(), request.HTTPSHandler(context=context), request.ProxyHandler({}))
        self.slots = threading.BoundedSemaphore(self.config.get('concurrency', 4))

    def assert_unchanged(self):
        if fingerprint(self.path) != self.identity:
            raise CompatError('Compatibility code/settings changed; restart and restage reviewed plans.')

    def _request(self, method, path, headers, body=None, *, origin=None):
        validate_path(path)
        origin = origin or self.origin
        url = parse.urlunsplit((origin.scheme, origin.netloc, path.split('?')[0], parse.urlsplit(path).query, ''))
        req = request.Request(url, data=body, method=method, headers=headers)
        try:
            response = self.opener.open(req, timeout=self.config.get('timeout_seconds', 20))
        except error.HTTPError as exc:
            if 300 <= exc.code < 400: raise CompatError('Upstream redirects are refused.') from exc
            response = exc
        with response:
            data = response.read(32 * 1024 * 1024 + 1)
            if len(data) > 32 * 1024 * 1024: raise CompatError('Upstream response exceeds bound.')
            return response.code, {name.title(): value for name, value in response.headers.items()}, data

    def _select_gateway(self, headers, *, exclude=None):
        with self.gateway_lock:
            if self.origin != exclude and time.monotonic() < self.gateway_verified_until:
                return self.origin
            candidates = [self.origin] + [origin for origin in self.gateway_origins if origin != self.origin]
            for origin in candidates:
                if origin == exclude:
                    continue
                try:
                    status, _, raw = self._request('GET', '/api2/json/cluster/status',
                        {'Authorization': headers['Authorization']}, origin=origin)
                    if status in (401, 403):
                        raise CompatError('Scoped cluster credentials were denied; gateway failover refused.')
                    if status != 200:
                        continue
                    rows = json.loads(raw).get('data', [])
                    number = int(origin.hostname.rsplit('.', 1)[1]) - 100
                    if (not isinstance(rows, list) or
                            not any(row.get('type') == 'cluster' and row.get('name') == 'proxmoxcluster'
                                    and row.get('quorate') == 1 for row in rows) or
                            {row.get('name') for row in rows if row.get('type') == 'node'} !=
                            {f'proxmox{i}' for i in range(1, 7)} or
                            not any(row.get('type') == 'node' and row.get('name') == f'proxmox{number}'
                                    and row.get('online') == 1 and row.get('local') == 1 for row in rows)):
                        continue
                except CompatError:
                    raise
                except (error.URLError, OSError, TimeoutError, ValueError):
                    continue
                self.origin = origin
                self.gateway_verified_until = time.monotonic() + 10
                return origin
            raise CompatError('No verified quorate cluster API gateway is reachable; no write submitted.')

    def _gateway_request(self, method, path, headers, body=None):
        if not self.gateway_origins:
            return self._request(method, path, headers, body)
        origin = self._select_gateway(headers)
        try:
            return self._request(method, path, headers, body, origin=origin)
        except (error.URLError, OSError, TimeoutError) as exc:
            if method != 'GET':
                raise CompatError('Cluster API write outcome is uncertain; reconcile its provider task. The write was not replayed.') from exc
            origin = self._select_gateway(headers, exclude=origin)
            return self._request(method, path, headers, body, origin=origin)

    def forward(self, method, path, headers, body):
        self.assert_unchanged()
        auth = headers.get('Authorization', '')
        if not auth.startswith('PVEAPIToken=') or '\r' in auth or '\n' in auth:
            raise CompatError('Scoped caller API token is required.')
        forwarded = {'Authorization': auth, 'Accept': 'application/json'}
        if headers.get('Content-Type'): forwarded['Content-Type'] = headers['Content-Type']
        payload, node = rewrite(method, path, forwarded.get('Content-Type', ''), body, self.config)
        if node:
            status, _, raw = self._gateway_request('GET', f'/api2/json/nodes/{node}/status', {'Authorization': auth})
            data = json.loads(raw).get('data', {}) if status == 200 else {}
            version = data.get('pveversion', '')
            expected = self.config['nodes'][node]['version']
            if not re.search(r'(?:^|/)' + re.escape(expected) + r'(?:/|$)', version):
                raise CompatError('Measured target node version changed or is unavailable; compatibility refused.')
        return self._gateway_request(method, path, forwarded, payload if body or method in ('POST', 'PUT') else None)


def serve(path=None):
    adapter = Adapter(path)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def handle_one_request(self):
            self.connection.settimeout(30)
            super().handle_one_request()
        def do_request(self):
            if self.client_address[0] != '127.0.0.1': return self.send_error(403)
            if self.path == '/__crange_compat/identity' and self.command == 'GET':
                try: adapter.assert_unchanged()
                except Exception: return self.send_error(409, 'Compatibility identity changed; restage')
                payload = json.dumps(adapter.identity).encode()
                self.send_response(200); self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload))); self.end_headers(); self.wfile.write(payload); return
            if self.path == '/__crange_compat/status' and self.command == 'GET':
                try: adapter.assert_unchanged()
                except Exception: return self.send_error(409, 'Compatibility identity changed; restage')
                with adapter.gateway_lock:
                    payload = json.dumps({'active_upstream': adapter.origin.geturl(),
                        'recently_verified': time.monotonic() < adapter.gateway_verified_until}).encode()
                self.send_response(200); self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload))); self.end_headers(); self.wfile.write(payload); return
            try:
                if self.headers.get('Transfer-Encoding'): raise CompatError('Chunked provider requests are unsupported.')
                lengths = self.headers.get_all('Content-Length', [])
                if len(lengths) > 1: raise CompatError('Ambiguous request length.')
                length = int(lengths[0]) if lengths else 0
                if not 0 <= length <= 8 * 1024 * 1024: raise CompatError('Provider body exceeds bound.')
                body = self.rfile.read(length)
                if len(body) != length: raise CompatError('Incomplete provider body.')
                status, headers, payload = adapter.forward(self.command, self.path, self.headers, body)
                self.send_response(status)
                for name in ('Content-Type', 'Content-Encoding'):
                    if name in headers: self.send_header(name, headers[name])
                self.send_header('Content-Length', str(len(payload))); self.end_headers(); self.wfile.write(payload)
            except CompatError as exc:
                # Only fixed validation messages, never request bodies or credentials.
                print(f'proxmox-compat: {exc}', flush=True)
                self.send_error(400, str(exc))
            except ValueError:
                self.send_error(400, 'Invalid compatibility request framing')
            except Exception: self.send_error(502, 'Proxmox upstream request failed')
        do_GET = do_POST = do_PUT = do_DELETE = do_request
    class BoundedServer(ThreadingHTTPServer):
        request_queue_size = 32
        def process_request(self, connection, address):
            # Terraform refreshes several resources concurrently. Keep work
            # bounded while queueing its short API requests instead of rejecting
            # ordinary refresh bursts with a spurious provider-visible 503.
            if not adapter.slots.acquire(timeout=5):
                try: connection.sendall(b'HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n')
                finally: self.shutdown_request(connection)
                return
            try: super().process_request(connection, address)
            except Exception:
                adapter.slots.release()
                raise
        def process_request_thread(self, connection, address):
            try: super().process_request_thread(connection, address)
            finally: adapter.slots.release()
    server = BoundedServer(('127.0.0.1', adapter.config['port']), Handler)
    server.daemon_threads = True
    certificate, key = tls_paths(adapter.config, adapter.path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(certificate), str(key))
    # Perform handshakes inside bounded request threads, after their timeout
    # is installed, so a stalled client cannot block the accept loop.
    server.socket = context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
    server.serve_forever()


if __name__ == '__main__':
    import argparse
    import sys
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--fingerprint', action='store_true')
    args = parser.parse_args()
    try:
        print(json.dumps(fingerprint(args.config))) if args.fingerprint else serve(args.config)
    except Exception as exc:
        print('crange: Proxmox compatibility adapter failed: ' + (str(exc) if isinstance(exc, CompatError) else type(exc).__name__), file=sys.stderr)
        sys.exit(1)
