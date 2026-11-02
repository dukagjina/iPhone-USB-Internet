"""One-reader, local-only Windows pipe: secrets never enter argv or a disk file."""
import ctypes as c
from ctypes import wintypes as w
import json
import secrets

import win_support as win


class Overlapped(c.Structure):
    _fields_ = [('Internal', c.c_size_t), ('InternalHigh', c.c_size_t),
                ('Offset', w.DWORD), ('OffsetHigh', w.DWORD), ('hEvent', w.HANDLE)]


class SecurityAttributes(c.Structure):
    _fields_ = [('length', w.DWORD), ('descriptor', c.c_void_p), ('inherit', w.BOOL)]


def user_sid():
    adv = c.WinDLL('advapi32', use_last_error=True)
    kernel = c.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = w.HANDLE
    adv.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, c.POINTER(w.HANDLE)]
    adv.GetTokenInformation.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.POINTER(w.DWORD)]
    adv.ConvertSidToStringSidW.argtypes = [c.c_void_p, c.POINTER(w.LPWSTR)]
    token = w.HANDLE()
    if not adv.OpenProcessToken(kernel.GetCurrentProcess(), 8, c.byref(token)):
        raise c.WinError(c.get_last_error())
    try:
        size = w.DWORD()
        adv.GetTokenInformation(token, 1, None, 0, c.byref(size))
        data = c.create_string_buffer(size.value)
        if not adv.GetTokenInformation(token, 1, data, size, c.byref(size)):
            raise c.WinError(c.get_last_error())
        sid = c.cast(data, c.POINTER(c.c_void_p))[0]
        result = w.LPWSTR()
        if not adv.ConvertSidToStringSidW(sid, c.byref(result)):
            raise c.WinError(c.get_last_error())
        try:
            return result.value
        finally:
            kernel.LocalFree.argtypes = [c.c_void_p]
            kernel.LocalFree(result)
    finally:
        win.close(token)


class SecretPipe:
    def __init__(self):
        self.name = r'\\.\pipe\iPhoneUSB-' + secrets.token_hex(24)
        self.handle = None
        self.kernel = k = c.WinDLL('kernel32', use_last_error=True)
        adv = c.WinDLL('advapi32', use_last_error=True)
        adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [w.LPCWSTR, w.DWORD, c.POINTER(c.c_void_p), c.c_void_p]
        descriptor = c.c_void_p()
        if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                'D:P(A;;GA;;;SY)(A;;GA;;;' + user_sid() + ')', 1, c.byref(descriptor), None):
            raise c.WinError(c.get_last_error())
        k.CreateNamedPipeW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.DWORD, c.POINTER(SecurityAttributes)]
        k.CreateNamedPipeW.restype = w.HANDLE
        k.CreateEventW.argtypes = [c.c_void_p, w.BOOL, w.BOOL, w.LPCWSTR]
        k.CreateEventW.restype = w.HANDLE
        k.ConnectNamedPipe.argtypes = [w.HANDLE, c.POINTER(Overlapped)]
        k.GetNamedPipeClientProcessId.argtypes = [w.HANDLE, c.POINTER(w.ULONG)]
        k.WriteFile.argtypes = [w.HANDLE, c.c_void_p, w.DWORD, c.POINTER(w.DWORD), c.POINTER(Overlapped)]
        k.GetOverlappedResult.argtypes = [w.HANDLE, c.POINTER(Overlapped), c.POINTER(w.DWORD), w.BOOL]
        k.CancelIoEx.argtypes = [w.HANDLE, c.POINTER(Overlapped)]
        k.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        try:
            attributes = SecurityAttributes(c.sizeof(SecurityAttributes), descriptor, False)
            # outbound | overlapped | first-instance; reject remote clients
            self.handle = k.CreateNamedPipeW(self.name, 0x40080002, 8, 1, 8192, 0, 0, c.byref(attributes))
            if self.handle == c.c_void_p(-1).value:
                self.handle = None
                raise c.WinError(c.get_last_error())
        finally:
            k.LocalFree.argtypes = [c.c_void_p]
            k.LocalFree(descriptor)

    def _finish(self, operation, timeout_ms):
        count = w.DWORD()
        if self.kernel.WaitForSingleObject(operation.hEvent, timeout_ms) != 0:
            self.kernel.CancelIoEx(self.handle, c.byref(operation))
            self.kernel.GetOverlappedResult(self.handle, c.byref(operation), c.byref(count), True)
            raise TimeoutError('Private configuration pipe timed out')
        if not self.kernel.GetOverlappedResult(self.handle, c.byref(operation), c.byref(count), False):
            raise c.WinError(c.get_last_error())
        return count.value

    def deliver(self, child_pid, data, timeout_ms=10000):
        if len(data) > 4096:
            raise ValueError('Private configuration is too large')
        event = self.kernel.CreateEventW(None, True, False, None)
        if not event:
            raise c.WinError(c.get_last_error())
        operation = Overlapped(hEvent=event)
        try:
            if not self.kernel.ConnectNamedPipe(self.handle, c.byref(operation)):
                error = c.get_last_error()
                if error == 997:
                    self._finish(operation, timeout_ms)
                elif error != 535:  # client connected before ConnectNamedPipe
                    raise c.WinError(error)
            client = w.ULONG()
            if not self.kernel.GetNamedPipeClientProcessId(self.handle, c.byref(client)) or client.value != child_pid:
                raise RuntimeError('Private configuration reader identity did not match the tunnel')
        finally:
            win.close(event)
        event = self.kernel.CreateEventW(None, True, False, None)
        if not event:
            raise c.WinError(c.get_last_error())
        operation = Overlapped(hEvent=event)
        buffer = c.create_string_buffer(data)
        count = w.DWORD()
        try:
            if not self.kernel.WriteFile(self.handle, buffer, len(data), c.byref(count), c.byref(operation)):
                if c.get_last_error() != 997:
                    raise c.WinError(c.get_last_error())
                count.value = self._finish(operation, timeout_ms)
            if count.value != len(data):
                raise RuntimeError('Private configuration transfer was incomplete')
        finally:
            win.close(event)
            self.close()  # EOF allows os.ReadFile in the tunnel to finish.

    def close(self):
        if self.handle:
            win.close(self.handle)
            self.handle = None


def tunnel_config(alias, port, password):
    # JSON is valid YAML and avoids escaping ambiguities.
    return json.dumps({'device': 'tun://' + alias, 'proxy': f'socks5://usb:{password}@127.0.0.1:{port}',
                       'mtu': 1280, 'udp-timeout': '60s', 'loglevel': 'silent'}).encode()
