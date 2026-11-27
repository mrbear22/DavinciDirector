#!/usr/bin/env python3
"""DaVinci Director installer.

    python install.py                 # install / update
    python install.py --diarization   # also install pyannote.audio (speaker detection, large)
    python install.py --check         # only diagnose, change nothing
    python install.py --uninstall

What it does
  1. finds DaVinci Resolve, its version and edition (Free / Studio);
  2. installs the bridge script  "DaVinci Director.lua"  into Resolve's user
     Scripts\\Utility folder — it appears in  Workspace > Scripts  on every page.
     (Resolve 21 free lists only Lua scripts there; Python menu scripts are not
     shown, which is why the old .py launcher never appeared);
  3. creates .venv and installs the Python dependencies of the app;
  4. registers a hidden watcher (HKCU Run, no shortcuts) that opens the panel when
     Workspace > Scripts > DaVinci Director runs in Resolve;
  5. prints a report with every check and what to do next.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import venv

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE_SRC = os.path.join(HERE, "resolve_scripts", "DaVinci Director.lua")
SCRIPT_NAME = "DaVinci Director.lua"
OLD_NAMES = ("DaVinci Director.py",)
MIN_EDIT_VERSION = (21, 1, 2)  # TimelineItem Set*Offset / Timeline.MoveItems
IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

REPORT = []


def report(status, what, detail=""):
    REPORT.append((status, what, detail))
    mark = {"ok": "[ OK ]", "warn": "[WARN]", "err": "[FAIL]", "info": "[ .. ]"}[status]
    print("%s %s%s" % (mark, what, (" — " + detail) if detail else ""))


# ------------------------------------------------------------------ Resolve discovery


def user_scripts_root():
    if IS_WIN:
        return os.path.join(os.environ.get("APPDATA", ""), "Blackmagic Design", "DaVinci Resolve", "Support", "Fusion", "Scripts")
    if IS_MAC:
        return os.path.expanduser("~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts")
    return os.path.expanduser("~/.local/share/DaVinciResolve/Fusion/Scripts")


def bridge_dir():
    """Must match davinci_director.link.default_bridge_dir() and the Lua default:
    Resolve's script sandbox refuses to write into %APPDATA%, %TEMP% works."""
    import tempfile
    base = os.environ.get("TEMP") or os.environ.get("TMP") or tempfile.gettempdir()
    return os.path.join(base, "DaVinciDirector", "bridge")


def _ps(cmd):
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                             capture_output=True, text=True, timeout=30)
        return out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def find_resolve():
    """Return {exe, version (tuple), version_str, studio (bool|None)} or None."""
    info = {"exe": None, "version": None, "version_str": "", "studio": None, "name": ""}
    if IS_WIN:
        try:
            import winreg
            for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                for base in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                             r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"):
                    try:
                        key = winreg.OpenKey(root, base)
                    except OSError:
                        continue
                    for i in range(winreg.QueryInfoKey(key)[0]):
                        try:
                            sub = winreg.OpenKey(key, winreg.EnumKey(key, i))
                            name = winreg.QueryValueEx(sub, "DisplayName")[0]
                        except OSError:
                            continue
                        if re.match(r"^DaVinci Resolve( Studio)?$", name or ""):
                            info["name"] = name
                            info["studio"] = "Studio" in name
                            try:
                                info["version_str"] = winreg.QueryValueEx(sub, "DisplayVersion")[0]
                            except OSError:
                                pass
        except ImportError:
            pass
        for exe in (os.path.join(os.environ.get("PROGRAMFILES", r"C:\Program Files"), "Blackmagic Design", "DaVinci Resolve", "Resolve.exe"),):
            if os.path.isfile(exe):
                info["exe"] = exe
                v = _ps("(Get-Item '%s').VersionInfo.ProductVersion" % exe)
                if v:
                    info["version_str"] = v
                pname = _ps("(Get-Item '%s').VersionInfo.ProductName" % exe)
                if pname:
                    info["studio"] = "Studio" in pname
                    info["name"] = pname
    elif IS_MAC:
        for app in ("/Applications/DaVinci Resolve/DaVinci Resolve.app", "/Applications/DaVinci Resolve Studio/DaVinci Resolve.app"):
            plist = os.path.join(app, "Contents", "Info.plist")
            if os.path.isfile(plist):
                info["exe"] = app
                info["studio"] = "Studio" in app
                with open(plist, "rb") as f:
                    import plistlib
                    d = plistlib.load(f)
                info["version_str"] = d.get("CFBundleShortVersionString", "")
    else:
        if os.path.exists("/opt/resolve/bin/resolve"):
            info["exe"] = "/opt/resolve/bin/resolve"
    if not info["exe"] and not info["name"]:
        return None
    m = re.match(r"(\d+)\.(\d+)(?:\.(\d+))?", info["version_str"] or "")
    if m:
        info["version"] = tuple(int(x or 0) for x in m.groups())
    return info


