"""DaVinci Director app.

    python -m davinci_director              # normal use (with the Resolve bridge script)
    python -m davinci_director --direct     # Resolve Studio: external scripting instead of the bridge
    python -m davinci_director --demo       # UI demo: mock timeline + synthetic transcript
    python -m davinci_director --mock FILE  # mock timeline over a real media file (real Whisper)
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

from . import __version__
from .engine import Engine, Transcript
from .link import FileBridgeLink, PollingDirectLink, connect_external, default_bridge_dir
from .server import App, make_server
from .storage import Settings, base_dir

DEFAULT_PORT = 8765


def _ping(port):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/ping" % port, timeout=1) as r:
            return json.loads(r.read()).get("app") == "davinci-director"
    except Exception:
        return False


def _ping_mode(port):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/ping" % port, timeout=1) as r:
            return json.loads(r.read())
    except Exception:
        return {}


def _post_quit(port):
    try:
        req = urllib.request.Request("http://127.0.0.1:%d/api/quit" % port, data=b"{}",
                                     headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=2).read()
    except Exception:
        pass


_window_proc = None


def _native_window_available():
    try:
        import importlib.util
        return importlib.util.find_spec("webview") is not None and importlib.util.find_spec("clr_loader") is not None
    except Exception:
        return False


def open_window(url, use_webview=True):
    """Open the panel.  Preferred: a native WebView2 window in its own process
    (no browser extensions, sidebars or toolbars).  Fallback: Edge/Chrome in app
    mode with a private profile and extensions disabled.  Never blocks."""
    if use_webview and _native_window_available():
        exe = sys.executable
        if os.name == "nt" and os.path.basename(exe).lower() == "python.exe":
            w = os.path.join(os.path.dirname(exe), "pythonw.exe")
            exe = w if os.path.isfile(w) else exe
        global _window_proc
        if _window_proc is not None and _window_proc.poll() is None:
            return False  # already open (it may still be loading)
        try:
            _window_proc = subprocess.Popen([exe, "-m", "davinci_director.window", url],
                                            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                            creationflags=0x08000000 if os.name == "nt" else 0)
            return False
        except OSError:
            pass
    candidates = [
        os.path.join(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"), r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(os.environ.get("PROGRAMFILES", r"C:\Program Files"), r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(os.environ.get("PROGRAMFILES", r"C:\Program Files"), r"Google\Chrome\Application\chrome.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Google\Chrome\Application\chrome.exe"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ] + [shutil.which(n) for n in ("msedge", "chrome", "google-chrome", "chromium")]
    profile = os.path.join(base_dir(), "window-profile")
    flags = ["--app=" + url, "--window-size=1500,920", "--user-data-dir=" + profile, "--no-first-run",
             "--no-default-browser-check", "--disable-extensions", "--disable-component-extensions-with-background-pages",
             "--disable-sync", "--disable-background-networking", "--disable-default-apps",
             "--disable-features=Translate,msEdgeSidebarV2,msHubApps,msEdgeShoppingUI,msShowCopilot,"
             "msImplicitSignin,msEdgeRewards,EdgeCollections,msUndersideButton,msWebAssist"]
    for exe in candidates:
        if exe and os.path.isfile(exe):
            try:
                subprocess.Popen([exe] + flags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return False
            except OSError:
                pass
    webbrowser.open(url)
    return False


def demo_link(media_path=None, minutes=12):
    """Mock Resolve with a stream recording on the timeline."""
    from .mock_resolve import MockResolve
    r = MockResolve(fps=25.0)
    path = media_path or os.path.join(base_dir(), "demo", "Стрім — демо.mp4")
    frames = int(minutes * 60 * 25)
    if media_path and os.path.exists(media_path):
        try:
            from .media import probe
            info = probe(media_path)
            frames = int(info["duration"] * ((info.get("video") or {}).get("fps") or 25))
        except Exception:
            pass
    mpi = r.add_media(path, fps=25.0, frames=frames)
    r.timeline_with(mpi, name="Стрім 14.11", ranges=[(0, frames)])
    return r, path, frames / 25.0


DEMO_LINES = [
    (0, "Всім привіт, з вами знову стрім, сьогодні проходимо фінальну главу."),
    (0, "Нагадую, що минулого разу ми застрягли на мосту біля фортеці."),
    (1, "Так, і ти тоді тричі впав з того мосту, я все записав."),
    (0, "Ну дякую, дуже підтримуєш. Давай цього разу без падінь."),
    (1, "Окей, бачу ворогів зліва, їх троє, обережно."),
    (0, "Беру лука, прикрий мене, я йду по даху."),
    (1, "Стоп, стоп, там пастка, бачиш плиту на підлозі?"),
    (0, "Ох, ледь не наступив. Добре, обходимо по стіні."),
    (0, "Чат пише, що за дверима бос. Подивимось."),
    (1, "Якщо це бос, то в нас лише два зілля лікування."),
    (0, "Нормально, головне не панікувати і стежити за атакою з неба."),
    (1, "Він злітає! Зараз буде вогонь, ховайся за колоною!"),
    (0, "Є! Я влучив у крило, він падає, бий зараз!"),
    (1, "Ще трохи, у нього половина здоров'я."),
    (0, "Ні-ні-ні, я без зілля, відходжу назад."),
    (1, "Тримай, кидаю тобі останнє, повертайся."),
    (0, "Дякую, друже. Фінальний удар — і все!"),
    (1, "Ми це зробили! Перемога з першої спроби!"),
    (0, "Чат, ви бачили? Це було неймовірно. Дякую всім, хто був з нами."),
    (1, "Наступного разу — секретна локація, не пропустіть."),
]


def demo_transcript(path, duration):
    words, t = [], 2.0
    rep = 0
    while t < duration - 10:
        for sp, line in DEMO_LINES:
            for w in line.split():
                d = 0.18 + 0.045 * len(w)
                words.append([round(t, 3), round(t + d, 3), w, sp, 1.0])
                t += d + 0.08
            t += 0.6 if rep % 3 else 4.5  # pauses = gameplay without speech
            rep += 1
            if t >= duration - 10:
                break
    return Transcript(path, words, [{"id": "S1", "name": "Олекса"}, {"id": "S2", "name": "Марко"}], lang="uk",
                      covered=[[0, duration]], meta={"model": "demo", "fps": 25})


def _log_to_file():
    """pythonw (Start-menu shortcut) has no console: send output to a log file."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    path = os.path.join(base_dir(), "app.log")
    try:
        if os.path.exists(path) and os.path.getsize(path) > 2_000_000:
            os.replace(path, path + ".1")
        f = open(path, "a", encoding="utf-8", buffering=1)
    except OSError:
        f = open(os.devnull, "w")
    sys.stdout = sys.stdout or f
    sys.stderr = sys.stderr or f
    print("\n==== %s" % time.strftime("%Y-%m-%d %H:%M:%S"))


