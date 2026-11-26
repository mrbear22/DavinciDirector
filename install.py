#!/usr/bin/env python3
"""DaVinci Director installer.

    python install.py              # venv + dependencies + Resolve menu entry
    python install.py --no-venv    # use the current interpreter
    python install.py --diarization  # also install pyannote.audio (large)
    python install.py --uninstall

Creates ``.venv`` next to this file, installs requirements, and writes the
launcher to Resolve's user Scripts/Utility folder so the add-on appears in
Workspace > Scripts > DaVinci Director.
"""

import argparse
import os
import shutil
import subprocess
import sys
import venv

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.join(HERE, "resolve_scripts", "DaVinci Director.py")


def scripts_dirs():
    if sys.platform.startswith("win"):
        return [os.path.join(os.environ.get("APPDATA", ""), "Blackmagic Design", "DaVinci Resolve", "Support",
                             "Fusion", "Scripts", "Utility")]
    if sys.platform == "darwin":
        return [os.path.expanduser("~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility")]
    return [os.path.expanduser("~/.local/share/DaVinciResolve/Fusion/Scripts/Utility")]


def venv_python(path):
    return os.path.join(path, "Scripts", "python.exe") if sys.platform.startswith("win") else os.path.join(path, "bin", "python")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-venv", action="store_true")
    ap.add_argument("--diarization", action="store_true", help="install pyannote.audio for speaker detection")
    ap.add_argument("--webview", action="store_true", help="install pywebview for a native window")
    ap.add_argument("--scripts-dir", help="custom Resolve Scripts/Utility folder")
    ap.add_argument("--uninstall", action="store_true")
    args = ap.parse_args()

    targets = [args.scripts_dir] if args.scripts_dir else scripts_dirs()
    if args.uninstall:
        for d in targets:
            f = os.path.join(d, "DaVinci Director.py")
            if os.path.exists(f):
                os.remove(f)
                print("Removed", f)
        return

    if sys.version_info < (3, 9):
        sys.exit("Python 3.9+ is required")
    if not shutil.which("ffmpeg"):
        print("WARNING: ffmpeg not found in PATH — install FFmpeg before transcribing.")

    if args.no_venv:
        py = sys.executable
    else:
        vdir = os.path.join(HERE, ".venv")
        if not os.path.exists(venv_python(vdir)):
            print("Creating virtual environment", vdir)
            venv.EnvBuilder(with_pip=True).create(vdir)
        py = venv_python(vdir)
    reqs = [os.path.join(HERE, "requirements.txt")]
    cmd = [py, "-m", "pip", "install", "--upgrade", "-r", reqs[0]]
    if args.diarization:
        cmd.append("pyannote.audio")
    if args.webview:
        cmd.append("pywebview")
    print(" ".join(cmd))
    subprocess.check_call(cmd)

    with open(LAUNCHER, encoding="utf-8") as f:
        code = f.read().replace("__ADDON_DIR__", HERE).replace("__APP_PYTHON__", py)
    for d in targets:
        os.makedirs(d, exist_ok=True)
        dest = os.path.join(d, "DaVinci Director.py")
        with open(dest, "w", encoding="utf-8") as f:
            f.write(code)
        print("Launcher installed:", dest)
    print("\nDone. Restart DaVinci Resolve and open Workspace > Scripts > DaVinci Director.")


if __name__ == "__main__":
    main()
