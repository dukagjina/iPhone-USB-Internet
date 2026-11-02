"""Windows USB test client. No packages, global proxy changes, or LAN server.

Apple Devices supplies the local USB multiplexer service. This client speaks its
documented plist protocol directly; it only selects physical USB connections.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import queue
import secrets
import socket
import socketserver
import ssl
import struct
import subprocess
import threading
import time
import urllib.parse

from phone_relay import recv_exact, send_message, receive_message, relay

ROOT = Path(__file__).resolve().parent
STATE = ROOT / ".local"
PHONE_PORT = 18765
TRANSFER_PORT = 18764
BOOTSTRAP = '''python3 -c 'import socket,hashlib; s=socket.socket(); s.bind(("127.0.0.1",18764)); s.listen(1); print("Ready for PC transfer",flush=True); c,_=s.accept(); c.settimeout(30); b=c.makefile("rb").read(65537); assert 0<len(b)<=65536; open("/root/usb_test.py","wb").write(b); c.sendall(hashlib.sha256(b).hexdigest().encode()); c.close(); s.close(); print("Saved. Run: python3 /root/usb_test.py")' '''.strip()


def load_config():
    STATE.mkdir(exist_ok=True)
    path = STATE / "pairing.json"
    if not path.exists():
        with path.open("x", encoding="utf-8") as f:
            json.dump({"token": secrets.token_hex(32)}, f)
    config = json.loads(path.read_text(encoding="utf-8"))
    if len(config.get("token", "")) != 64:
        raise ValueError("Invalid pairing file; do not edit .local/pairing.json")
    return config


def usb_request(message, address=("127.0.0.1", 27015)):
    connection = socket.create_connection(address, timeout=5)
    try:
        data = {"ClientVersionString": "USBTest-0.1", "ProgName": "USBTest",
                "kLibUSBMuxVersion": 3, **message}
        payload = plistlib.dumps(data)
        connection.sendall(struct.pack("<IIII", len(payload) + 16, 1, 8, 1) + payload)
        size, version, kind, tag = struct.unpack("<IIII", recv_exact(connection, 16))
        if version != 1 or kind != 8 or tag != 1 or not 16 <= size <= 1024 * 1024:
            raise ValueError("Unexpected response from Apple USB service")
        response = plistlib.loads(recv_exact(connection, size - 16))
        return connection, response
    except BaseException:
        connection.close()
        raise


def list_devices():
    connection, response = usb_request({"MessageType": "ListDevices"})
    connection.close()
    if "DeviceList" not in response:
        raise RuntimeError("Apple USB service could not list devices")
    return [d for d in response["DeviceList"]
            if d.get("Properties", {}).get("ConnectionType") == "USB"]


def select_device(config):
    devices = list_devices()
    if config.get("serial"):
        devices = [d for d in devices if d["Properties"].get("SerialNumber") == config["serial"]]
    if len(devices) != 1:
        raise RuntimeError("Connect exactly one iPhone by USB, unlock it, and tap Trust. "
                           "If already paired here, reconnect that same iPhone.")
    return devices[0]


def connect_phone(config, port):
    device = select_device(config)
    connection, response = usb_request({"MessageType": "Connect",
        "DeviceID": device["DeviceID"], "PortNumber": socket.htons(port)})
    if response.get("Number") != 0:
        connection.close()
        raise RuntimeError("iPhone refused the test port. Keep iSH open and run the "
                           "phone command for this step. Apple code: " + str(response.get("Number")))
    connection.settimeout(20)
    return connection


def phone_operation(config, operation, host=None):
    connection = connect_phone(config, PHONE_PORT)
    try:
        send_message(connection, {"token": config["token"], "op": operation,
                                  "host": host, "port": 443})
        response = receive_message(connection)
        if not response.get("ok"):
            raise RuntimeError(response.get("error", "Phone operation failed"))
        return connection, response
    except BaseException:
        connection.close()
        raise


def transfer(config):
    device = select_device(config)
    template = (ROOT / "phone_relay.py").read_text(encoding="utf-8")
    content = template.replace('TOKEN = "__USB_TEST_SECRET__"',
                               'TOKEN = ' + repr(config["token"])).encode("utf-8")
    if len(content) > 65536:
        raise ValueError("Phone source exceeds transfer limit")
    with connect_phone(config, TRANSFER_PORT) as connection:
        connection.sendall(content)
        connection.shutdown(socket.SHUT_WR)
        digest = recv_exact(connection, 64).decode("ascii")
    if digest != hashlib.sha256(content).hexdigest():
        raise RuntimeError("Transfer checksum mismatch. Do not run the phone file.")
    config["serial"] = device["Properties"]["SerialNumber"]
    (STATE / "pairing.json").write_text(json.dumps(config), encoding="utf-8")
    return "Phone file transferred and SHA-256 verified. On iPhone run: python3 /root/usb_test.py"


def check(config):
    connection, response = phone_operation(config, "ping")
    connection.close()
    return "USB relay authenticated successfully. " + response["service"]


def internet_test(config):
    connection, _ = phone_operation(config, "connect", "example.com")
    try:
        with ssl.create_default_context().wrap_socket(connection, server_hostname="example.com") as tls:
            tls.sendall(b"GET / HTTP/1.1\r\nHost: example.com\r\nConnection: close\r\n\r\n")
            body = bytearray()
            while len(body) < 65536:
                part = tls.recv(min(4096, 65536-len(body)))
                if not part:
                    break
                body.extend(part)
        if not bytes(body).startswith(b"HTTP/1.1 200") or b"Example Domain" not in body:
            raise RuntimeError("Unexpected website response; not marked successful")
        return ("PASS: HTTPS page fetched through the iPhone over USB; TLS certificate verified. "
                "Cellular-only is proven only when iPhone Wi-Fi is OFF and Personal Hotspot is OFF.")
    finally:
        connection.close()


class ProxyHandler(socketserver.BaseRequestHandler):
    def handle(self):
        upstream = None
        connected = False
        try:
            self.request.settimeout(15)
            headers = bytearray()
            while not headers.endswith(b"\r\n\r\n"):
                if len(headers) >= 16384:
                    raise ValueError("Request headers too large")
                headers.extend(recv_exact(self.request, 1))
            first = headers.split(b"\r\n", 1)[0].decode("ascii")
            method, authority, version = first.split(" ")
            if method != "CONNECT":
                self.request.sendall(b"HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
                return
            parsed = urllib.parse.urlsplit("//" + authority)
            if parsed.port != 443 or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("HTTPS port 443 only")
            upstream, _ = phone_operation(self.server.config, "connect", parsed.hostname)
            self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            connected = True
            relay(self.request, upstream)
        except (OSError, EOFError, ValueError, RuntimeError):
            if not connected:
                try:
                    self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
                except OSError:
                    pass
        finally:
            if upstream:
                upstream.close()


class ProxyServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, *args, **kwargs):
        self.active = set()
        self.active_lock = threading.Lock()
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        with self.active_lock:
            if len(self.active) >= 16:
                request.close()
                return
            self.active.add(request)
        super().process_request(request, client_address)

    def shutdown_request(self, request):
        with self.active_lock:
            self.active.discard(request)
        super().shutdown_request(request)

    def stop(self):
        self.shutdown()
        self.server_close()
        with self.active_lock:
            sockets = list(self.active)
        for connection in sockets:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()


def start_browser(config):
    check(config)
    edge = next((p for p in [Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft/Edge/Application/msedge.exe",
                            Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft/Edge/Application/msedge.exe"]
                 if p.is_file()), None)
    if edge is None:
        raise RuntimeError("Microsoft Edge was not found")
    server = ProxyServer(("127.0.0.1", 0), ProxyHandler)
    server.config = config
    threading.Thread(target=server.serve_forever, daemon=True).start()
    profile = STATE / ("browser-" + secrets.token_hex(6))
    try:
        subprocess.Popen([str(edge), "--user-data-dir=" + str(profile), "--no-first-run",
            "--no-default-browser-check", "--disable-quic", "--disable-background-networking",
            "--disable-sync", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            "--proxy-server=http://127.0.0.1:" + str(server.server_address[1]),
            "--new-window", "https://example.com"])
    except BaseException:
        server.stop()
        raise
    return server


def gui():
    import tkinter as tk
    from tkinter import ttk
    config = load_config()
    events = queue.Queue()
    browser_servers = []
    root = tk.Tk()
    root.title("iPhone USB Test")
    root.geometry("780x540")
    root.minsize(660, 440)
    ttk.Label(root, text="iPhone USB Test", font=("Segoe UI", 20)).pack(anchor="w", padx=20, pady=(18,8))
    ttk.Label(root, text="Free prototype • USB cable • Keep iSH open on the phone").pack(anchor="w", padx=20)
    ttk.Label(root, text="Phone: cellular ON, Wi-Fi OFF in Settings, Personal Hotspot OFF.").pack(anchor="w", padx=20, pady=8)
    log = tk.Text(root, wrap="word", height=13, font=("Consolas", 10))
    log.pack(fill="both", expand=True, padx=20, pady=10)
    buttons = []

    def action(fn):
        for b in buttons:
            b.configure(state="disabled")
        def work():
            try:
                events.put(str(fn()))
            except Exception as error:
                events.put("NOT CONNECTED: " + str(error))
            finally:
                events.put(None)
        threading.Thread(target=work, daemon=True).start()

    def poll():
        while not events.empty():
            message = events.get()
            if message is None:
                for b in buttons:
                    b.configure(state="normal")
            else:
                log.insert("end", message + "\n\n")
                log.see("end")
        root.after(150, poll)

    def browser():
        browser_servers.append(start_browser(config))
        return "Opened separate TEST browser. HTTPS pages only; your normal browser settings are unchanged."

    def stop():
        for server in browser_servers:
            server.stop()
        browser_servers.clear()
        return "Test browser relay stopped. Close its browser window; stop iSH using Ctrl+C."

    row = ttk.Frame(root)
    row.pack(fill="x", padx=20, pady=8)
    for title, fn in [("Check USB", lambda: "Physical USB devices found: " + str(len(list_devices()))),
                      ("Send phone file", lambda: transfer(config)),
                      ("Test internet", lambda: internet_test(config)),
                      ("Open test browser", browser), ("Stop relay", stop)]:
        button = ttk.Button(row, text=title, command=lambda fn=fn: action(fn))
        button.pack(side="left", padx=(0,5))
        buttons.append(button)
    ttk.Label(root, text="No global proxy, routes, firewall exceptions, or startup tasks are added.").pack(anchor="w", padx=20, pady=(0,16))
    events.put("1. In iSH run: apk add python3\n2. Run the transfer command from this chat.\n3. Click Send phone file.\n4. In iSH run: python3 /root/usb_test.py\n5. Turn phone Wi-Fi off, then click Test internet.")
    root.after(100, poll)
    root.mainloop()
    for server in browser_servers:
        server.stop()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", nargs="?", default="gui",
                        choices=["gui", "status", "transfer", "check", "test", "bootstrap"])
    args = parser.parse_args()
    if args.action == "gui":
        gui()
        return
    if args.action == "bootstrap":
        print(BOOTSTRAP)
        return
    config = load_config()
    if args.action == "status":
        devices = list_devices()
        print("Physical USB devices:", len(devices))
        for device in devices:
            print("USB device ID:", device["DeviceID"])
    elif args.action == "transfer":
        print(transfer(config))
    elif args.action == "check":
        print(check(config))
    else:
        print(internet_test(config))


if __name__ == "__main__":
    try:
        main()
    except (OSError, EOFError, ValueError, RuntimeError) as error:
        raise SystemExit("Not connected: " + str(error))
