"""Foreground-only USB HTTPS relay for iSH. Python standard library only.

This file is a template. pc_test.py inserts a per-installation secret during USB
transfer. No traffic content or destination history is saved. Wi-Fi must be off
on the phone for this test: sockets follow iOS's default internet connection.
"""
import hmac
import ipaddress
import json
import select
import socket
import struct
import threading
import time

TOKEN = "__USB_TEST_SECRET__"
PORT = 18765
MAX_MESSAGE = 4096


def recv_exact(sock, size):
    result = bytearray()
    while len(result) < size:
        part = sock.recv(size - len(result))
        if not part:
            raise EOFError("Connection closed")
        result.extend(part)
    return bytes(result)


def receive_message(sock):
    size = struct.unpack("!I", recv_exact(sock, 4))[0]
    if not 0 < size <= MAX_MESSAGE:
        raise ValueError("Invalid message size")
    result = json.loads(recv_exact(sock, size).decode("utf-8"))
    if not isinstance(result, dict):
        raise ValueError("Expected an object")
    return result


def send_message(sock, message):
    raw = json.dumps(message, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_MESSAGE:
        raise ValueError("Message too large")
    sock.sendall(struct.pack("!I", len(raw)) + raw)


def relay(left, right, idle_timeout=90):
    """Copy bytes, preserving half-closes; enforce an idle timeout."""
    peers = {left: right, right: left}
    readable = [left, right]
    last_activity = time.monotonic()
    while readable:
        remaining = idle_timeout - (time.monotonic() - last_activity)
        if remaining <= 0:
            return
        ready, _, _ = select.select(readable, [], [], min(remaining, 5))
        for source in ready:
            data = source.recv(32768)
            destination = peers[source]
            if data:
                destination.sendall(data)
                last_activity = time.monotonic()
            else:
                readable.remove(source)
                try:
                    destination.shutdown(socket.SHUT_WR)
                except OSError:
                    pass


def connect_public(host):
    if not isinstance(host, str) or not host or len(host) > 253:
        raise ValueError("Invalid hostname")
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    public = []
    for family, socktype, proto, _, addr in addresses:
        address = ipaddress.ip_address(addr[0])
        if not address.is_global or address.is_multicast:
            continue
        public.append((family, socktype, proto, addr))
    if not public:
        raise ValueError("Only public internet HTTPS destinations are allowed")
    for family, socktype, proto, addr in public:
        upstream = socket.socket(family, socktype, proto)
        try:
            upstream.settimeout(10)
            upstream.connect(addr)
            return upstream
        except OSError:
            upstream.close()
    raise OSError("Could not reach destination through the phone")


def handle(client, token, connector=connect_public):
    upstream = None
    acknowledged = False
    try:
        client.settimeout(15)
        request = receive_message(client)
        supplied = request.get("token", "")
        if not isinstance(supplied, str) or not hmac.compare_digest(
                supplied.encode("utf-8"), token.encode("utf-8")):
            send_message(client, {"ok": False, "error": "Authentication failed"})
            return
        if request.get("op") == "ping":
            send_message(client, {"ok": True, "service": "iPhone USB Test", "version": 1})
            return
        if request.get("op") != "connect" or request.get("port") != 443:
            raise ValueError("This test supports HTTPS on port 443 only")
        upstream = connector(request.get("host"))
        send_message(client, {"ok": True})
        acknowledged = True
        relay(client, upstream)
    except (OSError, EOFError, ValueError) as error:
        if not acknowledged:
            try:
                send_message(client, {"ok": False, "error": str(error)[:150]})
            except OSError:
                pass
    finally:
        client.close()
        if upstream is not None:
            upstream.close()


def main():
    if TOKEN == "__USB_TEST_" + "SECRET__" or len(TOKEN) != 64:
        raise SystemExit("Use the PC tool to transfer a configured copy first.")
    limit = threading.BoundedSemaphore(16)

    def worker(client):
        try:
            handle(client, TOKEN)
        finally:
            limit.release()

    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", PORT))
        listener.listen(8)
        listener.settimeout(1)
        print("USB TEST READY. Keep iSH visible and the phone unlocked.", flush=True)
        print("Turn Wi-Fi OFF in Settings; cellular ON; Personal Hotspot OFF.", flush=True)
        print("Local USB access only; secret required. Ctrl+C stops the test.", flush=True)
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
            print("Stopped.")


if __name__ == "__main__":
    main()
