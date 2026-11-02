"""Authenticated loopback SOCKS5 -> trusted USB -> iPhone TCP/UDP.

Never opens destination internet sockets on Windows. No host routes, proxies,
firewall rules, or drivers are changed by this program.
"""
import argparse
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import select
import socket
import socketserver
import ssl
import struct
import sys
import threading

import phone_network as protocol

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "iphone-usb-test"))
import pc_test as usb


def operation(config, op, host=None, port=None):
    connection = usb.connect_phone(config, protocol.PORT)
    try:
        protocol.reply(connection, token=config["token"], op=op, host=host, port=port)
        result = json.loads(protocol.read_frame(connection, 4096))
        if not result.get("ok"):
            raise RuntimeError(result.get("error", "Phone refused connection"))
        return connection, result
    except BaseException:
        connection.close()
        raise


def check(config):
    connection, result = operation(config, "ping")
    connection.close()
    if result.get("version") != 2 or "udp" not in result.get("capabilities", []):
        raise RuntimeError("Upgrade the phone relay first")
    return "Phone network v2 authenticated (TCP + UDP)."


def rendered_phone(config):
    return (ROOT / "phone_network.py").read_text(encoding="utf-8").replace(
        'TOKEN = "__USB_NETWORK_SECRET__"', 'TOKEN = ' + repr(config["token"])).encode()


def bootstrap(config):
    digest = hashlib.sha256(rendered_phone(config)).hexdigest()
    # Length framing avoids the old transfer's USB half-close/ACK failure.
    # Keep the existing /root/usb_test.py untouched for recovery.
    return ('python3 -c \'import socket,struct,hashlib,os; s=socket.socket(); '
            's.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); '
            's.bind(("127.0.0.1",18764)); s.listen(1); '
            'print("READY FOR UPGRADE",flush=True); c,_=s.accept(); c.settimeout(60); '
            'f=c.makefile("rb"); n=struct.unpack("!I",f.read(4))[0]; '
            'assert 0<n<65536; b=f.read(n); '
            f'assert len(b)==n and hashlib.sha256(b).hexdigest()=="{digest}","Checksum failed"; '
            'compile(b,"phone_network.py","exec"); '
            'open("/root/phone_network.py.new","wb").write(b); '
            'os.replace("/root/phone_network.py.new","/root/phone_network.py"); '
            'c.sendall(b"OK"); f.close(); c.close(); s.close(); '
            'print("Saved. Run: python3 /root/phone_network.py",flush=True)\'')


def transfer(config):
    data = rendered_phone(config)
    device = usb.select_device(config)
    with usb.connect_phone(config, usb.TRANSFER_PORT) as connection:
        protocol.write_frame(connection, data)
        if protocol.exact(connection, 2) != b"OK":
            raise RuntimeError("Upgrade not acknowledged")
    config["serial"] = device["Properties"]["SerialNumber"]
    (usb.STATE / "pairing.json").write_text(json.dumps(config), encoding="utf-8")
    return "Phone upgrade saved and phone-side SHA-256 verified. Run python3 /root/phone_network.py"


class SocksHandler(socketserver.BaseRequestHandler):
    def handle(self):
        upstream = None
        succeeded = False
        try:
            self.request.settimeout(20)
            version, count = protocol.exact(self.request, 2)
            if version != 5 or count == 0:
                return
            methods = protocol.exact(self.request, count)
            if 2 not in methods:
                self.request.sendall(b"\x05\xff")
                return
            self.request.sendall(b"\x05\x02")
            version, count = protocol.exact(self.request, 2)
            user = protocol.exact(self.request, count)
            password = protocol.exact(self.request, protocol.exact(self.request, 1)[0])
            if (version != 1 or not hmac.compare_digest(user, b"usb") or
                    not hmac.compare_digest(password, self.server.password.encode())):
                self.request.sendall(b"\x01\x01")
                return
            self.request.sendall(b"\x01\x00")
            version, command, reserved = protocol.exact(self.request, 3)
            host, port = protocol.read_address(lambda n: protocol.exact(self.request, n))
            if version != 5 or reserved != 0:
                raise ValueError("Invalid SOCKS request")
            if command == 1:
                upstream, _ = self.server.open_phone("tcp", host, port)
                self.request.sendall(b"\x05\x00\x00" + protocol.pack_address("127.0.0.1", 0))
                succeeded = True
                protocol.copy_stream(self.request, upstream, stop=self.server.stopping)
            elif command == 3:
                upstream, _ = self.server.open_phone("udp")
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                    udp.bind(("127.0.0.1", 0))
                    udp.settimeout(10)
                    self.request.sendall(b"\x05\x00\x00" + protocol.pack_address(
                        self.server.advertise_host, udp.getsockname()[1]))
                    succeeded = True
                    self.udp(udp, upstream, port)
            else:
                self.request.sendall(b"\x05\x07\x00" + protocol.pack_address("127.0.0.1", 0))
        except (OSError, EOFError, ValueError, RuntimeError):
            if not succeeded:
                try:
                    self.request.sendall(b"\x05\x01\x00" + protocol.pack_address("127.0.0.1", 0))
                except OSError:
                    pass
        finally:
            if upstream is not None:
                upstream.close()

    def udp(self, udp, upstream, requested_port):
        # SOCKS association lives exactly as long as its authenticated TCP control
        # socket. Pin UDP to one local source port, rejecting other local senders.
        peer = (self.client_address[0], requested_port) if requested_port else None
        while not self.server.stopping.is_set():
            ready, _, _ = select.select([self.request, udp, upstream], [], [], 1)
            if self.request in ready:
                return
            if udp in ready:
                data, source = udp.recvfrom(protocol.MAX_FRAME)
                if source[0] != self.client_address[0] or (peer is not None and source != peer):
                    continue
                try:
                    protocol.unpack_datagram(data)
                except (ValueError, UnicodeError):
                    continue
                if peer is None:
                    peer = source
                protocol.write_frame(upstream, data)
            if upstream in ready:
                data = protocol.read_frame(upstream)
                protocol.unpack_datagram(data)
                if peer is not None:
                    udp.sendto(data, peer)


