"""Native Windows behavior for PC Center's custom frameless window.

pywebview's WinForms backend removes the standard top-level window styles when
``frameless=True``.  The HTML header stays clean, but Windows then stops treating
the form as a normal minimizable taskbar window.  Restoring the behavioral
styles (without forcing a non-client frame recalculation) keeps the custom
chrome while returning minimize/restore to the shell and DWM.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes


GWL_STYLE = -16
WS_CAPTION = 0x00C00000
WS_SYSMENU = 0x00080000
WS_THICKFRAME = 0x00040000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000

WM_SYSCOMMAND = 0x0112
SC_MINIMIZE = 0xF020
DWMWA_TRANSITIONS_FORCEDISABLED = 3
APP_USER_MODEL_ID = 'Zuyis.iPhoneUSBInternet'

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SWP_SHOWWINDOW = 0x0040
FRAME_REFRESH_FLAGS = (
    SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED
)
MOVE_FLAGS = SWP_NOSIZE | SWP_NOZORDER | SWP_SHOWWINDOW

RDW_INVALIDATE = 0x0001
RDW_ERASE = 0x0004
RDW_ALLCHILDREN = 0x0080
RDW_UPDATENOW = 0x0100
REDRAW_FLAGS = RDW_INVALIDATE | RDW_ERASE | RDW_ALLCHILDREN | RDW_UPDATENOW

REQUIRED_STYLES = WS_SYSMENU | WS_MINIMIZEBOX


_user32 = ctypes.windll.user32
_dwmapi = ctypes.windll.dwmapi
_shell32 = ctypes.windll.shell32

_get_window_long = _user32.GetWindowLongPtrW
_get_window_long.argtypes = [wintypes.HWND, ctypes.c_int]
_get_window_long.restype = ctypes.c_ssize_t

_set_window_long = _user32.SetWindowLongPtrW
_set_window_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
_set_window_long.restype = ctypes.c_ssize_t

_post_message = _user32.PostMessageW
_post_message.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_post_message.restype = wintypes.BOOL

_set_window_pos = _user32.SetWindowPos
_set_window_pos.argtypes = [
    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, wintypes.UINT,
]
_set_window_pos.restype = wintypes.BOOL

_redraw_window = _user32.RedrawWindow
_redraw_window.argtypes = [
    wintypes.HWND, ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT,
]
_redraw_window.restype = wintypes.BOOL

_dwm_set_window_attribute = _dwmapi.DwmSetWindowAttribute
_dwm_set_window_attribute.argtypes = [
    wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD
]
_dwm_set_window_attribute.restype = ctypes.c_long

_set_process_app_id = _shell32.SetCurrentProcessExplicitAppUserModelID
_set_process_app_id.argtypes = [wintypes.LPCWSTR]
_set_process_app_id.restype = ctypes.c_long


def configure_process():
    """Give the process the stable identity used by installed shortcuts."""
    try:
        return int(_set_process_app_id(APP_USER_MODEL_ID)) == 0
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def _window_handle(window):
    """Return an integer HWND from a live pywebview WinForms window."""
    native = getattr(window, "native", None)
    handle = getattr(native, "Handle", None)
    if handle is None:
        return 0
    try:
        if hasattr(handle, "ToInt64"):
            return int(handle.ToInt64())
        if hasattr(handle, "ToInt32"):
            return int(handle.ToInt32())
        return int(handle)
    except (TypeError, ValueError, OverflowError):
        return 0


def native_style(style):
    """Return the behavioral style used by PC Center's frameless form."""
    # WS_CAPTION must remain absent. Windows recalculates the non-client area
    # after a minimize/restore cycle and would otherwise reveal a second,
    # native title bar above PC Center's HTML header. WS_SYSMENU and
    # WS_MINIMIZEBOX are sufficient for native minimize/taskbar behavior.
    # Maximize and the resize frame remain unavailable because the compact app
    # exposes neither control.
    return (
        (int(style) | REQUIRED_STYLES)
        & ~WS_CAPTION
        & ~WS_MAXIMIZEBOX
        & ~WS_THICKFRAME
    )


