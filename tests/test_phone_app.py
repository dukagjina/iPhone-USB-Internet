"""Never activates host networking. All lifecycle network operations are mocked."""
import ctypes
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import MagicMock, patch

BASE = Path(__file__).resolve().parent.parent
for directory in ('iphone-usb-app', 'iphone-usb-network', 'iphone-usb-test'):
    sys.path.insert(0, str(BASE / 'src'))
import app
import backend as b
import gateway as g
import phone_network as p
import pc_gateway as legacy
import win_support as win


class GatewayTests(unittest.TestCase):
    def create_gateway(self, opener, limit=88):
        server = g.Gateway({}, 'secret', opener=opener, limit=limit, queue_timeout=.5)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.stop)
        return server

    def test_no_auth_never_opens_phone(self):
        server = self.create_gateway(lambda *_: self.fail('Unauthenticated phone access'))
        with socket.create_connection(server.server_address) as client:
            client.sendall(b'\x05\x01\x00')
            self.assertEqual(p.exact(client, 2), b'\x05\xff')
        with self.assertRaises(RuntimeError):
            legacy.socks_connect(server.server_address, 'wrong', 1, '1.1.1.1', 443)

    def test_tcp_half_close_and_counts(self):
        peer, upstream = socket.socketpair()
        peer.settimeout(3)
        self.addCleanup(peer.close)
        server = self.create_gateway(lambda *_: (upstream, {}))
        client, _ = legacy.socks_connect(server.server_address, 'secret', 1, 'example.com', 443)
        with client:
            client.sendall(b'hello')
            client.shutdown(socket.SHUT_WR)
            self.assertEqual(p.exact(peer, 5), b'hello')
            self.assertEqual(peer.recv(1), b'')
            peer.sendall(b'world')
            peer.shutdown(socket.SHUT_WR)
            self.assertEqual(p.exact(client, 5), b'world')
        time.sleep(.1)
        self.assertEqual(server.snapshot()['upload'], 5)
        self.assertEqual(server.snapshot()['download'], 5)

    def test_udp_association_closes(self):
        peer, upstream = socket.socketpair()
        peer.settimeout(3)
        self.addCleanup(peer.close)
        server = self.create_gateway(lambda *_: (upstream, {}))
        control, bound = legacy.socks_connect(server.server_address, 'secret', 3, '0.0.0.0', 0)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            udp.settimeout(3)
            query = b'\0\0\0' + p.pack_address('1.1.1.1', 53) + b'query'
            udp.sendto(query, bound)
            self.assertEqual(p.read_frame(peer), query)
            p.write_frame(peer, query)
            self.assertEqual(udp.recvfrom(1024)[0], query)
        control.close()
        self.assertEqual(peer.recv(1), b'')

    def test_stop_closes_all_streams(self):
        peer, upstream = socket.socketpair()
        peer.settimeout(3)
        self.addCleanup(peer.close)
        server = self.create_gateway(lambda *_: (upstream, {}))
        client, _ = legacy.socks_connect(server.server_address, 'secret', 1, 'example.com', 443)
        with client:
            server.stop()
            self.assertEqual(client.recv(1), b'')
            self.assertEqual(peer.recv(1), b'')

    def test_capacity_rejection_is_counted(self):
        peer, upstream = socket.socketpair()
        self.addCleanup(peer.close)
        server = self.create_gateway(lambda *_: (upstream, {}), limit=1)
        client, _ = legacy.socks_connect(server.server_address, 'secret', 1, 'example.com', 443)
        with client:
            with self.assertRaises((EOFError, OSError, RuntimeError)):
                legacy.socks_connect(server.server_address, 'secret', 1, 'example.com', 443)
            self.assertEqual(server.snapshot()['rejected'], 1)

    def test_usb_failure_never_falls_back_in_gateway(self):
        def fail(*args):
            raise OSError('Disconnected USB')
        server = self.create_gateway(fail)
        with self.assertRaises(RuntimeError):
            legacy.socks_connect(server.server_address, 'secret', 1, '1.1.1.1', 443)