def sync_resolve_script():
    """Keep Workspace > Scripts > DaVinci Director in sync with this app version."""
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "resolve_scripts", "DaVinci Director.lua")
    if not os.path.isfile(src) or sys.platform != "win32":
        return
    dst_dir = os.path.join(os.environ.get("APPDATA", ""), "Blackmagic Design", "DaVinci Resolve", "Support",
                           "Fusion", "Scripts", "Utility")
    dst = os.path.join(dst_dir, "DaVinci Director.lua")
    try:
        with open(src, encoding="utf-8") as f:
            code = f.read()
        if os.path.isfile(dst):
            with open(dst, encoding="utf-8") as f:
                if f.read() == code:
                    return
        elif not os.path.isdir(os.path.dirname(dst_dir)):
            return  # Resolve is not installed for this user
        os.makedirs(dst_dir, exist_ok=True)
        with open(dst, "w", encoding="utf-8", newline="\n") as f:
            f.write(code)
        print("Resolve script updated: %s" % dst)
    except OSError as e:
        print("Resolve script not updated: %s" % e)


def main(argv=None):
    from .storage import setup_env
    setup_env()  # everything (caches of libraries too) goes into the program's folder; moves old data once
    _log_to_file()
    from . import components
    components.activate()
    ap = argparse.ArgumentParser(prog="davinci_director")
    ap.add_argument("--direct", action="store_true", help="Resolve Studio external scripting instead of the bridge")
    ap.add_argument("--demo", action="store_true", help="mock timeline with a synthetic transcript")
    ap.add_argument("--mock", metavar="FILE", help="mock timeline over a real media file")
    ap.add_argument("--bridge-dir", default=None)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-window", action="store_true")
    ap.add_argument("--browser", action="store_true", help="open in an Edge/Chrome app window instead of the native one")
    ap.add_argument("--keep-alive", action="store_true", help="do not exit when the window is closed")
    ap.add_argument("--background", action="store_true",
                    help="start hidden (Windows startup); open the window when the Resolve script connects")
    ap.add_argument("--no-script-sync", action="store_true", help="do not update the script in Resolve's Scripts folder")
    args = ap.parse_args(argv)
    if not (args.no_script_sync or args.demo or args.mock):
        sync_resolve_script()

    if not (args.demo or args.mock) and _ping(args.port):
        if args.background and not _ping_mode(args.port).get("background"):
            # a foreground copy is running (started by hand): ask it to finish, take over
            _post_quit(args.port)
            for _ in range(50):
                if not _ping(args.port):
                    break
                time.sleep(0.2)
        if _ping(args.port):
            if not args.no_window and not args.background:
                open_window("http://127.0.0.1:%d/" % args.port, not args.browser)
            print("DaVinci Director already running on port %d" % args.port)
            return 0

    settings = Settings()
    engine_project = None
    if args.demo or args.mock:
        r, path, duration = demo_link(args.mock)
        link = PollingDirectLink(r).start()
        engine_project = r.project.name
    elif args.direct:
        res = connect_external()
        if res is None:
            print("External scripting unavailable — falling back to the bridge script.")
            link = FileBridgeLink(args.bridge_dir or default_bridge_dir()).start()
        else:
            link = PollingDirectLink(res).start()
    else:
        link = FileBridgeLink(args.bridge_dir or default_bridge_dir()).start()

    engine = Engine(link)
    if engine_project:
        engine.load_project(engine_project)
        if args.demo:
            engine.set_transcript(demo_transcript(path, duration))
    elif link.info.get("project"):
        engine.load_project(link.info["project"])
    app = App(engine, settings)
    app.background = args.background
    srv = make_server(app, port=args.port)
    port = srv.server_address[1]
    url = "http://127.0.0.1:%d/" % port
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print("DaVinci Director %s — %s" % (__version__, url))

    if args.background:
        # Resolve's script sandbox cannot start programs, so the app waits hidden and
        # opens its window as soon as the script in Resolve connects (or reconnects).
        def on_link(kind, data):
            if kind == "link" and data.get("connected") and not app.subscribers:
                open_window(url, True)
        link.on(on_link)
    blocking = False if (args.no_window or args.background) else open_window(url, not args.browser)
    restart = False
    try:
        if blocking:
            return 0
        # Closing the window closes the program: every job stops.  In the background mode (started with
        # Windows so that Resolve's menu can open the window) the process then restarts as an idle watcher,
        # so nothing it loaded (Whisper, pyannote, CUDA) keeps CPU, GPU or memory busy.
        seen, idle_since = False, time.time()
        while True:
            time.sleep(1)
            if app.subscribers:
                seen, idle_since = True, time.time()
            if getattr(app, "quit_requested", False):
                break
            closed = getattr(app, "window_closed", False) or (seen and not app.subscribers
                                                               and time.time() - idle_since > 20)
            if closed and not args.no_window and not args.keep_alive:
                restart = args.background
                break
    except KeyboardInterrupt:
        pass
    finally:
        app.stop_all_jobs()
        try:
            link.stop()
        except Exception:
            pass
        srv.shutdown()
        srv.server_close()  # free the port for the restarted watcher
    if restart:
        _restart_watcher()
    # a job thread may sit inside native code (torch, CTranslate2): leave without waiting for it
    for f in (sys.stdout, sys.stderr):
        try:
            f.flush()
        except Exception:
            pass
    os._exit(0)


def _restart_watcher():
    """Start a fresh copy of this background process (same command line), then exit."""
    exe = sys.executable
    if os.name == "nt" and os.path.basename(exe).lower() == "python.exe":
        w = os.path.join(os.path.dirname(exe), "pythonw.exe")
        exe = w if os.path.isfile(w) else exe
    script = os.path.abspath(sys.argv[0])
    if os.path.basename(script) == "__main__.py":  # started as `python -m davinci_director`
        cmd, cwd = [exe, "-m", "davinci_director"] + sys.argv[1:], os.path.dirname(os.path.dirname(script))
    else:  # davinci_director_watcher.pyw (Windows startup)
        cmd, cwd = [exe, script] + sys.argv[1:], os.path.dirname(script)
    try:
        subprocess.Popen(cmd, cwd=cwd, creationflags=0x08000000 | 0x00000008 if os.name == "nt" else 0)
        print("Window closed: all work stopped, background watcher restarted.")
    except OSError as e:
        print("Background watcher not restarted: %s" % e)


if __name__ == "__main__":
    sys.exit(main())
