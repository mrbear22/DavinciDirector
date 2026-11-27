"""Drive DaVinci Resolve's playback from outside (Windows).

The Resolve scripting API can move the playhead and set In/Out marks but has no
"play" command.  So the panel does what an editor does: brings the Resolve window
to the front and presses Space (layout-independent key); the app presses Space
again when the playhead reaches the end of the previewed part.
"""

import ctypes
import os
import time

if os.name == "nt":
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

VK_SPACE = 0x20
VK_MENU = 0x12
KEYEVENTF_KEYUP = 0x0002


def _process_name(pid):
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
        return ""
    finally:
        kernel32.CloseHandle(h)


def find_resolve_window():
    """Largest visible top-level window of Resolve.exe."""
    if os.name != "nt":
        return None
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if _process_name(pid.value) == "resolve.exe":
            r = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            found.append(((r.right - r.left) * (r.bottom - r.top), hwnd))
        return True

    user32.EnumWindows(cb, 0)
    return max(found)[1] if found else None


def resolve_foreground():
    if os.name != "nt":
        return False
    fg = user32.GetForegroundWindow()
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
    return _process_name(pid.value) == "resolve.exe"


def focus_resolve():
    hwnd = find_resolve_window()
    if not hwnd:
        return False
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    # a tap of Alt lets a process that is not in front switch the foreground window
    user32.keybd_event(VK_MENU, 0, 0, 0)
    user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
    user32.SetForegroundWindow(hwnd)
    for _ in range(20):
        if resolve_foreground():
            time.sleep(0.08)  # let Resolve take keyboard focus
            return True
        time.sleep(0.03)
    return resolve_foreground()


def press_in_resolve(restore=True):
    """Press Space in Resolve (start or stop playback), bringing it forward for the keypress.  With
    ``restore`` the window that was in front before (e.g. this panel) gets the focus back.
    Returns False if Resolve's window was not found."""
    prev = user32.GetForegroundWindow() if os.name == "nt" else None
    was_resolve = resolve_foreground()
    if not focus_resolve():
        return False
    press_space()
    if restore and prev and not was_resolve:
        time.sleep(0.05)
        user32.keybd_event(VK_MENU, 0, 0, 0)  # same trick as focus_resolve: allowed to switch back
        user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
        user32.SetForegroundWindow(prev)
    return True


def play_for(seconds):
    """Bring Resolve forward, play, stop after ``seconds`` (unless the editor already
    switched away or stopped).  Returns False if Resolve's window was not found."""
    if not focus_resolve():
        return False
    t0 = time.perf_counter()
    press_space()
    end = t0 + max(0.2, seconds)
    while time.perf_counter() < end - 0.005:
        time.sleep(min(0.01, max(0.0, end - time.perf_counter())))
    if resolve_foreground():
        press_space()
    return True


def press_space():
    user32.keybd_event(VK_SPACE, 0x39, 0, 0)
    time.sleep(0.02)
    user32.keybd_event(VK_SPACE, 0x39, KEYEVENTF_KEYUP, 0)
