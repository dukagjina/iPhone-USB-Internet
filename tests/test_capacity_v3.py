"""Offline v3 tests. All traffic stays on loopback; no Apple USB or routes."""
import asyncio
import ast
import concurrent.futures
import json
from pathlib import Path
import socket
import sys
import threading
import time
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parent.parent
for name in ('iphone-usb-app', 'iphone-usb-network', 'iphone-usb-test'):
    sys.path.insert(0, str(BASE / 'src'))
import gateway
import backend
import phone_network as p
import phone_network_v3 as v3
import phone_upgrade
import pc_gateway as legacy
import win_support as win

SECRET = 'b' * 64


class EchoUDP(asyncio.DatagramProtocol):
    def connection_made(self, transport):
        self.transport = transport
    def datagram_received(self, data, address):
        self.transport.sendto(data, address)


class PhoneFixture:
    """Run the actual asynchronous phone protocol in a background event loop."""
    def __init__(self, limit=400):
        self.ready = threading.Event()
        self.limit = limit
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()
        if not self.ready.wait(10):
            raise RuntimeError('Fixture startup failed')

    def run(self):
        asyncio.run(self.main())

    async def tcp_handler(self, reader, writer):
        try:
            while True:
                data = await reader.read(32768)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def connect_target(self, address):
        return await asyncio.open_connection(*address, limit=v3.CHUNK)

    async def main(self):
        self.loop = asyncio.get_running_loop()
        self.stop_event = asyncio.Event()
        echo_server = await asyncio.start_server(self.tcp_handler, '127.0.0.1', 0)
        echo_address = echo_server.sockets[0].getsockname()
        udp, _ = await self.loop.create_datagram_endpoint(EchoUDP, local_addr=('127.0.0.1', 0))
        udp_address = udp.get_extra_info('sockname')
        def resolve(host, port, kind):
            return [(socket.AF_INET, socket.SOCK_DGRAM, 0, udp_address)]
        async def connect(host, port):
            return await self.connect_target(echo_address)
        self.relay = v3.Relay(SECRET, self.limit, resolver=resolve, connector=connect)
        server = await asyncio.start_server(self.relay.accept, '127.0.0.1', 0, limit=v3.CHUNK)
        self.address = server.sockets[0].getsockname()
        self.udp_address = udp_address
        self.ready.set()
        try:
            await self.stop_event.wait()
        finally:
            server.close()
            await server.wait_closed()
            for task in list(self.relay.tasks):
                task.cancel()
            await asyncio.gather(*self.relay.tasks, return_exceptions=True)
            echo_server.close()
            await echo_server.wait_closed()
            udp.close()

    def operation(self, op, host=None, port=None, token=SECRET):
        client = socket.create_connection(self.address, timeout=10)
        try:
            p.reply(client, token=token, op=op, host=host, port=port)
            result = json.loads(p.read_frame(client, 4096))
            if not result.get('ok'):
                raise RuntimeError(result.get('error'))
            return client, result
        except BaseException:
            client.close()
            raise

    def close(self):
        self.loop.call_soon_threadsafe(self.stop_event.set)
        self.thread.join(10)
        if self.thread.is_alive():
            raise RuntimeError('Phone fixture leaked its loop')


