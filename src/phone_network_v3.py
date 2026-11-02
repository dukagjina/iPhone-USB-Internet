"""Standalone foreground iSH relay, installed as /root/phone_network.py.

One asyncio event loop for established sessions, bounded DNS work and buffers.
Wire-compatible TCP/UDP frames; authenticated capability negotiation is v3.
"""
import asyncio
import concurrent.futures
import hmac
import json
import os
import selectors
import socket
import struct
import time

import ipaddress
from types import SimpleNamespace

# The installed file is always /root/phone_network.py. Pairing is injected only
# into the in-memory transfer payload, never into the distributed template.
TOKEN = '__PHONE_PAIRING_SECRET__'


def pack_address(host, port):
    if not 0 <= port <= 65535:
        raise ValueError('Invalid port')
    try:
        address = ipaddress.ip_address(host)
        data = bytes([1 if address.version == 4 else 4]) + address.packed
    except ValueError:
        data = host.encode('idna')
        if not 1 <= len(data) <= 253:
            raise ValueError('Invalid host')
        data = b'\x03' + bytes([len(data)]) + data
    return data + struct.pack('!H', port)


def unpack_datagram(data):
    if len(data) < 4 or data[:3] != b'\0\0\0':
        raise ValueError('Fragmented/invalid SOCKS datagram')
    offset = 3
    def read(count):
        nonlocal offset
        if offset + count > len(data):
            raise ValueError('Truncated address')
        part = data[offset:offset + count]
        offset += count
        return part
    kind = read(1)[0]
    if kind in (1, 4):
        host = str(ipaddress.ip_address(read(4 if kind == 1 else 16)))
    elif kind == 3:
        count = read(1)[0]
        if not count:
            raise ValueError('Empty hostname')
        host = read(count).decode('ascii')
    else:
        raise ValueError('Unsupported address type')
    return host, struct.unpack('!H', read(2))[0], data[offset:]


def public_addresses(host, port, kind):
    if not isinstance(host, str) or not 1 <= len(host) <= 253:
        raise ValueError('Invalid host')
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('Invalid port')
    results = []
    for family, socktype, proto, _, address in socket.getaddrinfo(host, port, type=kind):
        ip = ipaddress.ip_address(address[0])
        if ip.is_global and not ip.is_multicast and not ip.is_unspecified:
            if ip.version == 6 and (ip.ipv4_mapped or ip.sixtofour or ip.teredo):
                continue
            results.append((family, socktype, proto, address))
    if not results:
        raise ValueError('Only public internet destinations are allowed')
    return results


p = SimpleNamespace(MAX_FRAME=65536, PORT=18766, pack_address=pack_address,
                    unpack_datagram=unpack_datagram, public_addresses=public_addresses)

DATA_LIMIT = 400
HANDSHAKE_LIMIT = 32
CHUNK = 32768
IDLE_TCP = 900
IDLE_HALF = 30
IDLE_UDP = 90


async def frame(reader, limit=p.MAX_FRAME):
    size = struct.unpack('!I', await reader.readexactly(4))[0]
    if not 0 < size <= limit:
        raise ValueError('Invalid frame length')
    return await reader.readexactly(size)


async def send_frame(writer, data):
    if not 0 < len(data) <= p.MAX_FRAME:
        raise ValueError('Invalid frame length')
    writer.write(struct.pack('!I', len(data)) + data)
    await asyncio.wait_for(writer.drain(), 20)


async def reply(writer, **values):
    await send_frame(writer, json.dumps(values).encode())


async def close_writer(writer):
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), 2)
    except (OSError, asyncio.TimeoutError):
        pass


def buffers(writer):
    writer.transport.set_write_buffer_limits(high=65536, low=16384)


class DatagramInbox(asyncio.DatagramProtocol):
    """Python 3.9-compatible UDP I/O, with a bounded receive queue."""
    def __init__(self, family, allowed):
        self.family = family
        self.allowed = allowed
        self.queue = asyncio.Queue(maxsize=8)

    def datagram_received(self, data, address):
        if self.allowed.get((self.family, address[0], address[1]), 0) <= time.monotonic():
            return
        try:
            self.queue.put_nowait((data, address))
        except asyncio.QueueFull:
            pass  # UDP is lossy; do not let a flood allocate unbounded memory.


