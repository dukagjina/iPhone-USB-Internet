"""Bounded, authenticated loopback gateway compatible with the existing phone."""
import hmac
import select
import socket
import socketserver
import threading
import time
from collections import deque

import pc_gateway as legacy
import phone_network as p


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        upstream = None
        established = False
        command = None
        admitted = False
        try:
            self.request.settimeout(12)
            version, count = p.exact(self.request, 2)
            if version != 5 or count == 0 or 2 not in p.exact(self.request, count):
                self.request.sendall(b'\x05\xff')
                return
            self.request.sendall(b'\x05\x02')
            version, count = p.exact(self.request, 2)
            user = p.exact(self.request, count)
            password = p.exact(self.request, p.exact(self.request, 1)[0])
            if version != 1 or not hmac.compare_digest(user, b'usb') or not hmac.compare_digest(password, self.server.password.encode()):
                self.request.sendall(b'\x01\x01')
                return
            self.request.sendall(b'\x01\x00')
            version, command, reserved = p.exact(self.request, 3)
            host, port = p.read_address(lambda n: p.exact(self.request, n))
            if version != 5 or reserved != 0 or command not in (1, 3):
                self.request.sendall(b'\x05\x07\x00' + p.pack_address('127.0.0.1', 0))
                return
            if command == 3:
                # SOCKS UDP success acknowledges a local association, not an
                # upstream dial. Allocate phone capacity only when a datagram
                # arrives, and release it after answered DNS work becomes idle.
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                    udp.bind(('127.0.0.1', 0))
                    udp.settimeout(10)
                    self.request.sendall(b'\x05\x00\x00' + p.pack_address(*udp.getsockname()))
                    established = True
                    self.datagrams(udp, port)
                return
            admitted = self.server.acquire_slot(command, self.request)
            if not admitted:
                self.request.sendall(b'\x05\x01\x00' + p.pack_address('127.0.0.1', 0))
                return
            upstream, _ = self.server.open_phone('tcp' if command == 1 else 'udp', host, port)
            with self.server.lock:
                if self.server.stopping.is_set():
                    return
                self.server.sockets.add(upstream)
            if command == 1:
                self.request.sendall(b'\x05\x00\x00' + p.pack_address('127.0.0.1', 0))
                established = True
                self.stream(upstream, port)
        except (OSError, EOFError, ValueError, RuntimeError):
            if not self.server.stopping.is_set():
                with self.server.lock:
                    self.server.failures += 1
            if not established:
                try:
                    self.request.sendall(b'\x05\x01\x00' + p.pack_address('127.0.0.1', 0))
                except OSError:
                    pass
        finally:
            if upstream is not None:
                with self.server.lock:
                    self.server.sockets.discard(upstream)
                upstream.close()
            if admitted:
                self.server.release_slot(command)

    def stream(self, upstream, port):
        inputs = [self.request, upstream]
        peers = {self.request: upstream, upstream: self.request}
        last = time.monotonic()
        while inputs and not self.server.stopping.is_set():
            # Reclaim abandoned browser sockets, retaining active half-close replies.
            idle = time.monotonic() - last
            if idle > (30 if len(inputs) == 1 else 900):
                return
            # Under actual queue pressure, reclaim only a long-idle stream.
            # Actively transferring and recently used connections are untouched.
            with self.server.lock:
                pressure = bool(self.server.waiters[1] or self.server.waiters[3])
            pressure_idle = self.server.web_idle if port in (80, 443) else 60
            if pressure and idle > pressure_idle:
                with self.server.lock:
                    self.server.idle_reaped += 1
                return
            ready, _, _ = select.select(inputs, [], [], .5)
            for source in ready:
                data = source.recv(65536)
                if data:
                    peers[source].sendall(data)
                    last = time.monotonic()
                    with self.server.lock:
                        if source is self.request:
                            self.server.upload += len(data)
                        else:
                            self.server.download += len(data)
                else:
                    inputs.remove(source)
                    last = time.monotonic()
                    try:
                        peers[source].shutdown(socket.SHUT_WR)
                    except OSError:
                        pass

    def datagrams(self, udp, requested_port):
        peer = (self.client_address[0], requested_port) if requested_port else None
        last = time.monotonic()
        upstream = None
        admitted = False
        dns_only = True
        dns_replied = False
        pending_dns = set()

        def release():
            nonlocal upstream, admitted
            if upstream is not None:
                with self.server.lock:
                    self.server.sockets.discard(upstream)
                upstream.close()
                upstream = None
            if admitted:
                self.server.release_slot(3)
                admitted = False

        with self.server.lock:
            self.server.udp_associations += 1
        try:
            while not self.server.stopping.is_set() and time.monotonic() - last < 75:
                if upstream is not None and dns_only and dns_replied and not pending_dns and time.monotonic() - last > self.server.dns_idle:
                    release()
                    with self.server.lock:
                        self.server.dns_parked += 1
                ready, _, _ = select.select([self.request, udp] + ([upstream] if upstream else []), [], [], .2)
                if self.request in ready:
                    return
                if udp in ready:
                    data, source = udp.recvfrom(p.MAX_FRAME)
                    if source[0] != self.client_address[0] or (peer is not None and source != peer):
                        continue
                    try:
                        host, port, payload = p.unpack_datagram(data)
                    except (ValueError, UnicodeError):
                        continue
                    peer = source
                    if upstream is None:
                        admitted = self.server.acquire_slot(3, self.request)
                        if not admitted:
                            return
                        upstream, _ = self.server.open_phone('udp', None, None)
                        with self.server.lock:
                            self.server.sockets.add(upstream)
                        dns_replied = False
                    if port == 53 and len(payload) >= 12 and not payload[2] & 128 and len(pending_dns) < 128:
                        pending_dns.add((host, port, payload[:2]))
                    else:
                        # Never park generic UDP or an unbounded/malformed DNS
                        # workload. Those retain the existing 75s idle timeout.
                        dns_only = False
                    p.write_frame(upstream, data)
                    last = time.monotonic()
                    with self.server.lock:
                        self.server.upload += len(data)
                if upstream is not None and upstream in ready:
                    data = p.read_frame(upstream)
                    host, port, payload = p.unpack_datagram(data)
                    key = (host, port, payload[:2])
                    if len(payload) >= 12 and payload[2] & 128 and key in pending_dns:
                        pending_dns.discard(key)
                        dns_replied = True
                    if peer:
                        udp.sendto(data, peer)
                    last = time.monotonic()
                    with self.server.lock:
                        self.server.download += len(data)
        finally:
            release()
            with self.server.lock:
                self.server.udp_associations -= 1