class CapacityTests(unittest.TestCase):
    def fixture(self, limit=400):
        phone = PhoneFixture(limit)
        self.addCleanup(phone.close)
        return phone

    def server(self, phone, **kwargs):
        server = gateway.Gateway({}, 'test-password', opener=phone.operation, **kwargs)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.stop)
        return server

    def test_auth_and_version_negotiation(self):
        phone = self.fixture()
        with self.assertRaises(RuntimeError):
            phone.operation('ping', token='wrong')
        connection, info = phone.operation('ping')
        connection.close()
        self.assertEqual(info['version'], 3)
        self.assertEqual(backend.negotiated_capacity(info), 384)
        self.assertEqual(backend.negotiated_capacity({'max_sessions': 192}), 176)

    def test_phone_health_remains_available_at_data_capacity(self):
        phone = self.fixture(limit=2)
        clients = [phone.operation('tcp', 'example.com', 443)[0] for _ in range(2)]
        try:
            with self.assertRaisesRegex(RuntimeError, 'capacity'):
                phone.operation('tcp', 'example.com', 443)
            control, info = phone.operation('ping')
            control.close()
            self.assertEqual(info['active'], 2)
        finally:
            for client in clients:
                client.close()

    def test_queue_absorbs_burst_without_rejection(self):
        phone = self.fixture()
        server = self.server(phone, limit=1, queue_timeout=3)
        first, _ = legacy.socks_connect(server.server_address, 'test-password', 1, 'example.com', 443)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(legacy.socks_connect, server.server_address, 'test-password', 1, 'example.com', 443)
            deadline = time.monotonic() + 2
            while server.snapshot()['queued'] != 1 and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(server.snapshot()['queued'], 1)
            first.shutdown(socket.SHUT_WR)
            first.close()
            client, _ = pending.result(5)
            client.close()
        self.assertEqual(server.snapshot()['rejected'], 0)
        self.assertEqual(server.snapshot()['delayed'], 1)

    def test_reserved_capacity_for_each_protocol(self):
        phone = self.fixture()
        server = self.server(phone, limit=32)
        self.assertEqual(server.kind_limit, 28)
        for _ in range(28):
            self.assertTrue(server.acquire_slot(1))
        self.assertTrue(server.acquire_slot(3))
        for _ in range(28):
            server.release_slot(1)
        server.release_slot(3)

    def test_stop_wakes_waiters_without_waiting_for_timeout(self):
        phone = self.fixture()
        server = self.server(phone, limit=1, queue_timeout=20)
        self.assertTrue(server.acquire_slot(1))
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(server.acquire_slot, 1)
            time.sleep(.05)
            server.stop()
            self.assertFalse(pending.result(2))
        server.release_slot(1)

    def test_invalid_frames_rejected(self):
        phone = self.fixture()
        with socket.create_connection(phone.address, timeout=3) as client:
            client.sendall(b'\0\0\x20\0')
            self.assertFalse(json.loads(p.read_frame(client))['ok'])

    def test_udp_unsolicited_replies_dropped(self):
        inbox = v3.DatagramInbox(socket.AF_INET, {})
        inbox.datagram_received(b'unsolicited', ('1.1.1.1', 53))
        self.assertTrue(inbox.queue.empty())

    def test_udp_queue_is_bounded(self):
        inbox = v3.DatagramInbox(socket.AF_INET, {(socket.AF_INET, '1.1.1.1', 53): time.monotonic()+30})
        for _ in range(20):
            inbox.datagram_received(b'answer', ('1.1.1.1', 53))
        self.assertEqual(inbox.queue.qsize(), 8)

    def test_transfer_command_and_python39_syntax(self):
        source = BASE / 'src'
        command = phone_upgrade.receiver_command(source, {'token': SECRET})
        self.assertNotIn(SECRET, command)
        self.assertIn('/root/phone_network.py', command)
        self.assertNotIn('"/root/phone_network.py","wb"', command)
        compile(command[len("python3 -c '"):-1], 'receiver', 'exec')
        ast.parse(phone_upgrade.payload(source, {'token': SECRET}), feature_version=(3, 9))

    def test_upgrade_refuses_while_connection_is_active(self):
        with patch.object(win, 'connection_active', return_value=True), patch.object(legacy.usb, 'connect_phone') as connect:
            with self.assertRaisesRegex(RuntimeError, 'Disconnect it yourself'):
                phone_upgrade.send({}, BASE / 'src')
            connect.assert_not_called()

    def test_384_simultaneous_sessions_and_health(self):
        phone = self.fixture()
        server = self.server(phone)
        tcp_clients, udp_clients = [], []
        try:
            # TCP + UDP combined reach the configured limit; this is intentionally
            # far beyond the former 88-session ceiling, without touching the phone.
            def open_tcp(_):
                return legacy.socks_connect(server.server_address, 'test-password', 1, 'example.com', 443)[0]
            with concurrent.futures.ThreadPoolExecutor(max_workers=48) as pool:
                for client in pool.map(open_tcp, range(300)):
                    tcp_clients.append(client)
            for _ in range(84):
                client, bound = legacy.socks_connect(server.server_address, 'test-password', 3, '0.0.0.0', 0)
                udp_clients.append((client, bound))
            self.assertEqual(server.snapshot()['active'], 300)  # UDP dials lazily on its first packet.
            for index, client in enumerate(tcp_clients):
                value = ('tcp-%d' % index).encode()
                client.sendall(value)
                self.assertEqual(p.exact(client, len(value)), value)
            for index, (_, bound) in enumerate(udp_clients):
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                    udp.settimeout(5)
                    payload = ('udp-%d' % index).encode()
                    udp.sendto(b'\0\0\0'+p.pack_address('1.1.1.1', 53)+payload, bound)
                    _, _, answer = p.unpack_datagram(udp.recvfrom(65536)[0])
                    self.assertEqual(answer, payload)
            control, info = phone.operation('ping')
            control.close()
            self.assertEqual(server.snapshot()['active'], 384)
            self.assertEqual(info['active'], 384)
            self.assertEqual(server.snapshot()['rejected'], 0)
            print('\nSTRESS PASS: 300 TCP + 84 UDP sessions, all round-trips, health responsive, 0 capacity rejections.', flush=True)
        finally:
            for client in tcp_clients:
                client.close()
            for client, _ in udp_clients:
                client.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