def resolve_running():
    if IS_WIN:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Resolve.exe"], capture_output=True, text=True)
        return "Resolve.exe" in out.stdout
    out = subprocess.run(["pgrep", "-f", "Resolve"], capture_output=True, text=True)
    return bool(out.stdout.strip())


# ------------------------------------------------------------------ steps


def install_bridge(dry=False):
    root = user_scripts_root()
    target_dir = os.path.join(root, "Utility")
    support = os.path.dirname(root)
    if not os.path.isdir(support):
        report("warn", "Тека Fusion користувача не знайдена", "%s — Resolve ще жодного разу не запускався? Тека буде створена." % support)
    with open(BRIDGE_SRC, encoding="utf-8") as f:
        code = f.read()
    bdir = bridge_dir()  # the script computes the same folder from %TEMP% itself
    dest = os.path.join(target_dir, SCRIPT_NAME)
    if dry:
        exists = os.path.isfile(dest)
        report("ok" if exists else "warn", "Скрипт мосту у Resolve", dest if exists else "не встановлено: %s" % dest)
        return dest
    os.makedirs(target_dir, exist_ok=True)
    with open(dest, "w", encoding="utf-8", newline="\n") as f:
        f.write(code)
    with open(dest, encoding="utf-8") as f:
        ok = f.read() == code
    report("ok" if ok else "err", "Скрипт мосту встановлено", dest)
    for old in OLD_NAMES:
        p = os.path.join(target_dir, old)
        if os.path.isfile(p):
            os.remove(p)
            report("ok", "Видалено старий лаунчер, який Resolve не показував", p)
    for sub in ("in", "out", "state"):
        os.makedirs(os.path.join(bdir, sub), exist_ok=True)
    report("ok", "Тека обміну з Resolve", bdir)
    old_dir = os.path.join(os.environ.get("APPDATA", ""), "DaVinciDirector")  # v2.0 location, not writable for Resolve
    if IS_WIN and os.path.isdir(old_dir):
        shutil.rmtree(old_dir, ignore_errors=True)
    return dest


def setup_venv(args):
    if args.no_venv:
        return sys.executable
    vdir = os.path.join(HERE, ".venv")
    py = os.path.join(vdir, "Scripts", "python.exe") if IS_WIN else os.path.join(vdir, "bin", "python")
    if not os.path.exists(py):
        report("info", "Створення віртуального оточення", vdir)
        venv.EnvBuilder(with_pip=True).create(vdir)
    cmd = [py, "-m", "pip", "install", "--disable-pip-version-check", "-q", "--upgrade", "-r", os.path.join(HERE, "requirements.txt")]
    if args.diarization:
        cmd.append("pyannote.audio")
    if args.webview:
        cmd.append("pywebview")
    if IS_WIN and shutil.which("nvidia-smi") and not args.cpu:
        # CUDA runtime for faster-whisper on NVIDIA GPUs (falls back to CPU if unusable)
        cmd += ["nvidia-cublas-cu12", "nvidia-cudnn-cu12==9.*"]
        report("info", "Знайдено NVIDIA GPU", "буде встановлено cuBLAS/cuDNN для Whisper на GPU")
    report("info", "Встановлення залежностей (може тривати кілька хвилин)", " ".join(cmd[3:]))
    res = subprocess.run(cmd)
    if res.returncode != 0:
        report("err", "pip install завершився з помилкою", "код %d" % res.returncode)
        return None
    chk = subprocess.run([py, "-c", "import faster_whisper, numpy; print('ok')"], capture_output=True, text=True)
    report("ok" if "ok" in chk.stdout else "err", "Python-залежності", chk.stderr.strip().splitlines()[-1] if chk.returncode else "faster-whisper, numpy")
    if args.diarization:
        chk = subprocess.run([py, "-c", "import pyannote.audio; print('ok')"], capture_output=True, text=True)
        report("ok" if "ok" in chk.stdout else "warn", "pyannote.audio (визначення мовців)", "" if "ok" in chk.stdout else "не імпортується")
    return py


