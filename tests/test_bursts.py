"""Local-only burst and Windows telemetry regression tests. No real phone/routes."""
import asyncio
import concurrent.futures
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from test_capacity_v3 import PhoneFixture, BASE, gateway, legacy, p, win


async def socks(address, payload=b'burst'):
    reader, writer = await asyncio.open_connection(*address)
    try:
        writer.write(b'\x05\x01\x02')
        await writer.drain()
        assert await reader.readexactly(2) == b'\x05\x02'
        secret = b'test-password'
        writer.write(b'\x01\x03usb' + bytes([len(secret)]) + secret)
        await writer.drain()
        assert await reader.readexactly(2) == b'\x01\x00'
        writer.write(b'\x05\x01\x00' + p.pack_address('example.com', 443))
        await writer.drain()
        response = await reader.readexactly(10)
        if response[:3] != b'\x05\x00\x00':
            raise RuntimeError('Burst request rejected')
        writer.write(payload)
        await writer.drain()
        assert await reader.readexactly(len(payload)) == payload
        return reader, writer
    except BaseException:
        writer.close()
        await writer.wait_closed()
        raise


class BurstTests(unittest.TestCase):
    def server(self, **options):
        phone = PhoneFixture()
        self.addCleanup(phone.close)
        server = gateway.Gateway({}, 'test-password', opener=phone.operation, **options)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.stop)
        return server, phone

    def test_600_simultaneous_connection_attempts(self):
        server, phone = self.server()
        async def exercise():
            release = asyncio.Event()
            async def one(index):
                _, writer = await socks(server.server_address, f'tab-{index}'.encode())
                try:
                    await release.wait()
                finally:
                    writer.close()
                    await writer.wait_closed()
            tasks = [asyncio.create_task(one(i)) for i in range(600)]
            try:
                deadline = time.monotonic() + 12
                while server.snapshot()['queued'] < 100 and time.monotonic() < deadline:
                    await asyncio.sleep(.02)
                self.assertGreaterEqual(server.snapshot()['queued'], 100)
                control, info = await asyncio.to_thread(phone.operation, 'ping')
                control.close()
                self.assertLessEqual(info['active'], 400)
            finally:
                release.set()
                results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 30)
            errors = [type(result).__name__ + ': ' + str(result) for result in results if isinstance(result, BaseException)]
            self.assertEqual(errors, [])
        asyncio.run(exercise())
        stats = server.snapshot()
        self.assertEqual(stats['rejected'], 0)
        self.assertLessEqual(stats['peak_active'], 384)
        print('BURST PASS: 600 concurrent attempts; peak waiting (admission + setup)', stats['peak_waiting'], '; zero rejections', flush=True)

    def test_idle_web_capacity_is_reclaimed_before_queue_timeout(self):
        server, _ = self.server(limit=2, web_idle=.4, queue_timeout=3)
        clients = [legacy.socks_connect(server.server_address, 'test-password', 1, 'example.com', 443)[0] for _ in range(2)]
        try:
            newest, _ = legacy.socks_connect(server.server_address, 'test-password', 1, 'example.com', 443)
            with newest:
                newest.sendall(b'new-tab')
                self.assertEqual(p.exact(newest, 7), b'new-tab')
            self.assertGreater(server.snapshot()['idle_reaped'], 0)
            self.assertEqual(server.snapshot()['rejected'], 0)
        finally:
            for client in clients:
                client.close()

    def test_active_web_transfer_not_reclaimed(self):
        server, _ = self.server(limit=1, web_idle=.4, queue_timeout=1)
        client, _ = legacy.socks_connect(server.server_address, 'test-password', 1, 'example.com', 443)
        with client, concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            queued = pool.submit(legacy.socks_connect, server.server_address, 'test-password', 1, 'example.com', 443)
            deadline = time.monotonic() + 1.4
            while time.monotonic() < deadline:
                client.sendall(b'alive')
                self.assertEqual(p.exact(client, 5), b'alive')
                time.sleep(.1)
            with self.assertRaises(RuntimeError):
                queued.result(3)
            self.assertEqual(server.snapshot()['idle_reaped'], 0)
            self.assertEqual(server.snapshot()['rejection_reasons']['queue_timeout'], 1)

    def test_abandoned_waiter_releases_queue(self):
        server, _ = self.server(limit=1, queue_timeout=10)
        self.assertTrue(server.acquire_slot(1))
        left, right = socket.socketpair()
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(server.acquire_slot, 1, left)
                time.sleep(.05)
                right.close()
                self.assertFalse(pending.result(2))
            self.assertEqual(server.snapshot()['queued'], 0)
            self.assertEqual(server.snapshot()['cancelled_waiters'], 1)
        finally:
            left.close()
            right.close()
            server.release_slot(1)

    def test_bounded_queue_reports_overflow_without_exceeding_capacity(self):
        server, _ = self.server(limit=1, queue_limit=1, queue_timeout=3)
        self.assertTrue(server.acquire_slot(1))
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(server.acquire_slot, 1)
            deadline = time.monotonic() + 1
            while not server.snapshot()['queued'] and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertFalse(server.acquire_slot(1))
            self.assertEqual(server.snapshot()['rejection_reasons']['queue_full'], 1)
            server.release_slot(1)
            self.assertTrue(pending.result(2))
            server.release_slot(1)
        self.assertEqual(server.snapshot()['peak_active'], 1)

    def test_generic_udp_and_unanswered_dns_are_not_parked(self):
        for destination, query, response in (
                (443, b'generic-udp', b'generic-reply'),
                (53, b'\x00\x01\x01\0\0\x01\0\0\0\0\0\0',
                 b'\x00\x02\x81\0\0\x01\0\0\0\0\0\0')):
            with self.subTest(port=destination):
                left, right = socket.socketpair()
                right.settimeout(2)
                server = gateway.Gateway({}, 'test-password', opener=lambda *_: (left, {}), dns_idle=.05)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                try:
                    control, bound = legacy.socks_connect(server.server_address, 'test-password', 3, '0.0.0.0', 0)
                    with control, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                        udp.settimeout(2)
                        header = b'\0\0\0' + p.pack_address('1.1.1.1', destination)
                        udp.sendto(header + query, bound)
                        self.assertEqual(p.read_frame(right), header + query)
                        p.write_frame(right, header + response)
                        udp.recvfrom(65536)
                        time.sleep(.35)
                        self.assertEqual(server.snapshot()['active'], 1)
                        self.assertEqual(server.snapshot()['dns_parked'], 0)
                finally:
                    server.stop()
                    right.close()

    def test_dns_phone_slot_parks_and_reopens_without_closing_association(self):
        calls = []
        def opener(*args):
            calls.append(args)
            left, right = socket.socketpair()
            def echo_dns():
                with right:
                    try:
                        while True:
                            data = p.read_frame(right)
                            host, port, payload = p.unpack_datagram(data)
                            response = payload[:2] + bytes([payload[2] | 128]) + payload[3:]
                            p.write_frame(right, b'\0\0\0' + p.pack_address(host, port) + response)
                    except (OSError, EOFError):
                        pass
            threading.Thread(target=echo_dns, daemon=True).start()
            return left, {}
        server = gateway.Gateway({}, 'test-password', opener=opener, dns_idle=.1)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.stop)
        control, bound = legacy.socks_connect(server.server_address, 'test-password', 3, '0.0.0.0', 0)
        with control, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            udp.settimeout(3)
            self.assertEqual(len(calls), 0)
            for identifier in (1, 2):
                query = identifier.to_bytes(2, 'big') + b'\x01\0\0\x01\0\0\0\0\0\0'
                udp.sendto(b'\0\0\0' + p.pack_address('1.1.1.1', 53) + query, bound)
                reply = p.unpack_datagram(udp.recvfrom(65536)[0])[2]
                self.assertEqual(reply[:2], query[:2])
                deadline = time.monotonic() + 2
                while server.snapshot()['active'] and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertEqual(server.snapshot()['active'], 0)
                self.assertEqual(server.snapshot()['udp_associations'], 1)
            self.assertEqual(len(calls), 2)
            self.assertEqual(server.snapshot()['dns_parked'], 2)


