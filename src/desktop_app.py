"""Normal desktop behavior; no elevation, phone checks or routing on launch."""
import os
import sys
import time
import ctypes
from ctypes import wintypes
from pathlib import Path

import win32api
import win32con
import win32event
import win32gui
import win32process

APP_MUTEX = r'Local\iPhoneUSBInternet.App.v1'


def executable_path(process):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    query = kernel.QueryFullProcessImageNameW
    query.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    query.restype = wintypes.BOOL
    buffer = ctypes.create_unicode_buffer(32768)
    length = wintypes.DWORD(len(buffer))
    if not query(int(process), 0, buffer, ctypes.byref(length)):
        raise ctypes.WinError(ctypes.get_last_error())
    return buffer.value


class SingleInstance:
    def __init__(self):
        self.handle = win32event.CreateMutex(None, False, APP_MUTEX)
        self.is_first = win32api.GetLastError() != 183

    def activate_existing(self):
        # Only raise a window belonging to this exact executable. A duplicate
        # launch never sends network actions to the existing instance.
        executable = os.path.normcase(str(Path(sys.executable).resolve()))
        installed = os.path.normcase(str(Path(os.environ.get('LOCALAPPDATA', '')) /
                                       'Programs/iPhone USB Internet/iPhone USB Internet.exe'))
        for _ in range(20):
            found = []
            def visit(hwnd, _):
                if not win32gui.IsWindowVisible(hwnd) or win32gui.GetWindowText(hwnd) != 'iPhone USB Internet':
                    return
                process = None
                try:
                    _, pid = win32process.GetWindowThreadProcessId(hwnd)
                    process = win32api.OpenProcess(0x1000, False, pid)
                    candidate = executable_path(process)
                    if os.path.normcase(candidate) in (executable, installed):
                        found.append(hwnd)
                except OSError:
                    pass
                finally:
                    if process:
                        process.Close()
            win32gui.EnumWindows(visit, None)
            if found:
                try:
                    win32gui.ShowWindow(found[0], win32con.SW_RESTORE)
                    win32gui.SetForegroundWindow(found[0])
                except OSError:
                    pass
                return
            time.sleep(.1)

    def close(self):
        if self.handle:
            self.handle.Close()
            self.handle = None

