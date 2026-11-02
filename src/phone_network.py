"""Foreground USB TCP/UDP relay. Standard library; no hotspot or TLS interception.

Only the trusted USB host can reach the loopback listener. A 256-bit pairing
secret is required. UDP replies are accepted only from recently contacted peers.
"""
import hmac
import ipaddress
import json
import select
import socket
import struct
import threading
import time

TOKEN = "__USB_NETWORK_SECRET__"
PORT = 18766
MAX_FRAME = 65536


def exact(sock, count):
    result = bytearray()
    while len(result) < count:
        data = sock.recv(count - len(result))
        if not data:
            raise EOFError("Connection closed")
        result.extend(data)
    return bytes(result)


def read_frame(sock, limit=MAX_FRAME):
    size = struct.unpack("!I", exact(sock, 4))[0]
    if not 0 < size <= limit:
        raise ValueError("Invalid frame size")
    return exact(sock, size)


def write_frame(sock, data):
    if not 0 < len(data) <= MAX_FRAME:
        raise ValueError("Invalid frame size")
    sock.sendall(struct.pack("!I", len(data)) + data)


def reply(sock, **data):
    write_frame(sock, json.dumps(data).encode())


def pack_address(host, port):
    if not 0 <= port <= 65535:
        raise ValueError("Invalid port")
    try:
        address = ipaddress.ip_address(host)
        data = bytes([1 if address.version == 4 else 4]) + address.packed
    except ValueError:
        data = host.encode("idna")
        if not 1 <= len(data) <= 253:
            raise ValueError("Invalid host")
        data = b"\x03" + bytes([len(data)]) + data
    return data + struct.pack("!H", port)


def read_address(reader):
    kind = reader(1)[0]
    if kind in (1, 4):
        host = str(ipaddress.ip_address(reader(4 if kind == 1 else 16)))
    elif kind == 3:
        count = reader(1)[0]
        if count == 0:
            raise ValueError("Empty hostname")
        host = reader(count).decode("ascii")
    else:
        raise ValueError("Unsupported address type")
    return host, struct.unpack("!H", reader(2))[0]


def unpack_datagram(data):
    if len(data) < 4 or data[:3] != b"\x00\x00\x00":
        raise ValueError("Fragmented/invalid SOCKS datagram")
    offset = 3
    def read(count):
        nonlocal offset
        if offset + count > len(data):
            raise ValueError("Truncated address")
        part = data[offset:offset + count]
        offset += count
        return part
    host, port = read_address(read)
    return host, port, data[offset:]


def public_addresses(host, port, kind):
    if not isinstance(host, str) or not 1 <= len(host) <= 253:
        raise ValueError("Invalid host")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Invalid port")
    # Resolve on the PHONE, not over the PC's normal internet connection.
    results = []
    for family, socktype, proto, _, address in socket.getaddrinfo(host, port, type=kind):
        ip = ipaddress.ip_address(address[0])
        if ip.is_global and not ip.is_multicast and not ip.is_unspecified:
            if ip.version == 6 and (ip.ipv4_mapped or ip.sixtofour or ip.teredo):
                continue
            results.append((family, socktype, proto, address))
    if not results:
        raise ValueError("Only public internet destinations are allowed")
    return results


def connect_public(host, port):
    for family, kind, proto, address in public_addresses(host, port, socket.SOCK_STREAM):
        upstream = socket.socket(family, kind, proto)
        try:
            upstream.settimeout(15)
            upstream.connect(address)
            return upstream
        except OSError:
            upstream.close()
    raise OSError("Phone could not reach destination")


def copy_stream(left, right, stop=None, idle=1800):
    inputs = [left, right]
    peers = {left: right, right: left}
    active = time.monotonic()
    while inputs and (stop is None or not stop.is_set()):
        if time.monotonic() - active > idle:
            return
        ready, _, _ = select.select(inputs, [], [], 1)
        for source in ready:
            data = source.recv(32768)
            if data:
                peers[source].sendall(data)
                active = time.monotonic()
            else:
                inputs.remove(source)
                try:
                    peers[source].shutdown(socket.SHUT_WR)
                except OSError:
                    pass