def check_python_env(py):
    if not py or not os.path.exists(py):
        report("warn", "Віртуальне оточення не створено", "запустіть python install.py")
        return
    chk = subprocess.run([py, "-c", "import faster_whisper; print('ok')"], capture_output=True, text=True)
    report("ok" if "ok" in chk.stdout else "warn", "faster-whisper у .venv", "" if "ok" in chk.stdout else "не встановлено")
    chk = subprocess.run([py, "-c", "import pyannote.audio; print('ok')"], capture_output=True, text=True)
    report("ok" if "ok" in chk.stdout else "info", "pyannote.audio (мовці)", "" if "ok" in chk.stdout else "не встановлено — python install.py --diarization")


def find_ffmpeg():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    if IS_WIN:  # winget installs into a per-user package folder that may not be on PATH for every process
        base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Packages")
        if os.path.isdir(base):
            for root, dirs, files in os.walk(base):
                if "ffmpeg.exe" in files:
                    return os.path.join(root, "ffmpeg.exe")
    return None


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "DaVinciDirector"
WATCHER = os.path.join(HERE, "davinci_director_watcher.pyw")
OLD_SHORTCUTS = ("DaVinci Director.lnk", os.path.join("Startup", "DaVinci Director (фон).lnk"))


def _remove_old_launchers():
    """Earlier versions created shortcuts and a .cmd — the panel now opens only from Resolve."""
    if not IS_WIN:
        return
    folders = [os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs"),
               _ps("[Environment]::GetFolderPath('Desktop')")]
    for folder in folders:
        for name in OLD_SHORTCUTS:
            path = os.path.join(folder or "", name)
            if folder and os.path.exists(path):
                os.remove(path)
                report("ok", "Видалено старий ярлик", path)
    cmd = os.path.join(HERE, "DaVinci Director.cmd")
    if os.path.exists(cmd):
        os.remove(cmd)


def register_watcher(py):
    """Like AutoSubs' "Open from Resolve": a hidden process started at login (HKCU Run)
    opens the panel when Workspace > Scripts > DaVinci Director runs in Resolve.
    Resolve's script sandbox cannot start programs itself."""
    _remove_old_launchers()
    if not IS_WIN:
        report("warn", "Автовідкриття з Resolve налаштовується лише у Windows",
               "запускайте: %s %s" % (py, WATCHER))
        return
    pyw = os.path.join(os.path.dirname(py), "pythonw.exe")
    target = pyw if os.path.exists(pyw) else py
    import winreg
    command = '"%s" "%s"' % (target, WATCHER)
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, command)
    report("ok", "Панель відкриватиметься з меню Resolve", "фоновий процес без вікна стартує разом з Windows (HKCU\\...\\Run)")
    subprocess.Popen([target, WATCHER], cwd=HERE, creationflags=0x00000008 | 0x00000200)  # DETACHED | NEW_GROUP
    report("ok", "Фоновий процес запущено", "можна відкривати панель: Workspace > Scripts > DaVinci Director")


def unregister_watcher():
    if not IS_WIN:
        return
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, RUN_VALUE)
            report("ok", "Автозапуск фонового процесу вимкнено")
    except OSError:
        pass
    try:
        import urllib.request
        req = urllib.request.Request("http://127.0.0.1:8765/api/quit", data=b"{}",
                                     headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=2).read()
    except Exception:
        pass