class StatusTests(unittest.TestCase):
    def test_real_windows_reader_lock_is_nonfatal_and_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'status.json'
            path.write_text('{"state":"old"}')
            publisher = win.StatusPublisher(path)
            with path.open('rb') as reader:
                self.assertFalse(publisher.publish({'state': 'connected'}))
                self.assertEqual(json.loads(reader.read())['state'], 'old')
            self.assertTrue(publisher.publish({'state': 'connected'}))
            self.assertEqual(win.read_status(path)['status_write_failures'], 1)
            self.assertEqual(win.read_status(path)['state'], 'connected')

    def test_retries_handle_transient_sharing_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'status.json'
            original = win.os.replace
            attempts = []
            def transient(*args):
                attempts.append(1)
                if len(attempts) < 3:
                    raise PermissionError('Fixture sharing conflict')
                original(*args)
            with patch.object(win.os, 'replace', side_effect=transient):
                win.atomic_json(path, {'state': 'connected'})
            self.assertEqual(len(attempts), 3)
            self.assertEqual(win.read_status(path)['state'], 'connected')

    def test_permanent_status_failure_is_nonfatal(self):
        publisher = win.StatusPublisher('unused')
        with patch.object(win, 'atomic_json', side_effect=PermissionError('Fixture denial')):
            for _ in range(5):
                self.assertFalse(publisher.publish({'state': 'connected'}))
        self.assertEqual(publisher.failed_updates, 5)


if __name__ == '__main__':
    unittest.main(verbosity=2)
