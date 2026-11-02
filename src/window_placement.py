"""Persist normal window bounds in Windows workspace coordinates."""
from __future__ import annotations

import json
import os
import tempfile

import win32api
import win32con
import win32gui

from window_chrome import _window_handle


class WindowPlacement:
    def __init__(self, app_name, path=None):
        self.path = path or os.path.join(
            os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
            app_name, "window-placement.json",
        )

    @staticmethod
    def _bounds(value):
        if (not isinstance(value, (list, tuple)) or len(value) != 4
                or any(type(item) is not int or abs(item) > 100000 for item in value)):
            raise ValueError("Invalid window bounds")
        left, top, right, bottom = value
        if not 200 <= right - left <= 16384 or not 200 <= bottom - top <= 16384:
            raise ValueError("Invalid window size")
        return tuple(value)

    def restore(self, window, *, before_show=False):
        """Restore bounds, optionally while the native window is still hidden."""
        try:
            hwnd = _window_handle(window)
            if not hwnd:
                return False
            with open(self.path, encoding="utf-8") as handle:
                value = json.load(handle)
            bounds = self._bounds(value["normal_bounds"])
            # The matching Windows APIs use workspace coordinates, preserve
            # negative monitor positions, and relocate disconnected monitors.
            # Do not reveal a startup window here: the backend's first Show
            # must happen only after positioning is complete.
            command = win32con.SW_HIDE if before_show else win32con.SW_SHOWNORMAL
            win32gui.SetWindowPlacement(
                hwnd, (0, command, (-1, -1), (-1, -1), bounds)
            )
            if before_show:
                # WinForms otherwise applies CenterScreen on its first Show,
                # overriding the native placement we just restored. Derive
                # the enum from the live form after the backend loads .NET.
                native = window.native
                native.StartPosition = type(native.StartPosition).Manual
            return True
        except (OSError, ValueError, TypeError, KeyError, win32gui.error):
            return False

    def fit_to_work_area(self, window, *, center=False):
        """Keep the complete window reachable, without exposing a startup frame.

        Read screen coordinates AFTER Windows restores workspace placement;
        never feed saved workspace coordinates to SetWindowPos. This also
        handles a smaller replacement monitor and taskbars on any edge.
        """
        try:
            hwnd = _window_handle(window)
            if not hwnd:
                return False
            native = window.native
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            monitor = win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST)
            x1, y1, x2, y2 = win32api.GetMonitorInfo(monitor)["Work"]
            available_width, available_height = x2 - x1, y2 - y1
            if available_width <= 0 or available_height <= 0:
                return False
            width = min(right - left, available_width)
            height = min(bottom - top, available_height)
            if center:
                left = x1 + (available_width - width) // 2
                top = y1 + (available_height - height) // 2
            else:
                left = min(max(left, x1), x2 - width)
                top = min(max(top, y1), y2 - height)
            # WinForms enforces its minimum during WM_WINDOWPOSCHANGING.
            # Permit the actual screen size on low-resolution/high-DPI displays.
            minimum = native.MinimumSize
            if minimum.Width > available_width or minimum.Height > available_height:
                native.MinimumSize = type(minimum)(
                    min(minimum.Width, available_width),
                    min(minimum.Height, available_height),
                )
            win32gui.SetWindowPos(
                hwnd, 0, left, top, width, height,
                win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE,
            )
            native.StartPosition = type(native.StartPosition).Manual
            return True
        except (AttributeError, OSError, ValueError, TypeError, KeyError, win32gui.error):
            return False

    def save(self, window):
        temporary = None
        try:
            hwnd = _window_handle(window)
            if not hwnd:
                return
            bounds = self._bounds(win32gui.GetWindowPlacement(hwnd)[4])
            directory = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(directory, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=directory, delete=False
            ) as handle:
                temporary = handle.name
                json.dump({"schema": 1, "normal_bounds": bounds}, handle)
                handle.write("\n")
            os.replace(temporary, self.path)
            temporary = None
        except (OSError, ValueError, TypeError, win32gui.error):
            # Failure to save an optional preference must never prevent exit.
            pass
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
