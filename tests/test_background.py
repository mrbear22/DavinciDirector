"""--background: the app waits hidden and opens its window when the Resolve script connects."""

import json
import os
import tempfile
import threading
import time

from davinci_director import __main__ as app_main


def test_window_opens_when_bridge_connects(tmp_path, monkeypatch):
    monkeypatch.setenv("DD_HOME", str(tmp_path / "home"))
    opened = []
    monkeypatch.setattr(app_main, "open_window", lambda url, webview=False: opened.append(url) or False)
    base = tempfile.mkdtemp(prefix="ddb")
    t = threading.Thread(target=app_main.main, args=(["--background", "--no-script-sync", "--bridge-dir", base, "--port", "8797"],), daemon=True)
    t.start()
    deadline = time.time() + 15
    while not os.path.isdir(os.path.join(base, "out")) and time.time() < deadline:
        time.sleep(0.05)
    time.sleep(0.5)
    assert opened == [], "no window before Resolve connects"
    # what DaVinci Director.lua writes on start: an info message (name carries hex JSON)
    payload = json.dumps({"v": "2.1", "project": "P", "tl": "T"}).encode().hex()
    with open(os.path.join(base, "out", "abc123-00000001-i-1of1-%s.csv" % payload), "w") as f:
        f.write("x")
    deadline = time.time() + 5
    while not opened and time.time() < deadline:
        time.sleep(0.05)
    assert opened and opened[0].startswith("http://127.0.0.1:")


def test_closing_the_window_stops_everything(tmp_path, monkeypatch):
    """Closing the window must not leave work running unseen: jobs are cancelled, installs killed, the port
    is released and the background process restarts as an idle watcher (then this one exits)."""
    import socket
    import urllib.request
    from davinci_director import components, server
    monkeypatch.setenv("DD_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(app_main, "open_window", lambda url, webview=False: False)
    events = []
    monkeypatch.setattr(app_main, "_restart_watcher", lambda: events.append("restart"))
    monkeypatch.setattr(components, "kill_children", lambda: events.append("kill"))

    def fake_exit(code):
        events.append("exit")  # the real one ends the process; here main() just returns

    monkeypatch.setattr(app_main.os, "_exit", fake_exit)
    apps = []
    real_app = server.App
    monkeypatch.setattr(app_main, "App", lambda *a, **k: apps.append(real_app(*a, **k)) or apps[-1])
    base = tempfile.mkdtemp(prefix="ddb")
    port = 8796
    t = threading.Thread(target=app_main.main, args=(["--background", "--no-script-sync", "--bridge-dir", base,
                                                      "--port", str(port)],), daemon=True)
    t.start()
    deadline = time.time() + 15
    while not app_main._ping(port) and time.time() < deadline:
        time.sleep(0.05)
    job = server.Job("transcribe", "Транскрибування")
    apps[0].jobs[job.id] = job
    req = urllib.request.Request("http://127.0.0.1:%d/api/window_closed" % port, data=b"{}",
                                 headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=5).read()
    t.join(10)
    assert not t.is_alive()
    assert job.cancelled
    assert events[-2:] == ["restart", "exit"] and "kill" in events
    s = socket.socket()
    s.bind(("127.0.0.1", port))  # the port is free for the restarted watcher
    s.close()
