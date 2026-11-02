"""Small Windows APIs. Importing this module does not change networking."""
import ctypes as c
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import subprocess
import sys
import time

kernel = c.WinDLL('kernel32', use_last_error=True)
kernel.CloseHandle.argtypes = [w.HANDLE]
kernel.CloseHandle.restype = w.BOOL
kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
kernel.WaitForSingleObject.restype = w.DWORD
kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
kernel.OpenProcess.restype = w.HANDLE


def close(handle):
    if handle:
        kernel.CloseHandle(handle)


def alive(handle):
    return bool(handle) and kernel.WaitForSingleObject(handle, 0) == 258


def watch_process(pid):
    handle = kernel.OpenProcess(0x100000, False, pid)
    if not handle:
        raise c.WinError(c.get_last_error())
    return handle


def admin():
    return bool(c.windll.shell32.IsUserAnAdmin())


def connection_active():
    kernel.OpenMutexW.argtypes = [w.DWORD, w.BOOL, w.LPCWSTR]
    kernel.OpenMutexW.restype = w.HANDLE
    handle = kernel.OpenMutexW(0x100000, False, 'Global\\iPhoneUSBInternet.Connection.v1')
    if handle:
        close(handle)
        return True
    # Access denied is not evidence that no other user's connection exists.
    return c.get_last_error() != 2


class Blob(c.Structure):
    _fields_ = [('size', w.DWORD), ('data', c.POINTER(c.c_ubyte))]


def protect(data, decrypt=False):
    crypt = c.WinDLL('crypt32', use_last_error=True)
    buffer = (c.c_ubyte * len(data)).from_buffer_copy(data)
    source = Blob(len(data), buffer)
    target = Blob()
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [c.POINTER(Blob), c.c_void_p, c.c_void_p, c.c_void_p,
                   c.c_void_p, w.DWORD, c.POINTER(Blob)]
    fn.restype = w.BOOL
    # Current Windows user scope. Never machine-wide and never display DPAPI UI.
    if not fn(c.byref(source), None, None, None, None, 1, c.byref(target)):
        raise c.WinError(c.get_last_error())
    try:
        return c.string_at(target.data, target.size)
    finally:
        kernel.LocalFree.argtypes = [c.c_void_p]
        kernel.LocalFree(target.data)


class ShellInfo(c.Structure):
    _fields_ = [('cbSize', w.DWORD), ('fMask', w.ULONG), ('hwnd', w.HWND),
                ('lpVerb', w.LPCWSTR), ('lpFile', w.LPCWSTR),
                ('lpParameters', w.LPCWSTR), ('lpDirectory', w.LPCWSTR),
                ('nShow', c.c_int), ('hInstApp', w.HINSTANCE),
                ('lpIDList', c.c_void_p), ('lpClass', w.LPCWSTR),
                ('hkeyClass', w.HKEY), ('dwHotKey', w.DWORD),
                ('hIcon', w.HANDLE), ('hProcess', w.HANDLE)]


def elevate(arguments):
    args = arguments if getattr(sys, 'frozen', False) else [str(Path(__file__).with_name('app.py')), *arguments]
    info = ShellInfo()
    info.cbSize = c.sizeof(info)
    info.fMask = 0x40 | 0x100 | 0x400
    info.lpVerb = 'runas'
    info.lpFile = sys.executable
    info.lpParameters = subprocess.list2cmdline(args)
    info.lpDirectory = str(Path(sys.executable).parent)
    info.nShow = 0
    shell = c.WinDLL('shell32', use_last_error=True)
    shell.ShellExecuteExW.argtypes = [c.POINTER(ShellInfo)]
    shell.ShellExecuteExW.restype = w.BOOL
    if not shell.ShellExecuteExW(c.byref(info)):
        if c.get_last_error() == 1223:
            raise RuntimeError('Administrator approval was cancelled. Nothing was changed.')
        raise c.WinError(c.get_last_error())
    if not info.hProcess:
        raise RuntimeError('Windows did not return a connection process handle.')
    return info.hProcess


class BasicLimit(c.Structure):
    _fields_ = [('PerProcessUserTimeLimit', c.c_longlong), ('PerJobUserTimeLimit', c.c_longlong),
                ('LimitFlags', w.DWORD), ('MinimumWorkingSetSize', c.c_size_t),
                ('MaximumWorkingSetSize', c.c_size_t), ('ActiveProcessLimit', w.DWORD),
                ('Affinity', c.c_size_t), ('PriorityClass', w.DWORD), ('SchedulingClass', w.DWORD)]


class IO(c.Structure):
    _fields_ = [(name, c.c_ulonglong) for name in
                ('ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
                 'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]


class ExtendedLimit(c.Structure):
    _fields_ = [('BasicLimitInformation', BasicLimit), ('IoInfo', IO),
                ('ProcessMemoryLimit', c.c_size_t), ('JobMemoryLimit', c.c_size_t),
                ('PeakProcessMemoryUsed', c.c_size_t), ('PeakJobMemoryUsed', c.c_size_t)]


class Job:
    """Own children so a crash also terminates the tunnel; never kills other apps."""
    def __init__(self):
        kernel.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
        kernel.CreateJobObjectW.restype = w.HANDLE
        kernel.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
        kernel.SetInformationJobObject.restype = w.BOOL
        kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        kernel.AssignProcessToJobObject.restype = w.BOOL
        self.handle = kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise c.WinError(c.get_last_error())
        limits = ExtendedLimit()
        limits.BasicLimitInformation.LimitFlags = 0x2000
        if not kernel.SetInformationJobObject(self.handle, 9, c.byref(limits), c.sizeof(limits)):
            self.close()
            raise c.WinError(c.get_last_error())

    def add(self, process):
        if not kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            process.kill()
            process.wait(5)
            raise c.WinError(c.get_last_error())

    def close(self):
        close(self.handle)
        self.handle = None


def read_status(path):
    """Allow the worker to replace status.json while the GUI is reading it."""
    kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
    kernel.CreateFileW.restype = w.HANDLE
    kernel.ReadFile.argtypes = [w.HANDLE, c.c_void_p, w.DWORD, c.POINTER(w.DWORD), c.c_void_p]
    handle = kernel.CreateFileW(str(path), 0x80000000, 7, None, 3, 0, None)
    if handle == c.c_void_p(-1).value:
        raise c.WinError(c.get_last_error())
    try:
        buffer = c.create_string_buffer(65537)
        count = w.DWORD()
        if not kernel.ReadFile(handle, buffer, len(buffer), c.byref(count), None):
            raise c.WinError(c.get_last_error())
        if count.value > 65536:
            raise ValueError('Oversized status file')
        return json.loads(buffer.raw[:count.value].decode('utf-8'))
    finally:
        close(handle)


def atomic_json(path, data):
    path = Path(path)
    temporary = path.with_suffix('.new')
    temporary.write_text(json.dumps(data), encoding='utf-8')
    # Windows readers/AV can briefly deny replacement. Bound the retries so
    # telemetry never holds up cancellation or the routing worker for seconds.
    for attempt in range(4):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 3:
                raise
            time.sleep(.015 * (attempt + 1))


class StatusPublisher:
    """Best-effort telemetry only. Network safety decisions never depend on it."""
    def __init__(self, path):
        self.path = path
        self.failed_updates = 0

    def publish(self, data):
        data = dict(data, status_write_failures=self.failed_updates)
        try:
            atomic_json(self.path, data)
            return True
        except OSError:
            # GUI explicitly marks stale status as unknown. It may still request
            # Disconnect through its separate stop file; health checks keep running.
            self.failed_updates += 1
            return False