class WindowsTests(unittest.TestCase):
    def test_error_details_redact_credentials(self):
        detail = b.error_detail(RuntimeError('Failed socks5://usb:secret@127.0.0.1:1234 ' + 'a' * 64))
        self.assertNotIn('secret', detail)
        self.assertNotIn('a' * 64, detail)
        self.assertIn('Failed', detail)

    def test_actual_powershell_identity_helper_with_offline_fixtures(self):
        powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        result = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                                 '-File', str(BASE / 'tests/test_adapter_identity.ps1')],
                                capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        self.assertIn(b'9 adapter identity tests passed', result.stdout)

    def test_dpapi_round_trip(self):
        content = b'test-data-not-a-real-secret'
        encrypted = win.protect(content)
        self.assertNotIn(content, encrypted)
        self.assertEqual(win.protect(encrypted, decrypt=True), content)

    def test_kill_on_close_job_with_harmless_child(self):
        job = win.Job()
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            job.add(child)
            self.assertIsNone(child.poll())
            job.close()
            child.wait(5)
            self.assertIsNotNone(child.returncode)
        finally:
            job.close()
            if child.poll() is None:
                child.kill()

    def test_session_traversal_rejected(self):
        for value in ('../bad', 'C:\\Windows', 'not-a-uuid'):
            with self.assertRaises(ValueError):
                b.session_dir(value)

    def test_script_scopes_all_mutations(self):
        text = (BASE / 'src/network.ps1').read_text()
        self.assertNotIn('ResetServerAddresses', text)
        self.assertNotIn('Disable-NetAdapter', text)
        self.assertNotIn('New-NetFirewallRule', text)
        self.assertNotIn('PersistentStore', text)
        for line in text.splitlines():
            if line.startswith(('Set-Net', 'New-Net', 'Set-DnsClient')):
                self.assertIn('-InterfaceIndex $index', line)
        self.assertIn('ExpectedGuid', text)
        self.assertIn('ActiveStore', text)

    def test_gui_cancel_never_elevates(self):
        gui = app.App.__new__(app.App)
        gui.busy = False
        gui.process = None
        gui.check_passed = True
        gui.root = MagicMock()
        gui.messagebox = MagicMock()
        gui.messagebox.askokcancel.return_value = False
        with patch.object(win, 'elevate') as elevate:
            gui.connect()
            elevate.assert_not_called()


