"""The DaVinci Director window: a native WebView2 window (pywebview), no browser UI.

Runs as its own process (pywebview needs the main thread), so the background app
stays alive when the window is closed:

    pythonw -m davinci_director.window http://127.0.0.1:8765/
"""

import ctypes
import os
import sys
import threading
import urllib.request

ICON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "icon.ico")


class Api:
    """Called from the page: window.pywebview.api.<name>()."""

    def play(self, seconds):
        from davinci_director import resolve_window
        if not resolve_window.find_resolve_window():
            return {"ok": False, "error": "DaVinci Resolve window not found"}
        threading.Thread(target=resolve_window.play_for, args=(float(seconds),), daemon=True).start()
        return {"ok": True}

    def space(self, restore=True):
        """Space in Resolve (the page decides when: it knows from the playhead whether Resolve plays)."""
        from davinci_director import resolve_window
        return {"ok": bool(resolve_window.press_in_resolve(bool(restore)))}


def _set_icon(window):
    """The WebView2 (WinForms) window takes its title-bar / taskbar icon from the form."""
    import clr  # noqa: F401  pythonnet must be loaded before System.* can be imported
    from System import Action
    from System.Drawing import Icon
    form = window.native
    form.Invoke(Action(lambda: setattr(form, "Icon", Icon(ICON))))


def main(url):
    import webview
    webview.settings["ALLOW_DOWNLOADS"] = True  # exports (JSON, SRT, zip)
    webview.settings["OPEN_DEVTOOLS_IN_DEBUG"] = False
    # its own taskbar entry and icon instead of being grouped under pythonw.exe
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("DaVinciDirector.Panel")
    w = webview.create_window("DaVinci Director", url, width=1500, height=920, min_size=(1000, 600),
                              background_color="#1e1e22", text_select=True, js_api=Api())
    w.events.shown += lambda: _set_icon(w)
    # private mode: a fresh WebView2 data folder per window, so a previous window that is
    # still shutting down can never lock it ("resource in use"); the panel keeps no browser state
    webview.start(gui="edgechromium", private_mode=True)
    _closed(url)
    return 0


def _closed(url):
    """Tell the app the user closed the window, so it stops all work (transcription, speaker detection,
    downloads) instead of continuing unseen in the background."""
    req = urllib.request.Request(url.rstrip("/") + "/api/window_closed", data=b"{}",
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=3).read()
    except OSError:
        pass  # the app is gone already; otherwise it notices the lost connection itself


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765/"))
