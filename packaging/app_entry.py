"""Entry point of the installed program (embedded Python, see build.py).

    pythonw dd_entry.py               open the panel (or bring the running app's window)
    pythonw dd_entry.py --background  hidden watcher (started at login via HKCU Run)
    pythonw dd_entry.py --quit        stop a running copy (installer / uninstaller)
    python  dd_entry.py --selftest F  check the installation, write a JSON report to F
"""

import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def quit_running(port=8765):
    import time
    import urllib.request
    try:
        req = urllib.request.Request("http://127.0.0.1:%d/api/quit" % port, data=b"{}",
                                     headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=2).read()
    except Exception:
        return
    for _ in range(40):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/api/ping" % port, timeout=0.5).read()
            time.sleep(0.25)
        except Exception:
            return


def selftest(path):
    report = {"ok": True, "checks": {}}

    def check(name, fn):
        try:
            report["checks"][name] = fn() or "ok"
        except Exception as e:  # report every failure
            report["ok"] = False
            report["checks"][name] = "FAIL: %r" % e

    here = os.path.dirname(os.path.abspath(__file__))
    check("python", lambda: sys.version.split()[0])
    check("stdlib", lambda: [__import__(m) for m in ("ssl", "http.server", "zipfile", "sqlite3", "ctypes", "winreg")] and "ok")
    check("pip", lambda: __import__("pip").__version__)
    check("app", lambda: __import__("davinci_director.server").__name__)
    check("web", lambda: len(os.listdir(os.path.join(here, "davinci_director", "web"))) >= 3 and "ok")
    check("lua", lambda: os.path.getsize(os.path.join(here, "resolve_scripts", "DaVinci Director.lua")) > 1000 and "ok")
    check("window", lambda: __import__("webview") and __import__("clr_loader") and "ok")
    from davinci_director import components
    check("components_dir", components.components_dir)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    return 0 if report["ok"] else 1


def run():
    args = sys.argv[1:]
    if args[:1] == ["--selftest"]:
        return selftest(args[1] if len(args) > 1 else "selftest.json")
    if args[:1] == ["--quit"]:
        quit_running()
        return 0
    from davinci_director.storage import setup_env
    setup_env()
    from davinci_director.__main__ import main
    return main(args)


if __name__ == "__main__":
    try:
        sys.exit(run())
    except SystemExit:
        raise
    except Exception:
        try:
            from davinci_director.storage import base_dir
            log = os.path.join(base_dir(), "crash.log")
        except Exception:
            log = os.path.join(os.environ.get("TEMP", "."), "DaVinciDirector-crash.log")
        os.makedirs(os.path.dirname(log), exist_ok=True)
        with open(log, "a", encoding="utf-8") as f:
            traceback.print_exc(file=f)
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, "DaVinci Director не запустився. Подробиці: %s" % log,
                                             "DaVinci Director", 0x10)
        except Exception:
            pass
        sys.exit(1)
