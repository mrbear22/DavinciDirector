#!/usr/bin/env python3
"""Build the DaVinci Director installer for Windows.

    python build.py                # -> dist\\DaVinciDirector-Setup-<version>.exe
    python build.py --skip-tests

The installer is small (~15 MB): the official embeddable Python from python.org,
pip, pywebview (the program's native WebView2 window) and the app code.  No libraries are bundled — the program downloads what it
needs while it runs, into %LOCALAPPDATA%\\DaVinciDirector\\components:
  * Whisper engine (faster-whisper, CTranslate2) with PyAV/FFmpeg — first transcription;
  * NVIDIA CUDA libraries — only with an NVIDIA GPU and "прискорення" on;
  * pyannote + torch — only when speaker detection is used.
Whisper/pyannote models are downloaded by those libraries on first use.

The build downloads (once, cached in build\\cache):
  * python-<ver>-embed-amd64.zip  — python.org, ~11 MB;
  * Inno Setup 6 (is.exe)         — jrsoftware.org, ~5 MB, installed per user into build\\tools
    if it is not installed yet.
"""

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(HERE, "build")
CACHE = os.path.join(BUILD, "cache")
STAGE = os.path.join(BUILD, "stage")
TOOLS = os.path.join(BUILD, "tools")
DIST = os.path.join(HERE, "dist")
PY_VER = "%d.%d.%d" % sys.version_info[:3]
INNO_URL = "https://jrsoftware.org/download.php/is.exe"


def step(msg):
    print("\n== %s" % msg, flush=True)


def fail(msg):
    print("\nЗБІРКА НЕ ВДАЛАСЯ: %s" % msg)
    sys.exit(1)


def version():
    ns = {}
    with open(os.path.join(HERE, "davinci_director", "__init__.py"), encoding="utf-8") as f:
        exec(f.read(), ns)
    return ns["__version__"]


def download(url, dest):
    if os.path.isfile(dest) and os.path.getsize(dest) > 0:
        return dest
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    print("завантаження %s" % url, flush=True)
    req = urllib.request.Request(url, headers={"User-Agent": "davinci-director-build"})
    with urllib.request.urlopen(req, timeout=120) as r, open(dest + ".part", "wb") as f:
        shutil.copyfileobj(r, f)
    os.replace(dest + ".part", dest)
    return dest


def find_iscc():
    for p in (os.path.join(TOOLS, "InnoSetup6", "ISCC.exe"),
              r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe", r"C:\Program Files\Inno Setup 6\ISCC.exe",
              os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Programs\Inno Setup 6\ISCC.exe")):
        if os.path.isfile(p):
            return p
    return shutil.which("iscc")


def ensure_inno():
    iscc = find_iscc()
    if iscc:
        return iscc
    step("Inno Setup 6 (завантаження в build\\tools)")
    setup = download(INNO_URL, os.path.join(CACHE, "innosetup.exe"))
    target = os.path.join(TOOLS, "InnoSetup6")
    code = subprocess.call([setup, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER", "/NOICONS",
                            "/DIR=%s" % target, "/LOG=%s" % os.path.join(BUILD, "innosetup-install.log")])
    iscc = find_iscc()
    if code != 0 or not iscc:
        fail("не вдалося встановити Inno Setup (код %s). Встановіть вручну: https://jrsoftware.org/isdl.php" % code)
    return iscc


def stage_python():
    step("Вбудований Python %s" % PY_VER)
    if platform.machine().lower() not in ("amd64", "x86_64"):
        fail("потрібна 64-бітна Windows")
    url = "https://www.python.org/ftp/python/%s/python-%s-embed-amd64.zip" % (PY_VER, PY_VER)
    archive = download(url, os.path.join(CACHE, os.path.basename(url)))
    py_dir = os.path.join(STAGE, "python")
    with zipfile.ZipFile(archive) as z:
        z.extractall(py_dir)
    pth = next(f for f in os.listdir(py_dir) if f.endswith("._pth"))
    stdlib_zip = next(f for f in os.listdir(py_dir) if f.startswith("python") and f.endswith(".zip"))
    with open(os.path.join(py_dir, pth), "w", encoding="ascii") as f:
        # app folder + site-packages (pip); "import site" so pip works
        f.write("%s\n.\n..\\app\nLib\\site-packages\nimport site\n" % stdlib_zip)
    site = os.path.join(py_dir, "Lib", "site-packages")
    os.makedirs(site, exist_ok=True)
    # pip for downloading components at run time (from PyPI, same wheels as `pip download pip`)
    # + pywebview: the program's own window (WebView2, built into Windows) — no browser UI or extensions
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q",
                           "--target", site, "--no-compile", "pip", "pywebview"])
    return py_dir


def stage_app():
    step("Код програми")
    app = os.path.join(STAGE, "app")
    shutil.copytree(os.path.join(HERE, "davinci_director"), os.path.join(app, "davinci_director"),
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    os.makedirs(os.path.join(app, "resolve_scripts"))
    shutil.copy2(os.path.join(HERE, "resolve_scripts", "DaVinci Director.lua"), os.path.join(app, "resolve_scripts"))
    shutil.copy2(os.path.join(HERE, "davinci_director_watcher.pyw"), app)
    shutil.copy2(os.path.join(HERE, "packaging", "app_entry.py"), os.path.join(app, "dd_entry.py"))
    return app


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-tests", action="store_true")
    args = ap.parse_args()
    if not sys.platform.startswith("win"):
        fail("збірка підтримується лише у Windows")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    ver = version()
    t0 = time.time()

    if not args.skip_tests:
        step("Тести")
        if subprocess.call([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=HERE) != 0:
            fail("тести не пройшли (запустіть з --skip-tests, щоб зібрати все одно)")

    shutil.rmtree(STAGE, ignore_errors=True)
    py_dir = stage_python()
    app = stage_app()

    step("Перевірка")
    report_path = os.path.join(BUILD, "selftest.json")
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}
    code = subprocess.call([os.path.join(py_dir, "python.exe"), os.path.join(app, "dd_entry.py"), "--selftest", report_path],
                           cwd=app, env=env)
    try:
        with open(report_path, encoding="utf-8") as f:
            report = json.load(f)
    except (OSError, ValueError):
        fail("програма не запустилась (selftest без звіту, код %s)" % code)
    for k, v in report["checks"].items():
        print("  %-15s %s" % (k, v))
    if not report["ok"]:
        fail("selftest знайшов проблеми")

    iscc = ensure_inno()
    step("Інсталятор")
    base = "DaVinciDirector-Setup-%s" % ver
    if subprocess.call([iscc, "/Q", "/DAppVersion=%s" % ver, "/DStageDir=%s" % STAGE, "/DOutputBase=%s" % base,
                        os.path.join(HERE, "packaging", "installer.iss")]) != 0:
        fail("Inno Setup завершився з помилкою")
    out = os.path.join(DIST, base + ".exe")
    print("\nГотово за %d с: %s (%.1f МБ)" % (time.time() - t0, out, os.path.getsize(out) / 1e6))
    print("Інсталятор без прав адміністратора; компоненти (Whisper/FFmpeg, CUDA, pyannote) і моделі\n"
          "програма завантажує сама, коли вони вперше потрібні.")


if __name__ == "__main__":
    main()