class Gateway(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 512

    def __init__(self, config, password, opener=None, limit=384, queue_limit=256, queue_timeout=20,
                 web_idle=15, dns_idle=.5):
        self.password = password
        self.opener = opener or (lambda op, host, port: legacy.operation(config, op, host, port))
        self.capacity = limit
        self.queue_limit = queue_limit
        self.queue_timeout = queue_timeout
        self.web_idle, self.dns_idle = web_idle, dns_idle
        # Parked UDP associations still own a bounded lightweight handler, but
        # no phone socket. Leave separate handler headroom for them.
        self.handler_limit = threading.BoundedSemaphore(limit + queue_limit + 128)
        self.dial_limit = threading.BoundedSemaphore(min(24, limit))
        self.stopping = threading.Event()
        self.lock = threading.Lock()
        self.changed = threading.Condition(self.lock)
        self.waiters = {1: deque(), 3: deque()}
        self.by_kind = {1: 0, 3: 0}
        self.kind_limit = limit - min(16, limit // 8)
        # TCP uses two phone descriptors; dual-stack UDP can use three.
        # At full capacity reserve 128 of select()'s 1024 descriptors for
        # listeners, handshakes and probes. Smaller negotiated phones retain
        # a conservative budget instead of assuming their descriptor ceiling.
        self.socket_budget = 896 if limit == 384 else max(3, 2 * limit)
        self.sockets = set()
        self.active = self.rejected = self.failures = self.upload = self.download = 0
        self.peak_active = self.peak_waiting = 0
        self.handlers = self.queued_total = self.idle_reaped = 0
        self.dialing = self.dial_waiting = 0
        self.udp_associations = self.dns_parked = self.cancelled_waiters = 0
        self.rejection_reasons = dict(queue_full=0, queue_timeout=0, handler_full=0)
        self.peak_by_kind = {1: 0, 3: 0}
        self.peak_capacity_waiting = 0
        super().__init__(('127.0.0.1', 0), Handler)

    def snapshot(self):
        with self.lock:
            return dict(active=self.active, rejected=self.rejected, failures=self.failures,
                        upload=self.upload, download=self.download, capacity=self.capacity,
                        tcp=self.by_kind[1], udp=self.by_kind[3],
                        queued=sum(map(len, self.waiters.values())), delayed=self.queued_total,
                        idle_reaped=self.idle_reaped, dialing=self.dialing, dial_waiting=self.dial_waiting,
                        peak_active=self.peak_active, peak_waiting=self.peak_waiting,
                        udp_associations=self.udp_associations, dns_parked=self.dns_parked,
                        cancelled_waiters=self.cancelled_waiters,
                        peak_tcp=self.peak_by_kind[1], peak_udp=self.peak_by_kind[3],
                        peak_capacity_waiting=self.peak_capacity_waiting,
                        rejection_reasons=dict(self.rejection_reasons))

    def open_phone(self, op, host, port):
        # Bound concurrent USB/open handshakes as well as established streams.
        # Leave headroom in the phone's 32-slot handshake pool for health checks.
        with self.lock:
            self.dial_waiting += 1
            self.peak_waiting = max(self.peak_waiting, self.dial_waiting + sum(map(len, self.waiters.values())))
        acquired = False
        deadline = time.monotonic() + 15
        try:
            while not self.stopping.is_set():
                if self.dial_limit.acquire(timeout=.1):
                    acquired = True
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError('Phone connection setup queue timed out')
        finally:
            with self.lock:
                self.dial_waiting -= 1
        if not acquired:
            raise RuntimeError('Gateway stopped')
        with self.lock:
            self.dialing += 1
        try:
            return self.opener(op, host, port)
        finally:
            with self.lock:
                self.dialing -= 1
            self.dial_limit.release()

    def acquire_slot(self, kind, client=None):
        def abandoned():
            if client is None:
                return False
            try:
                ready, _, _ = select.select([client], [], [], 0)
                return bool(ready) and client.recv(1, socket.MSG_PEEK) == b''
            except OSError:
                return True

        with self.changed:
            available = lambda: (self.active < self.capacity and self.by_kind[kind] < self.kind_limit
                                 and 2 * self.by_kind[1] + 3 * self.by_kind[3]
                                 + (2 if kind == 1 else 3) <= self.socket_budget)
            if self.stopping.is_set():
                return False
            if not self.waiters[kind] and available():
                self.active += 1
                self.peak_active = max(self.peak_active, self.active)
                self.by_kind[kind] += 1
                self.peak_by_kind[kind] = max(self.peak_by_kind[kind], self.by_kind[kind])
                return True
            if sum(map(len, self.waiters.values())) >= self.queue_limit:
                self.rejected += 1
                self.rejection_reasons['queue_full'] += 1
                return False
            ticket = object()
            self.waiters[kind].append(ticket)
            self.peak_capacity_waiting = max(self.peak_capacity_waiting, sum(map(len, self.waiters.values())))
            self.peak_waiting = max(self.peak_waiting, self.dial_waiting + sum(map(len, self.waiters.values())))
            self.queued_total += 1
            deadline = time.monotonic() + self.queue_timeout
            try:
                while not self.stopping.is_set():
                    if abandoned():
                        self.cancelled_waiters += 1
                        return False
                    if self.waiters[kind][0] is ticket and available():
                        self.active += 1
                        self.peak_active = max(self.peak_active, self.active)
                        self.by_kind[kind] += 1
                        self.peak_by_kind[kind] = max(self.peak_by_kind[kind], self.by_kind[kind])
                        return True
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self.rejected += 1
                        self.rejection_reasons['queue_timeout'] += 1
                        return False
                    self.changed.wait(min(remaining, .2))
                return False
            finally:
                self.waiters[kind].remove(ticket)
                self.changed.notify_all()

    def release_slot(self, kind):
        with self.changed:
            self.active -= 1
            self.by_kind[kind] -= 1
            self.changed.notify_all()

    def process_request(self, request, client_address):
        # Never block the accept loop behind one congested client. Bounded
        # handlers authenticate before joining a per-protocol FIFO wait queue.
        if self.stopping.is_set():
            request.close()
            return
        if not self.handler_limit.acquire(False):
            with self.lock:
                self.rejected += 1
                self.rejection_reasons['handler_full'] += 1
            request.close()
            return
        with self.lock:
            self.handlers += 1
            self.sockets.add(request)
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.finished(request)
            raise

    def finished(self, request):
        with self.lock:
            self.handlers -= 1
            self.sockets.discard(request)
        self.handler_limit.release()

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.finished(request)

    def stop(self):
        self.stopping.set()
        with self.changed:
            self.changed.notify_all()
            sockets = list(self.sockets)
        for connection in sockets:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        self.shutdown()
        self.server_close()
