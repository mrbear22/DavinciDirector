"""DaVinci Director app: a hidden process started at Windows login.

Resolve's script sandbox cannot start programs, so the app waits in the background and
opens its window when Workspace > Scripts > DaVinci Director runs in Resolve.

    python -m davinci_director [--port N] [--bridge-dir DIR] [--no-script-sync]
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

from . import __version__
from .engine import Engine
from .link import FileBridgeLink, default_bridge_dir
from .server import App, make_server
from .storage import Settings, base_dir

DEFAULT_PORT = 8765
NO_CONSOLE = 0x08000000  # CREATE_NO_WINDOW
DETACHED = 0x00000008  # DETACHED_PROCESS
CLOSED_GRACE = 20  # s without a connected window before the app treats it as closed (a reload is shorter)

_window_proc = None


def running_on(port):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/ping" % port, timeout=1) as r:
            return json.loads(r.read()).get("app") == "davinci-director"
    except OSError:
        return False


def _gui_python():
    """pythonw.exe next to python.exe: child processes without a console window."""
    exe = sys.executable
    if os.name == "nt" and os.path.basename(exe).lower() == "python.exe":
        w = os.path.join(os.path.dirname(exe), "pythonw.exe")
        return w if os.path.isfile(w) else exe
    return exe


def open_window(url):
    """The native WebView2 window, in its own process; never a second one."""
    global _window_proc
    if _window_proc is not None and _window_proc.poll() is None:
        return
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _window_proc = subprocess.Popen([_gui_python(), "-m", "davinci_director.window", url], cwd=pkg_parent,
                                    creationflags=NO_CONSOLE if os.name == "nt" else 0)


def _log_to_file():
    """pythonw has no console: output goes to data\\app.log (one rotated copy)."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    path = os.path.join(base_dir(), "app.log")
    if os.path.exists(path) and os.path.getsize(path) > 2_000_000:
        os.replace(path, path + ".1")
    f = open(path, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stdout or f
    sys.stderr = sys.stderr or f
    print("\n==== %s" % time.strftime("%Y-%m-%d %H:%M:%S"))


def sync_resolve_script():
    """Keep Workspace > Scripts > DaVinci Director in sync with this app version."""
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "resolve_scripts",
                       "DaVinci Director.lua")
    dst_dir = os.path.join(os.environ.get("APPDATA", ""), "Blackmagic Design", "DaVinci Resolve", "Support",
                           "Fusion", "Scripts", "Utility")
    dst = os.path.join(dst_dir, "DaVinci Director.lua")
    if sys.platform != "win32" or not os.path.isfile(src) or not os.path.isdir(os.path.dirname(dst_dir)):
        return  # no Resolve for this user
    with open(src, encoding="utf-8") as f:
        code = f.read()
    if os.path.isfile(dst):
        with open(dst, encoding="utf-8") as f:
            if f.read() == code:
                return
    try:
        os.makedirs(dst_dir, exist_ok=True)
        with open(dst, "w", encoding="utf-8", newline="\n") as f:
            f.write(code)
        print("Resolve script updated: %s" % dst)
    except OSError as e:
        print("Resolve script not updated: %s" % e)


def _parse_args(argv):
    ap = argparse.ArgumentParser(prog="davinci_director")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--bridge-dir", default=None)
    ap.add_argument("--no-script-sync", action="store_true", help="do not update the script in Resolve's Scripts folder")
    return ap.parse_args(argv)


def _wait_until_window_closed(app):
    """Closing the window ends the work: nothing keeps CPU, GPU or memory busy unseen.
    Returns True when the window was closed (restart as an idle watcher), False on quit."""
    seen, idle_since = False, time.time()
    while True:
        time.sleep(1)
        if app.subscribers:
            seen, idle_since = True, time.time()
        if app.quit_requested:
            return False
        if app.window_closed or (seen and not app.subscribers and time.time() - idle_since > CLOSED_GRACE):
            return True


def main(argv=None):
    from .storage import setup_env
    setup_env()
    _log_to_file()
    from . import components
    components.activate()
    args = _parse_args(argv)
    if not args.no_script_sync:
        sync_resolve_script()
    if running_on(args.port):
        print("DaVinci Director already running on port %d" % args.port)
        return 0

    link = FileBridgeLink(args.bridge_dir or default_bridge_dir()).start()
    engine = Engine(link)
    app = App(engine, Settings())
    srv = make_server(app, port=args.port)
    url = "http://127.0.0.1:%d/" % srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print("DaVinci Director %s — %s" % (__version__, url))
    link.on(lambda kind, data: kind == "link" and data.get("connected") and open_window(url))

    restart = False
    try:
        restart = _wait_until_window_closed(app)
    except KeyboardInterrupt:
        pass
    finally:
        app.stop_all_jobs()
        if _window_proc is not None and _window_proc.poll() is None:
            _window_proc.terminate()  # an open window would keep the program's files locked (an update fails)
        link.stop()
        srv.shutdown()
        srv.server_close()  # free the port for the restarted watcher
    if restart:
        _restart_watcher()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)  # a job thread may sit inside native code (torch): do not wait for it


def _restart_watcher():
    """Start a fresh copy of this process (same command line); it holds no models."""
    script = os.path.abspath(sys.argv[0])
    if os.path.basename(script) == "__main__.py":  # python -m davinci_director
        cmd, cwd = [_gui_python(), "-m", "davinci_director"] + sys.argv[1:], os.path.dirname(os.path.dirname(script))
    else:  # davinci_director_watcher.pyw
        cmd, cwd = [_gui_python(), script] + sys.argv[1:], os.path.dirname(script)
    try:
        subprocess.Popen(cmd, cwd=cwd, creationflags=NO_CONSOLE | DETACHED if os.name == "nt" else 0)
        print("Window closed: all work stopped, background watcher restarted.")
    except OSError as e:
        print("Background watcher not restarted: %s" % e)


if __name__ == "__main__":
    sys.exit(main())
