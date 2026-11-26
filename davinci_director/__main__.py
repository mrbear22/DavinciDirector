"""Entry point: python -m davinci_director [--bridge URL --token T] [--mock FILE]"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.request
import webbrowser

from . import __version__
from .bridge import BridgeClient, DirectClient, MockResolveApp
from .server import App, make_server


def _ping(port):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/ping" % port, timeout=1) as r:
            return json.loads(r.read()).get("app") == "davinci-director"
    except Exception:
        return False


def _post(port, path, body):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def open_window(url, title="DaVinci Director", use_webview=True):
    """Native window via pywebview if installed, else a Chromium app window,
    else the default browser.  Returns True if the call blocks (webview)."""
    if use_webview:
        try:
            import webview  # pywebview
            webview.create_window(title, url, width=1600, height=950, min_size=(1100, 650),
                                  background_color="#1c1c1e")
            webview.start()
            return True
        except Exception:
            pass
    for name in ("google-chrome", "chromium", "chromium-browser", "msedge", "chrome",
                 r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                 r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                 "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                 "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"):
        exe = shutil.which(name) or (name if os.path.isfile(name) else None)
        if exe:
            try:
                subprocess.Popen([exe, "--app=" + url, "--window-size=1600,950"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return False
            except OSError:
                pass
    webbrowser.open(url)
    return False


def main(argv=None):
    ap = argparse.ArgumentParser(prog="davinci_director", description="DaVinci Director %s" % __version__)
    ap.add_argument("--bridge", help="URL of the in-Resolve bridge (set by the launcher)")
    ap.add_argument("--token", help="bridge token")
    ap.add_argument("--port", type=int, default=int(os.environ.get("DD_PORT", 8765)))
    ap.add_argument("--mock", nargs="*", metavar="VIDEO", help="demo mode without Resolve: video files for a mock timeline")
    ap.add_argument("--no-window", action="store_true", help="do not open a window (server only)")
    ap.add_argument("--no-webview", action="store_true", help="use the browser instead of pywebview")
    args = ap.parse_args(argv)

    url = "http://127.0.0.1:%d/" % args.port
    if _ping(args.port):
        # already running: hand over the fresh bridge and just show the window
        if args.bridge:
            _post(args.port, "/api/bridge", {"url": args.bridge, "token": args.token})
        if not args.no_window:
            open_window(url, use_webview=False)
        return 0

    if args.mock is not None:
        from .media import probe
        files = [(os.path.abspath(f), probe(f)["duration"]) for f in args.mock]
        client = DirectClient(MockResolveApp(files))
    elif args.bridge:
        client = BridgeClient(args.bridge, args.token)
    else:
        client = DirectClient()

    app = App(client)
    server = make_server(app, port=args.port)
    print("DaVinci Director %s — %s" % (__version__, url), flush=True)
    try:
        app.connect()
    except Exception as e:
        print("Resolve: %s" % e, flush=True)

    if args.no_window:
        server.serve_forever()
        return 0
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    blocking = open_window(url, use_webview=not args.no_webview)
    if not blocking:
        try:
            t.join()
        except KeyboardInterrupt:
            pass
    app.saver.flush()
    if isinstance(app.resolve, BridgeClient):
        try:
            req = urllib.request.Request(app.resolve.url + "/shutdown", data=b"{}",
                                         headers={"X-DD-Token": app.resolve.token}, method="POST")
            urllib.request.urlopen(req, timeout=2)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