def configure_window(window):
    """Restore shell/DWM behavior without adding a visible Windows title bar."""
    hwnd = _window_handle(window)
    if not hwnd:
        return False
    try:
        current = int(_get_window_long(hwnd, GWL_STYLE))
        desired = native_style(current)
        if desired != current:
            ctypes.set_last_error(0)
            previous = int(_set_window_long(hwnd, GWL_STYLE, desired))
            if previous == 0 and ctypes.get_last_error():
                return False

        # Frameless WebView2 windows need an explicit non-client recalculation.
        # Without it Windows can leave the WebView surface unpainted after a
        # minimize/restore cycle even though the process and HWND remain alive.
        # The caption bits are already absent, so FRAMECHANGED cannot reveal a
        # native title bar; it only makes the client geometry authoritative.
        if not _set_window_pos(hwnd, 0, 0, 0, 0, 0, FRAME_REFRESH_FLAGS):
            return False
        _redraw_window(hwnd, None, None, REDRAW_FLAGS)

        # This is normally FALSE already. Set it explicitly because the window
        # is custom-framed and smooth native minimize/restore transitions are
        # part of the PC Center contract.
        transitions_disabled = wintypes.BOOL(False)
        _dwm_set_window_attribute(
            hwnd,
            DWMWA_TRANSITIONS_FORCEDISABLED,
            ctypes.byref(transitions_disabled),
            ctypes.sizeof(transitions_disabled),
        )
        applied = int(_get_window_long(hwnd, GWL_STYLE))
        return (applied & REQUIRED_STYLES) == REQUIRED_STYLES
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def minimize_window(window):
    """Post the same native system command used by normal Windows chrome."""
    hwnd = _window_handle(window)
    if not hwnd:
        return False
    try:
        return bool(_post_message(hwnd, WM_SYSCOMMAND, SC_MINIMIZE, 0))
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def _repaint_webview(native):
    """Run on the WinForms UI thread after restoration/activation settles."""
    if native.IsDisposed or native.WindowState.ToString() == 'Minimized':
        return
    view = getattr(native, 'webview', None)
    if view is None or view.IsDisposed:
        return
    # Native chrome restoration can leave the embedded compositor with its
    # minimized geometry even while the parent and DOM are alive. Reapply its
    # client bounds and visibility without navigating/reloading the document.
    native.PerformLayout()
    view.Visible = False
    view.Bounds = native.ClientRectangle
    view.Visible = True
    view.Invalidate(True)
    view.Refresh()
    native.Invalidate(True)
    native.Update()


def refresh_webview(window):
    native = getattr(window, 'native', None)
    if native is None:
        return False
    try:
        from System import Action
        native.BeginInvoke(Action(lambda: _repaint_webview(native)))
        return True
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def install_restore_refresh(window):
    """Also handle taskbar activation paths that don't emit restored."""
    native = getattr(window, 'native', None)
    if native is None:
        return
    native.Activated += lambda sender, event: refresh_webview(window)


def restore_window(window):
    configure_window(window)
    refresh_webview(window)


def move_window(window, x, y):
    """Move a frameless window using logical screen coordinates.

    pywebview 6.1 currently forwards ``None`` for the unused width and height
    arguments of WinForms ``SetWindowPos``.  The ctypes binding requires
    integers, so its built-in drag region fails at runtime.  This equivalent
    call supplies zeros and preserves pywebview's display-scale conversion.
    """
    hwnd = _window_handle(window)
    if not hwnd:
        return False
    try:
        scale = float(getattr(getattr(window, "native", None), "_scale", 1) or 1)
        x_physical = round(float(x) * scale)
        y_physical = round(float(y) * scale)
        return bool(
            _set_window_pos(
                hwnd, 0, x_physical, y_physical, 0, 0, MOVE_FLAGS
            )
        )
    except (AttributeError, OSError, TypeError, ValueError, OverflowError):
        return False