class Gateway(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, config, password, port=0, opener=None, advertise_host="127.0.0.1"):
        self.config = config
        self.password = password
        self.stopping = threading.Event()
        self.limit = threading.BoundedSemaphore(80)
        self.opener = opener
        self.advertise_host = advertise_host
        super().__init__(("127.0.0.1", port), SocksHandler)

    def open_phone(self, op, host=None, port=None):
        if self.opener is not None:
            return self.opener(op, host, port)
        return operation(self.config, op, host, port)

    def process_request(self, request, client_address):
        if not self.limit.acquire(False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.limit.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.limit.release()

    def stop(self):
        self.stopping.set()
        self.shutdown()
        self.server_close()


def socks_connect(address, password, command, host, port):
    connection = socket.create_connection(address, timeout=20)
    try:
        connection.sendall(b"\x05\x01\x02")
        if protocol.exact(connection, 2) != b"\x05\x02":
            raise RuntimeError("SOCKS authentication unavailable")
        secret = password.encode()
        connection.sendall(b"\x01\x03usb" + bytes([len(secret)]) + secret)
        if protocol.exact(connection, 2) != b"\x01\x00":
            raise RuntimeError("SOCKS authentication failed")
        connection.sendall(bytes([5, command, 0]) + protocol.pack_address(host, port))
        response = protocol.exact(connection, 3)
        bound = protocol.read_address(lambda n: protocol.exact(connection, n))
        if response != b"\x05\x00\x00":
            raise RuntimeError("SOCKS connection failed")
        return connection, bound
    except BaseException:
        connection.close()
        raise


def diagnose(config):
    print(check(config), flush=True)
    password = secrets.token_hex(24)
    gateway = Gateway(config, password)
    thread = threading.Thread(target=gateway.serve_forever, daemon=True)
    thread.start()
    try:
        connection, _ = socks_connect(gateway.server_address, password, 1, "www.microsoft.com", 443)
        with ssl.create_default_context().wrap_socket(connection, server_hostname="www.microsoft.com") as tls:
            tls.sendall(b"HEAD / HTTP/1.1\r\nHost: www.microsoft.com\r\nConnection: close\r\n\r\n")
            response = tls.recv(4096)
            if not response.startswith(b"HTTP/1."):
                raise RuntimeError("Unexpected HTTPS reply")
        print("PASS: authenticated SOCKS -> USB -> phone HTTPS, certificate verified.", flush=True)
        connection, _ = socks_connect(gateway.server_address, password, 1, "www.microsoft.com", 80)
        with connection:
            connection.sendall(b"HEAD / HTTP/1.1\r\nHost: www.microsoft.com\r\nConnection: close\r\n\r\n")
            if not connection.recv(4096).startswith(b"HTTP/1."):
                raise RuntimeError("Unexpected port 80 reply")
        print("PASS: TCP port 80 through phone (not just HTTPS).", flush=True)
        control, bound = socks_connect(gateway.server_address, password, 3, "0.0.0.0", 0)
        with control, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            udp.settimeout(20)
            transaction = secrets.token_bytes(2)
            query = transaction + b"\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x03www\x09microsoft\x03com\x00\x00\x01\x00\x01"
            udp.sendto(b"\0\0\0" + protocol.pack_address("1.1.1.1", 53) + query, bound)
            data, _ = udp.recvfrom(65536)
            host, port, answer = protocol.unpack_datagram(data)
            if host != "1.1.1.1" or port != 53 or len(answer) < 12 or answer[:2] != transaction or not answer[2] & 128 or answer[3] & 15 or answer[6:8] == b"\0\0":
                raise RuntimeError("Unexpected DNS reply")
        print("PASS: UDP DNS query and answer through phone.", flush=True)
    finally:
        gateway.stop()
        thread.join(3)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["bootstrap", "transfer", "check", "test", "serve"])
    parser.add_argument("--port", type=int, default=18767)
    parser.add_argument("--vm", action="store_true", help="Advertise VirtualBox NAT's host-loopback alias for UDP")
    args = parser.parse_args()
    config = usb.load_config()
    if args.action == "bootstrap":
        print(bootstrap(config))
    elif args.action == "transfer":
        print(transfer(config))
    elif args.action == "check":
        print(check(config))
    elif args.action == "test":
        diagnose(config)
    else:
        check(config)
        state = ROOT / ".local"
        state.mkdir(exist_ok=True)
        path = state / "gateway.json"
        if path.exists():
            password = json.loads(path.read_text())["password"]
        else:
            password = secrets.token_hex(24)
            path.write_text(json.dumps({"username": "usb", "password": password}), encoding="utf-8")
        with Gateway(config, password, args.port,
                     advertise_host="10.0.2.2" if args.vm else "127.0.0.1") as gateway:
            print("USB gateway listening on PC loopback only. No Windows routes changed.", flush=True)
            try:
                gateway.serve_forever()
            except KeyboardInterrupt:
                gateway.stopping.set()


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, EOFError, RuntimeError) as error:
        raise SystemExit("Not connected: " + str(error))
