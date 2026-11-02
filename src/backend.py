"""Explicit-connect worker. GUI launch and imports never call worker()."""
import ctypes
import hashlib
import json
import os
import re
from pathlib import Path
import secrets
import socket
import ssl
import subprocess
import sys
import threading
import time
import uuid
import pairing

import gateway
import pc_gateway as legacy
import win_support as win
from secret_pipe import SecretPipe, tunnel_config
from health import Health

ROOT = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
RESOURCES = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
DATA = Path(os.environ.get('LOCALAPPDATA', ROOT)) / 'iPhone USB Internet'
HASHES = {'tun2socks.exe': '076b3c3d6a372bae3f49f2b415a4105f70c30a3ed3caaed7979390e649892559',
          'wintun.dll': 'e5da8447dc2c320edc0fc52fa01885c103de8c118481f683643cacc3220dafce'}


def session_dir(session):
    parsed = uuid.UUID(session)
    if str(parsed) != session:
        raise ValueError('Invalid session identifier')
    return DATA / 'sessions' / session


def load_pairing():
    return pairing.load(ROOT, DATA)


def verify_bundle():
    for name, expected in HASHES.items():
        if hashlib.sha256((RESOURCES / 'vendor' / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError('Integrity check failed for ' + name + '. Do not connect with this copy.')


def error_detail(error):
    """Keep useful failure details, but never expose session/pairing secrets."""
    text = str(error).strip() or type(error).__name__
    text = re.sub(r'socks5://[^\s]+', 'socks5://[redacted]', text, flags=re.I)
    text = re.sub(r'(?i)\b[0-9a-f]{48,64}\b', '[redacted]', text)
    return text[:1600]


def final_status(failure, cleanup_errors):
    if cleanup_errors:
        original = ('Connection error: ' + failure + '\n\n') if failure else ''
        return 'cleanup-needed', original + 'Cleanup warning: ' + '\n'.join(cleanup_errors)
    if failure:
        return 'error', 'Connection error: ' + failure + '\nThe connection worker has stopped.'
    return 'stopped', 'Disconnected. Your existing Wi-Fi/Ethernet settings were not changed.'


def phone_info(config):
    connection, result = legacy.operation(config, 'ping')
    connection.close()
    if result.get('version') != 3 or 'async-relay' not in result.get('capabilities', []):
        raise RuntimeError('The phone needs an update. Open Phone update after disconnecting the old app.')
    limit = result.get('max_sessions')
    if type(limit) is not int or limit < 32:
        raise RuntimeError('Phone capacity is insufficient for this build.')
    return result


def negotiated_capacity(info):
    return min(384, info['max_sessions'] - 16)


def check_phone(config=None, return_info=False):
    config = config or load_pairing()
    info = phone_info(config)
    stream, _ = legacy.operation(config, 'tcp', '1.1.1.1', 443)
    try:
        with ssl.create_default_context().wrap_socket(stream, server_hostname='one.one.one.one') as tls:
            tls.sendall(b'HEAD / HTTP/1.1\r\nHost: one.one.one.one\r\nConnection: close\r\n\r\n')
            if not tls.recv(512).startswith(b'HTTP/1.'):
                raise RuntimeError('The phone did not return a valid web response.')
    finally:
        stream.close()
    if return_info:
        return info
    return ('Phone ready. USB + HTTPS passed. Capacity: ' + str(negotiated_capacity(info)) +
            ' sessions. PC internet is unchanged.')


def network(action, alias=None, guid=None, job=None, cancelled=None, timeout=45):
    powershell = str(Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe')
    args = [powershell, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
            '-File', str(RESOURCES / 'network.ps1'), '-Action', action]
    if alias:
        args += ['-Alias', alias]
    if guid:
        args += ['-ExpectedGuid', guid]
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        if job:
            job.add(process)
        deadline = time.monotonic() + timeout
        while True:
            if cancelled and cancelled():
                raise RuntimeError('Connection cancelled.')
            if time.monotonic() > deadline:
                raise RuntimeError('Windows network operation timed out.')
            try:
                output, error = process.communicate(timeout=.2)
                break
            except subprocess.TimeoutExpired:
                pass
        if process.returncode:
            # This script contains no secrets; still avoid dumping host configuration.
            raise RuntimeError('Windows ' + action.lower() + ' step failed. ' + error.decode(errors='replace')[:750])
        return json.loads(output.decode('utf-8-sig')) if output.strip() else {}
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


def worker(session):
    directory = session_dir(session)
    status_path = directory / 'status.json'
    status_publisher = win.StatusPublisher(status_path)
    stop_path = directory / 'stop'
    parent = None
    lock = None
    job = None
    server = None
    tunnel = None
    pipe = None
    health = Health()
    alias = 'iPhoneUSB-' + session.replace('-', '')[:12]
    guid = None
    failure = None
    started = time.monotonic()
    cleanup_errors = []

    def report(state, message):
        status_publisher.publish(dict(state=state, message=message, elapsed=int(time.monotonic()-started),
                                         updated_at=time.time(),
                                         stats=server.snapshot() if server else {},
                                         health=health.snapshot(),
                                         connection_error=failure, cleanup_errors=list(cleanup_errors)))

    def cancelled():
        return stop_path.exists() or not win.alive(parent)

    try:
        if not win.admin():
            raise RuntimeError('Connect requires Windows administrator approval.')
        request = json.loads(win.protect((directory / 'request.dpapi').read_bytes(), decrypt=True))
        if request.get('session') != session or request.get('consent') != 'connect-with-normal-fallback':
            raise RuntimeError('Missing explicit connection approval.')
        if abs(time.time() - request['created']) > 180:
            raise RuntimeError('Connection approval expired. Click Connect again.')
        parent = win.watch_process(int(request['parent_pid']))
        # Global mutex prevents two users/workers routing the PC simultaneously.
        win.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.wintypes.BOOL, ctypes.wintypes.LPCWSTR]
        win.kernel.CreateMutexW.restype = ctypes.wintypes.HANDLE
        lock = win.kernel.CreateMutexW(None, False, 'Global\\iPhoneUSBInternet.Connection.v1')
        if not lock or ctypes.get_last_error() == 183:
            raise RuntimeError('Another phone connection is running. Disconnect it first.')
        report('preparing', 'Checking phone, app integrity, and network conflicts...')
        verify_bundle()
        config = load_pairing()
        if cancelled():
            raise RuntimeError('Connection cancelled.')
        info = check_phone(config, return_info=True)
        health.record('usb', True)
        health.record('internet', True)
        job = win.Job()
        if network('Inspect', job=job, cancelled=cancelled).get('conflict'):
            raise RuntimeError('A VPN, phone test, or conflicting adapter/route is active. Disconnect it first; no routes were changed.')
        if cancelled():
            raise RuntimeError('Connection cancelled.')
        password = secrets.token_hex(24)
        server = gateway.Gateway(config, password, limit=negotiated_capacity(info))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        report('preparing', 'Creating the temporary phone network adapter...')
        pipe = SecretPipe()
        tunnel = subprocess.Popen([str(RESOURCES / 'vendor/tun2socks.exe'), '--config', pipe.name],
            cwd=RESOURCES / 'vendor', stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW)
        job.add(tunnel)
        pipe.deliver(tunnel.pid, tunnel_config(alias, server.server_address[1], password))
        # Wait for device creation; Configure checks the exact name and Wintun type.
        for _ in range(20):
            if cancelled() or tunnel.poll() is not None:
                raise RuntimeError('Connection stopped before the adapter was configured.')
            time.sleep(.2)
        result = network('Configure', alias, job=job, cancelled=cancelled)
        guid = result['guid']
        report('verifying', 'Checking the new phone route before marking it connected...')
        network('Verify', alias, guid, job, cancelled)
        health_stop = threading.Event()

        def health_loop():
            while not health_stop.wait(5):
                try:
                    probe_started = time.monotonic()
                    phone_info(config)
                    health.record('usb', True, (time.monotonic() - probe_started) * 1000)
                except (OSError, ValueError, RuntimeError, EOFError):
                    health.record('usb', False)

        def internet_loop():
            # Small certificate-validated HTTPS check through the phone, not
            # through the host default route. Does not claim phone-only routing.
            while not health_stop.wait(30):
                try:
                    check_phone(config)
                    health.record('internet', True)
                except (OSError, ValueError, RuntimeError, EOFError):
                    health.record('internet', False)

        threading.Thread(target=health_loop, daemon=True).start()
        threading.Thread(target=internet_loop, daemon=True).start()
        try:
            while not cancelled():
                if tunnel.poll() is not None:
                    raise RuntimeError('The tunnel stopped. Removing the phone route.')
                usb_health = health.snapshot()['usb']
                if usb_health['failures'] >= 2 or usb_health['age_seconds'] > 50:
                    raise RuntimeError('The phone stopped responding. Keep iSH visible and the phone unlocked, then reconnect.')
                report('connected', 'Phone route is active. Keep iSH visible and your iPhone unlocked.')
                time.sleep(.5)
        finally:
            health_stop.set()
    except Exception as error:
        failure = error_detail(error)
    finally:
        if pipe:
            pipe.close()
        try:
            report('disconnecting', 'Removing the temporary phone route...')
        except OSError:
            pass
        # Also covers partial Configure failure, not just fully successful setup.
        if tunnel is not None:
            try:
                network('Cleanup', alias, guid, job, timeout=20)
            except Exception as error:
                cleanup_errors.append('Route removal: ' + error_detail(error))
        if server:
            try:
                server.stop()
            except Exception as error:
                cleanup_errors.append('USB gateway shutdown: ' + error_detail(error))
        if job:
            try:
                job.close()  # Still attempted if route or gateway cleanup fails.
            except Exception as error:
                cleanup_errors.append('Tunnel job shutdown: ' + error_detail(error))
        if tunnel:
            try:
                tunnel.wait(5)
            except subprocess.TimeoutExpired:
                try:
                    tunnel.kill()
                    tunnel.wait(5)
                except Exception as error:
                    cleanup_errors.append('Tunnel process shutdown: ' + error_detail(error))
            except Exception as error:
                cleanup_errors.append('Tunnel process check: ' + error_detail(error))
        win.close(parent)
        win.close(lock)
        state, message = final_status(failure, cleanup_errors)
        try:
            report(state, message)
        except OSError:
            pass
