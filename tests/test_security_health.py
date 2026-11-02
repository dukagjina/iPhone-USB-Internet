"""Local-only tests: no phone, no TUN, no route changes, no production key edits."""
import ast
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import struct
import unittest
from unittest.mock import patch, MagicMock

BASE = Path(__file__).resolve().parent.parent
SOURCE = BASE / 'src'
for name in ('iphone-usb-app', 'iphone-usb-network', 'iphone-usb-test'):
    sys.path.insert(0, str(BASE / 'src'))
import secret_pipe
import phone_upgrade
import credential_cleanup
import health
import win_support as win
import app


class SecurityTests(unittest.TestCase):
    def test_private_pipe_round_trip(self):
        pipe = secret_pipe.SecretPipe()
        content = b'fixture-private-configuration'
        child = subprocess.Popen([sys.executable, '-c',
            'import sys; data=open(sys.argv[1],"rb").read(); sys.stdout.buffer.write(data)', pipe.name],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            pipe.deliver(child.pid, content)
            output, errors = child.communicate(timeout=10)
            self.assertEqual(child.returncode, 0, errors)
            self.assertEqual(output, content)
        finally:
            pipe.close()
            if child.poll() is None:
                child.kill()
                child.wait()

    def test_wrong_reader_gets_no_secret(self):
        pipe = secret_pipe.SecretPipe()
        child = subprocess.Popen([sys.executable, '-c',
            'import sys; sys.stdout.buffer.write(open(sys.argv[1],"rb").read())', pipe.name],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            with self.assertRaisesRegex(RuntimeError, 'identity'):
                pipe.deliver(child.pid + 100000, b'never-delivered')
            pipe.close()
            output, _ = child.communicate(timeout=10)
            self.assertNotIn(b'never-delivered', output)
        finally:
            pipe.close()
            if child.poll() is None:
                child.kill()
                child.wait()

    def test_pipe_timeout(self):
        pipe = secret_pipe.SecretPipe()
        try:
            with self.assertRaises(TimeoutError):
                pipe.deliver(0, b'not-delivered', timeout_ms=50)
        finally:
            pipe.close()

    def test_actual_tunnel_reads_pipe_before_any_device_creation(self):
        pipe = secret_pipe.SecretPipe()
        vendor = BASE / 'vendor/tun2socks.exe'
        if not vendor.is_file():
            self.skipTest('Optional vendor binary is not present')
        child = subprocess.Popen([str(vendor), '--config', pipe.name], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            # Invalid YAML forces exit during config parsing, before engine.Start
            # (and therefore before any driver/device/network operation).
            pipe.deliver(child.pid, b'proxy: [unterminated')
            output, errors = child.communicate(timeout=10)
            self.assertNotEqual(child.returncode, 0)
            self.assertIn(b'Failed to unmarshal config', output + errors)
        finally:
            pipe.close()
            if child.poll() is None:
                child.kill()
                child.wait()

    def test_config_has_no_shell_and_password_is_not_in_argv_code(self):
        config = json.loads(secret_pipe.tunnel_config('iPhoneUSB-test', 1234, 'fixture-secret'))
        self.assertEqual(config['loglevel'], 'silent')
        self.assertEqual(config['udp-timeout'], '60s')
        backend = (SOURCE / 'backend.py').read_text()
        self.assertNotIn("'--proxy'", backend)
        self.assertIn("'--config', pipe.name", backend)

    def test_installed_payload_is_standalone_and_stable_command(self):
        data = phone_upgrade.payload(SOURCE, {'token': 'b' * 64})
        ast.parse(data, feature_version=(3, 9))
        self.assertNotIn(b'import phone_network', data)
        self.assertNotIn(b'__PHONE_PAIRING_SECRET__', data)
        self.assertEqual(app.PHONE_COMMAND, 'python3 /root/phone_network.py')
        command = phone_upgrade.receiver_command(SOURCE, {'token': 'b' * 64})
        self.assertNotIn('b' * 64, command)
        self.assertIn(hashlib.sha256(data).hexdigest(), command)
        self.assertIn('os.replace', command)
        self.assertIn('.backup', command)
        compile(command[len("python3 -c '"):-1], 'receiver', 'exec')

    def run_receiver(self, root, data):
        command = phone_upgrade.receiver_command(SOURCE, {'token': 'b' * 64})
        code = command[len("python3 -c '"):-1].replace('/root/phone_network.py', (root / 'phone_network.py').as_posix())
        client, listener = MagicMock(), MagicMock()
        client.makefile.return_value = io.BytesIO(struct.pack('!I', len(data)) + data)
        listener.accept.return_value = client, None
        with patch('socket.socket', return_value=listener), patch('os.umask'), patch('builtins.print'):
            exec(compile(code, 'receiver-fixture', 'exec'), {})
        return client

    def test_receiver_replaces_default_preserves_backup_and_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'phone_network.py'
            target.write_bytes(b'# original fixture')
            data = phone_upgrade.payload(SOURCE, {'token': 'b' * 64})
            client = self.run_receiver(root, data)
            self.assertEqual(target.read_bytes(), data)
            self.assertEqual(target.with_suffix('.py.backup').read_bytes(), b'# original fixture')
            client.sendall.assert_called_once_with(b'OK')
            self.run_receiver(root, data)
            self.assertEqual(target.with_suffix('.py.backup').read_bytes(), b'# original fixture')

    def test_receiver_bad_checksum_does_not_touch_old_phone(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'phone_network.py'
            target.write_bytes(b'# original fixture')
            with self.assertRaisesRegex(AssertionError, 'Checksum'):
                self.run_receiver(root, b'# modified payload')
            self.assertEqual(target.read_bytes(), b'# original fixture')
            self.assertFalse(target.with_suffix('.py.backup').exists())

    def test_key_cleanup_recovers_bytes_and_removes_only_exact_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = root / 'pairing.json'
            data = b'{"token":"fixture-only"}'
            key.write_bytes(data)
            manifest = root / 'manifest.json'
            manifest.write_text(json.dumps([dict(path=str(key), sha256=hashlib.sha256(data).hexdigest())]))
            archive = root / 'archive'
            with patch.object(win, 'connection_active', return_value=False):
                self.assertEqual(credential_cleanup.retire(manifest, archive), [str(key)])
            self.assertFalse(key.exists())
            self.assertEqual(win.protect(next(archive.glob('*.dpapi')).read_bytes(), decrypt=True), data)

    def test_cleanup_refuses_active_connection(self):
        with patch.object(win, 'connection_active', return_value=True):
            with self.assertRaisesRegex(RuntimeError, 'Disconnect'):
                credential_cleanup.retire('unused', 'unused')

    def test_cleanup_refuses_changed_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = root / 'gateway.json'
            key.write_bytes(b'changed')
            manifest = root / 'manifest.json'
            manifest.write_text(json.dumps([dict(path=str(key), sha256='0' * 64)]))
            with patch.object(win, 'connection_active', return_value=False):
                with self.assertRaisesRegex(RuntimeError, 'changed'):
                    credential_cleanup.retire(manifest, root / 'archive')
            self.assertEqual(key.read_bytes(), b'changed')


class HealthTests(unittest.TestCase):
    def test_age_failure_and_recovery(self):
        now = [10]
        observed = health.Health(clock=lambda: now[0])
        observed.record('usb', True, 12)
        now[0] = 17
        observed.record('usb', False)
        self.assertEqual(observed.snapshot()['usb'], dict(age_seconds=7, failures=1, latency_ms=12))
        self.assertIn('delayed', health.describe(observed.snapshot(), {}))
        observed.record('usb', True, 14)
        self.assertEqual(observed.snapshot()['usb']['failures'], 0)

    def test_internet_health_not_inferred_from_usb(self):
        observed = health.Health()
        observed.record('usb', True)
        self.assertIn('HTTPS: not checked yet', health.describe(observed.snapshot(), {}))
        observed.record('internet', False)
        self.assertIn('recent check failed', health.describe(observed.snapshot(), {}))


if __name__ == '__main__':
    unittest.main(verbosity=2)
