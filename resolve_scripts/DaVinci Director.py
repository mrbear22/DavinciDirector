#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""DaVinci Director launcher — Workspace > Scripts > DaVinci Director.

Runs inside DaVinci Resolve's script host (free & Studio).  It:
  1. starts a local token-protected bridge to the Resolve scripting API;
  2. launches the DaVinci Director app in its own Python environment;
  3. serves Resolve API calls for the app until the app exits.

install.py writes the two paths below.  Only the standard library is used.
"""

import os
import secrets
import socket
import subprocess
import sys
import time

ADDON_DIR = r"__ADDON_DIR__"
APP_PYTHON = r"__APP_PYTHON__"

sys.path.insert(0, os.path.join(ADDON_DIR, "davinci_director"))
import bridge as dd_bridge  # noqa: E402  (stdlib-only module of the add-on)


def _resolve_object():
    try:
        return resolve  # noqa: F821  injected by Resolve's script host
    except NameError:
        pass
    try:
        return bmd.scriptapp("Resolve")  # noqa: F821
    except NameError:
        pass
    import DaVinciResolveScript as dvr  # noqa: N813
    return dvr.scriptapp("Resolve")


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main():
    res = _resolve_object()
    if res is None:
        print("[DaVinci Director] Resolve scripting API is not available.")
        return
    token = secrets.token_hex(16)
    port = _free_port()
    url = "http://127.0.0.1:%d" % port
    log_path = os.path.join(os.path.expanduser("~"), ".davinci_director", "app.log")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    log = open(log_path, "a")
    env = dict(os.environ)
    env["PYTHONPATH"] = ADDON_DIR + os.pathsep + env.get("PYTHONPATH", "")
    for var in ("PYTHONHOME",):  # do not leak Resolve's interpreter settings
        env.pop(var, None)
    flags = 0x08000000 if sys.platform.startswith("win") else 0  # CREATE_NO_WINDOW
    proc = {"p": None}

    def started(_port):
        proc["p"] = subprocess.Popen([APP_PYTHON, "-m", "davinci_director", "--bridge", url, "--token", token],
                                     cwd=ADDON_DIR, env=env, stdout=log, stderr=log, creationflags=flags)
        print("[DaVinci Director] bridge %s, app pid %s" % (url, proc["p"].pid))

    try:
        dd_bridge.serve_bridge(res, port, token, should_stop=lambda: _app_gone(proc, url), on_ready=started)
    finally:
        log.close()
    print("[DaVinci Director] closed")


def _app_gone(proc, url):
    """Stop serving when no app instance uses this bridge any more."""
    p = proc["p"]
    if p is None or p.poll() is None:
        return False
    # The child may have exited right away after handing the bridge to an
    # already running instance; that instance shuts us down via /shutdown.
    return p.returncode not in (0,)


main()