def udp_session(tunnel, resolver=public_addresses, stop=None):
    sockets = {}
    allowed = {}
    cache = {}
    active = time.monotonic()
    try:
        while stop is None or not stop.is_set():
            now = time.monotonic()
            if now - active > 600:
                return
            ready, _, _ = select.select([tunnel, *sockets.values()], [], [], 1)
            for source in ready:
                now = time.monotonic()
                if source is tunnel:
                    host, port, payload = unpack_datagram(read_frame(tunnel))
                    if len(payload) > 65507:
                        continue
                    key = (host, port)
                    if key not in cache or cache[key][0] < now:
                        try:
                            resolved = resolver(host, port, socket.SOCK_DGRAM)
                        except (OSError, ValueError):
                            continue
                        if len(cache) >= 128:
                            cache.clear()
                        cache[key] = (now + 60, resolved)
                    allowed = {k: v for k, v in allowed.items() if v > now}
                    for family, kind, proto, address in cache[key][1]:
                        try:
                            if family not in sockets:
                                udp = socket.socket(family, kind, proto)
                                udp.settimeout(10)
                                udp.bind(("0.0.0.0" if family == socket.AF_INET else "::", 0))
                                sockets[family] = udp
                            peer = (family, address[0], address[1])
                            if peer not in allowed and len(allowed) >= 128:
                                break
                            sockets[family].sendto(payload, address)
                            allowed[peer] = now + 300
                            active = now
                            break
                        except OSError:
                            continue
                else:
                    try:
                        payload, peer = source.recvfrom(65507)
                    except OSError:
                        continue
                    if allowed.get((source.family, peer[0], peer[1]), 0) <= now:
                        continue
                    write_frame(tunnel, b"\0\0\0" + pack_address(peer[0], peer[1]) + payload)
                    active = now
    finally:
        for udp in sockets.values():
            udp.close()


def handle(client, token, connector=connect_public, udp_handler=udp_session):
    acknowledged = False
    upstream = None
    try:
        client.settimeout(20)
        request = json.loads(read_frame(client, 4096))
        if not isinstance(request, dict):
            raise ValueError("Invalid request")
        secret = request.get("token")
        if not isinstance(secret, str) or not hmac.compare_digest(secret.encode(), token.encode()):
            raise ValueError("Authentication failed")
        operation = request.get("op")
        if operation == "ping":
            reply(client, ok=True, version=2, capabilities=["tcp", "udp"])
        elif operation == "tcp":
            upstream = connector(request.get("host"), request.get("port"))
            reply(client, ok=True)
            acknowledged = True
            copy_stream(client, upstream)
        elif operation == "udp":
            reply(client, ok=True)
            acknowledged = True
            udp_handler(client)
        else:
            raise ValueError("Unknown operation")
    except (OSError, EOFError, ValueError, TypeError) as error:
        if not acknowledged:
            try:
                reply(client, ok=False, error=str(error)[:160])
            except (OSError, ValueError):
                pass
    finally:
        client.close()
        if upstream is not None:
            upstream.close()


def main():
    if len(TOKEN) != 64:
        raise SystemExit("Transfer a paired copy from the PC first")
    limit = threading.BoundedSemaphore(96)
    def worker(client):
        try:
            handle(client, TOKEN)
        finally:
            limit.release()
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", PORT))
        listener.listen(32)
        listener.settimeout(1)
        print("PHONE NETWORK READY (TCP + UDP). Keep iSH open and unlocked.", flush=True)
        print("Cellular ON; Wi-Fi OFF in Settings; Personal Hotspot OFF.", flush=True)
        try:
            while True:
                try:
                    client, _ = listener.accept()
                except socket.timeout:
                    continue
                if not limit.acquire(False):
                    client.close()
                    continue
                threading.Thread(target=worker, args=(client,), daemon=True).start()
        except KeyboardInterrupt:
            print("Stopped")


if __name__ == "__main__":
    main()
