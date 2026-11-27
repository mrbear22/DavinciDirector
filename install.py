#!/usr/bin/env python3
"""DaVinci Director from the source code (for development; friends get the installer from build.py).

    python install.py              # .venv + hidden start with Windows
    python install.py --uninstall  # stop it and remove the autostart entry

The app copies its script into Resolve's Scripts folder itself and downloads its components
(audio, neural networks) and the recognition model when they are first needed.
"""

import argparse
import os
import subprocess
import sys
import urllib.request
import venv
import winreg

HERE = os.path.dirname(os.path.abspath(__file__))
VENV = os.path.join(HERE, ".venv")
PYW = os.path.join(VENV, "Scripts", "pythonw.exe")
WATCHER = os.path.join(HERE, "davinci_director_watcher.pyw")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "DaVinciDirector"
PORT = 8765
DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


def install():
    if not os.path.exists(PYW):
        print("Створюю .venv")
        venv.EnvBuilder(with_pip=True).create(VENV)
    py = os.path.join(VENV, "Scripts", "python.exe")
    subprocess.check_call([py, "-m", "pip", "install", "-q", "--disable-pip-version-check", "-r",
                           os.path.join(HERE, "requirements.txt")])
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, '"%s" "%s"' % (PYW, WATCHER))
    subprocess.Popen([PYW, WATCHER], cwd=HERE, creationflags=DETACHED)
    print("Готово. У DaVinci Resolve: Workspace > Scripts > DaVinci Director "
          "(якщо пункту немає — перезапустіть Resolve).")


def uninstall():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, RUN_VALUE)
    except FileNotFoundError:
        pass  # was not registered
    req = urllib.request.Request("http://127.0.0.1:%d/api/quit" % PORT, data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=2).read()
    except OSError:
        pass  # not running
    print("Автозапуск вимкнено, програму зупинено.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uninstall", action="store_true")
    sys.exit(uninstall() if ap.parse_args().uninstall else install())