class Relay:
    def __init__(self, token, limit=DATA_LIMIT, resolver=p.public_addresses, connector=None):
        self.token = token
        self.limit = limit
        self.resolver = resolver
        self.connector = connector
        self.pending = self.active = 0
        self.tasks = set()
        self.dns_slots = asyncio.Semaphore(16)

    async def addresses(self, host, port, kind):
        # Numeric destinations avoid the executor entirely (normal TUN traffic).
        try:
            socket.inet_pton(socket.AF_INET6 if ':' in host else socket.AF_INET, host)
            return self.resolver(host, port, kind)
        except OSError:
            await self.dns_slots.acquire()
            try:
                pending = asyncio.get_running_loop().run_in_executor(None, self.resolver, host, port, kind)
            except BaseException:
                self.dns_slots.release()
                raise
            pending.add_done_callback(lambda _: self.dns_slots.release())
            # Keep a DNS slot until the underlying resolver really finishes, even
            # when its caller times out. This prevents an unbounded executor queue.
            return await asyncio.shield(pending)

    async def connect(self, host, port):
        if self.connector:
            return await self.connector(host, port)
        loop = asyncio.get_running_loop()
        addresses = await self.addresses(host, port, socket.SOCK_STREAM)
        # Overall handshake deadline bounds sequential address fallback as well.
        for family, kind, proto, address in addresses[:8]:
            outbound = socket.socket(family, kind, proto)
            outbound.setblocking(False)
            transferred = False
            try:
                await asyncio.wait_for(loop.sock_connect(outbound, address), 3)
                reader, writer = await asyncio.open_connection(sock=outbound, limit=CHUNK)
                buffers(writer)
                transferred = True
                return reader, writer
            except (OSError, asyncio.TimeoutError):
                pass
            finally:
                if not transferred:
                    outbound.close()
        raise OSError('Phone could not reach destination')

    async def accept(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        acquired = False
        pending = False
        acknowledged = False
        remote = None
        try:
            if self.pending >= HANDSHAKE_LIMIT:
                return
            self.pending += 1
            pending = True
            buffers(writer)
            request = json.loads(await asyncio.wait_for(frame(reader, 4096), 10))
            if not isinstance(request, dict):
                raise ValueError('Invalid request')
            token = request.get('token')
            if not isinstance(token, str) or not hmac.compare_digest(token.encode(), self.token.encode()):
                raise ValueError('Authentication failed')
            operation = request.get('op')
            if operation == 'ping':
                await reply(writer, ok=True, version=3, capabilities=['tcp', 'udp', 'async-relay'],
                            max_sessions=self.limit, active=self.active)
                return
            if operation not in ('tcp', 'udp'):
                raise ValueError('Unknown operation')
            if self.active >= self.limit:
                await reply(writer, ok=False, error='Phone capacity reached', code='capacity')
                return
            self.active += 1
            acquired = True
            self.pending -= 1
            pending = False
            if operation == 'tcp':
                remote_reader, remote = await asyncio.wait_for(
                    self.connect(request.get('host'), request.get('port')), 15)
                await reply(writer, ok=True)
                acknowledged = True
                await self.stream(reader, writer, remote_reader, remote)
            else:
                await reply(writer, ok=True)
                acknowledged = True
                await self.datagrams(reader, writer)
        except (OSError, ValueError, TypeError, asyncio.TimeoutError, asyncio.IncompleteReadError) as error:
            if not acknowledged:
                try:
                    await reply(writer, ok=False, error=str(error)[:160])
                except (OSError, asyncio.TimeoutError):
                    pass
        finally:
            # Release capacity even if close/wait_closed itself fails.
            if pending:
                self.pending -= 1
            if acquired:
                self.active -= 1
            try:
                if remote is not None:
                    await close_writer(remote)
                await close_writer(writer)
            finally:
                self.tasks.discard(task)

    async def stream(self, left_reader, left_writer, right_reader, right_writer):
        last = [time.monotonic()]
        async def pump(reader, writer):
            while True:
                data = await reader.read(CHUNK)
                last[0] = time.monotonic()
                if not data:
                    if writer.can_write_eof():
                        writer.write_eof()
                        await asyncio.wait_for(writer.drain(), 20)
                    return
                writer.write(data)
                await asyncio.wait_for(writer.drain(), 20)
        tasks = [asyncio.create_task(pump(left_reader, right_writer)),
                 asyncio.create_task(pump(right_reader, left_writer))]
        try:
            while True:
                done, pending = await asyncio.wait(tasks, timeout=.5, return_when=asyncio.ALL_COMPLETED)
                for task in done:
                    task.result()
                if not pending:
                    return
                if time.monotonic() - last[0] > (IDLE_HALF if done else IDLE_TCP):
                    return
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def datagrams(self, reader, writer):
        loop = asyncio.get_running_loop()
        sockets = {}
        receive_tasks = {}
        allowed = {}
        cache = {}
        incoming = asyncio.create_task(frame(reader))
        last = time.monotonic()
        try:
            while time.monotonic() - last < IDLE_UDP:
                done, _ = await asyncio.wait([incoming, *receive_tasks], timeout=1,
                                             return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if task is incoming:
                        host, port, data = p.unpack_datagram(task.result())
                        incoming = asyncio.create_task(frame(reader))
                        if len(data) > 65507:
                            continue
                        key = host, port
                        now = time.monotonic()
                        if key not in cache or cache[key][0] < now:
                            try:
                                resolved = await asyncio.wait_for(self.addresses(host, port, socket.SOCK_DGRAM), 8)
                            except (OSError, ValueError, asyncio.TimeoutError):
                                continue
                            if len(cache) >= 128:
                                cache.clear()
                            cache[key] = now + 60, resolved
                        for expired in [k for k, v in allowed.items() if v <= now]:
                            del allowed[expired]
                        for family, kind, proto, address in cache[key][1]:
                            peer = family, address[0], address[1]
                            if peer not in allowed and len(allowed) >= 128:
                                break
                            try:
                                if family not in sockets:
                                    transport, inbox = await loop.create_datagram_endpoint(
                                        lambda: DatagramInbox(family, allowed),
                                        local_addr=('0.0.0.0' if family == socket.AF_INET else '::', 0), family=family)
                                    sockets[family] = transport, inbox
                                    receive_tasks[asyncio.create_task(inbox.queue.get())] = family
                                allowed[peer] = now + 300
                                sockets[family][0].sendto(data, address)
                                last = time.monotonic()
                                break
                            except (OSError, asyncio.TimeoutError):
                                continue
                    else:
                        family = receive_tasks.pop(task)
                        data, peer = task.result()
                        receive_tasks[asyncio.create_task(sockets[family][1].queue.get())] = family
                        if allowed.get((family, peer[0], peer[1]), 0) <= time.monotonic():
                            continue
                        await send_frame(writer, b'\0\0\0' + p.pack_address(peer[0], peer[1]) + data)
                        last = time.monotonic()
        finally:
            tasks = [incoming, *receive_tasks]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for transport, _ in sockets.values():
                transport.close()


def safe_capacity():
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        desired = 1024 if hard == resource.RLIM_INFINITY else min(1024, hard)
        if soft != resource.RLIM_INFINITY and soft < desired:
            resource.setrlimit(resource.RLIMIT_NOFILE, (desired, hard))
            soft = desired
        return DATA_LIMIT if soft == resource.RLIM_INFINITY else min(DATA_LIMIT, max(1, (soft - 96) // 2))
    except (ImportError, OSError, ValueError):
        return 192  # Conservative fallback; PC negotiates down, never assumes 400.


async def main():
    if len(TOKEN) != 64:
        raise RuntimeError('Run Phone update from the paired Windows app first.')
    relay = Relay(TOKEN, safe_capacity())
    loop = asyncio.get_running_loop()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=16)
    loop.set_default_executor(executor)
    server = await asyncio.start_server(relay.accept, '127.0.0.1', p.PORT, limit=CHUNK, backlog=128)
    print('PHONE NETWORK READY - capacity', relay.limit, flush=True)
    print('Cellular ON; Wi-Fi and Personal Hotspot OFF. Keep iSH visible and unlocked.', flush=True)
    try:
        async with server:
            await server.serve_forever()
    finally:
        for task in list(relay.tasks):
            task.cancel()
        await asyncio.gather(*relay.tasks, return_exceptions=True)
        executor.shutdown(wait=False, cancel_futures=True)


if __name__ == '__main__':
    if os.name != 'nt':
        # iSH already runs select() in the working v2 relay. Avoid depending on
        # epoll emulation, while keeping descriptor use below select's 1024 limit.
        class PhoneLoopPolicy(asyncio.DefaultEventLoopPolicy):
            def new_event_loop(self):
                return asyncio.SelectorEventLoop(selectors.SelectSelector())
        asyncio.set_event_loop_policy(PhoneLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('Stopped. Start again: python3 /root/phone_network.py')