class WorkerTests(unittest.TestCase):
    def run_worker(self, fail_action=None, cancel=False, invalid_consent=False, gateway_stop_fails=False, status_error=False):
        with tempfile.TemporaryDirectory() as temporary:
            sid = str(uuid.uuid4())
            calls = []
            job = MagicMock()
            server = MagicMock()
            server.server_address = ('127.0.0.1', 12345)
            server.snapshot.return_value = {}
            if gateway_stop_fails:
                server.stop.side_effect = RuntimeError('Injected gateway shutdown failure')
            tunnel = MagicMock()
            tunnel.poll.return_value = None
            request = dict(session=sid, consent='wrong' if invalid_consent else 'connect-with-normal-fallback',
                           created=time.time(), parent_pid=os.getpid())
            def fake_network(action, *args, **kwargs):
                calls.append(action)
                if action in (fail_action if isinstance(fail_action, tuple) else (fail_action,)):
                    raise RuntimeError('Injected ' + action + ' failure')
                if action == 'Verify' and not status_error:
                    (b.session_dir(sid) / 'stop').touch()
                return {'conflict': False, 'guid': str(uuid.uuid4())}
            original_publish = win.atomic_json
            status_attempts = []
            def publish(path, data):
                if status_error and data['state'] == 'connected':
                    status_attempts.append(1)
                    if len(status_attempts) == 1:
                        raise PermissionError('Injected Windows sharing conflict')
                    (b.session_dir(sid) / 'stop').touch()
                return original_publish(path, data)
            with patch.object(b, 'DATA', Path(temporary)), \
                 patch.object(win, 'admin', return_value=True), \
                 patch.object(b, 'verify_bundle'), patch.object(b, 'load_pairing', return_value={}), \
                 patch.object(b, 'check_phone', return_value={'max_sessions': 400}), patch.object(b, 'network', side_effect=fake_network), \
                 patch.object(win, 'Job', return_value=job), \
                 patch.object(b, 'SecretPipe'), \
                 patch.object(win, 'atomic_json', side_effect=publish), \
                 patch.object(g, 'Gateway', return_value=server), \
                 patch.object(b.subprocess, 'Popen', return_value=tunnel), \
                 patch.object(b.time, 'sleep'):
                directory = b.session_dir(sid)
                directory.mkdir(parents=True)
                (directory / 'request.dpapi').write_bytes(win.protect(json.dumps(request).encode()))
                if cancel:
                    (directory / 'stop').touch()
                b.worker(sid)
                status = json.loads((directory / 'status.json').read_text())
            return calls, status, job, server

    def test_cancel_before_connect_never_mutates(self):
        calls, status, _, _ = self.run_worker(cancel=True)
        self.assertEqual(calls, [])
        self.assertEqual(status['state'], 'error')

    def test_missing_consent_never_mutates(self):
        calls, status, _, _ = self.run_worker(invalid_consent=True)
        self.assertEqual(calls, [])
        self.assertEqual(status['state'], 'error')

    def test_partial_configure_failure_cleans_up(self):
        calls, status, job, server = self.run_worker(fail_action='Configure')
        self.assertEqual(calls, ['Inspect', 'Configure', 'Cleanup'])
        self.assertEqual(status['state'], 'error')
        job.close.assert_called_once()
        server.stop.assert_called_once()

    def test_failed_verification_cleans_up(self):
        calls, status, job, _ = self.run_worker(fail_action='Verify')
        self.assertEqual(calls, ['Inspect', 'Configure', 'Verify', 'Cleanup'])
        self.assertEqual(status['state'], 'error')
        job.close.assert_called_once()

    def test_successful_connect_disconnect_lifecycle(self):
        calls, status, job, _ = self.run_worker()
        self.assertEqual(calls, ['Inspect', 'Configure', 'Verify', 'Cleanup'])
        self.assertEqual(status['state'], 'stopped')
        job.close.assert_called_once()

    def test_status_failure_does_not_disconnect_worker(self):
        calls, status, job, _ = self.run_worker(status_error=True)
        self.assertEqual(calls, ['Inspect', 'Configure', 'Verify', 'Cleanup'])
        self.assertEqual(status['state'], 'stopped')
        self.assertIsNone(status['connection_error'])
        self.assertEqual(status['status_write_failures'], 1)
        job.close.assert_called_once()

    def test_cleanup_failure_not_misreported(self):
        calls, status, job, _ = self.run_worker(fail_action='Cleanup')
        self.assertEqual(status['state'], 'cleanup-needed')
        job.close.assert_called_once()

    def test_original_error_survives_cleanup_failure(self):
        calls, status, job, _ = self.run_worker(fail_action=('Configure', 'Cleanup'))
        self.assertEqual(status['state'], 'cleanup-needed')
        self.assertIn('Injected Configure failure', status['message'])
        self.assertIn('Injected Cleanup failure', status['message'])
        self.assertEqual(status['connection_error'], 'Injected Configure failure')
        self.assertTrue(status['cleanup_errors'])
        job.close.assert_called_once()

    def test_gateway_shutdown_error_does_not_skip_tunnel_shutdown(self):
        _, status, job, _ = self.run_worker(gateway_stop_fails=True)
        self.assertEqual(status['state'], 'cleanup-needed')
        self.assertIn('gateway shutdown failure', status['message'])
        job.close.assert_called_once()


if __name__ == '__main__':
    unittest.main(verbosity=2)