def uninstall():
    root = os.path.join(user_scripts_root(), "Utility")
    for name in (SCRIPT_NAME,) + OLD_NAMES:
        p = os.path.join(root, name)
        if os.path.exists(p):
            os.remove(p)
            report("ok", "Видалено", p)
    unregister_watcher()
    _remove_old_launchers()
    shutil.rmtree(os.path.dirname(bridge_dir()), ignore_errors=True)
    shutil.rmtree(os.path.join(os.environ.get("APPDATA", ""), "DaVinciDirector"), ignore_errors=True)
    report("info", "Транскрипції та сюжети залишено", os.path.join(os.path.expanduser("~"), ".davinci_director"))


# ------------------------------------------------------------------ main


def main():
    ap = argparse.ArgumentParser(description="DaVinci Director installer")
    ap.add_argument("--check", action="store_true", help="only diagnose")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--no-venv", action="store_true", help="use the current interpreter")
    ap.add_argument("--diarization", action="store_true", help="install pyannote.audio for speaker detection")
    ap.add_argument("--webview", action="store_true", help="install pywebview (native window)")
    ap.add_argument("--cpu", action="store_true", help="do not install CUDA libraries even if an NVIDIA GPU is present")
    args = ap.parse_args()
    if IS_WIN:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    print("DaVinci Director — встановлення\n")
    if args.uninstall:
        uninstall()
        return 0
    if sys.version_info < (3, 9):
        report("err", "Потрібен Python 3.9+", sys.version.split()[0])
        return 1
    report("ok", "Python", "%s (%s)" % (sys.version.split()[0], sys.executable))

    res = find_resolve()
    if not res:
        report("err", "DaVinci Resolve не знайдено", "встановіть Resolve 21.1.2+ і запустіть інсталятор ще раз")
    else:
        edition = "Studio" if res["studio"] else ("Free" if res["studio"] is False else "?")
        report("ok", "DaVinci Resolve", "%s %s (%s)" % (res["name"] or "DaVinci Resolve", res["version_str"] or "?", edition))
        if res["version"] and res["version"] < MIN_EDIT_VERSION:
            report("warn", "Версія Resolve старіша за 21.1.2",
                   "читання таймлайна працюватиме, але редагування кліпів з тексту потребує 21.1.2+ (API Set*Offset/MoveItems)")
    ff = find_ffmpeg()
    report("ok" if ff else "warn", "FFmpeg", ff or "не знайдено — встановіть: winget install Gyan.FFmpeg (потрібен для транскрибування)")

    if args.check:
        install_bridge(dry=True)
        py = os.path.join(HERE, ".venv", "Scripts" if IS_WIN else "bin", "python.exe" if IS_WIN else "python")
        check_python_env(py)
    else:
        if not install_bridge():
            return 1
        py = setup_venv(args)
        if not py:
            return 1
        register_watcher(py)

    running = resolve_running()
    errors = [r for r in REPORT if r[0] == "err"]
    print("\n" + "-" * 72)
    if errors:
        print("Встановлення НЕ завершено: %d помилк(и). Виправте пункти [FAIL] вище." % len(errors))
        return 1
    if args.check:
        warns = [r for r in REPORT if r[0] == "warn"]
        print("Діагностика завершена: %s." % ("попереджень немає" if not warns else "%d попередж." % len(warns)))
        if any(r[1].startswith("Скрипт мосту") for r in warns):
            print("Щоб встановити аддон, запустіть:  python install.py")
        return 0
    print("""Готово. Як запустити:

  1. У DaVinci Resolve відкрийте проєкт і таймлайн із записом.
  2. Меню Resolve:  Workspace > Scripts > DaVinci Director — відкриється панель DaVinci Director.
  3. Індикатор у панелі стане зеленим — текст і таймлайн синхронізовано.
""")
    if running:
        print("  Resolve зараз запущено: новий скрипт з'явиться в меню одразу (Resolve перечитує теку),\n"
              "  якщо ні — перезапустіть Resolve.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
