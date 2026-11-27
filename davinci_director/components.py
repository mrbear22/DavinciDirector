"""Components downloaded while the program runs (not shipped in the installer).

    audio     PyAV (FFmpeg libraries), Silero VAD on onnxruntime: reading audio, finding speech
    speakers  torch + transformers (Parakeet speech recognition) + pyannote.audio (speaker detection)

How a component is installed (per user, no admin rights):
  1. ``pip install --dry-run --report`` resolves the exact files (URLs + SHA-256);
  2. ``downloader`` fetches them in parallel (big wheels in byte ranges), resumable;
  3. ``pip install --no-index --find-links`` installs from the local files into
     <program folder>\\components\\<name> (see storage.root()).
Each component has its own folder, so installing one never touches files of another
that is already loaded.
"""

import importlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from urllib.parse import unquote

from . import Cancelled, downloader

ORDER = ("audio", "speakers")
COMPONENTS = {
    # without dependencies: silero-vad would pull a second torch (it uses the one of "speakers"),
    # onnxruntime its developer tools; what they need at run time is listed here
    "audio": {"label": "аудіо й пошук мовлення (FFmpeg, Silero VAD)", "size": "≈ 60 МБ",
              "check": ["av", "numpy", "onnxruntime", "silero_vad"], "no_deps": True,
              "verify": ["av", "numpy", "onnxruntime"],  # silero_vad imports torch of "speakers"
              "pip": ["av", "numpy", "silero-vad", "onnxruntime", "flatbuffers", "protobuf", "packaging"]},
    # one torch for both: speech recognition (Parakeet through transformers) and speaker detection (pyannote)
    "speakers": {"label": "нейромережі: розпізнавання Parakeet і визначення мовців (torch, transformers, pyannote)",
                 "size": "≈ 1 ГБ", "check": ["torch", "transformers", "pyannote.audio"],
                 "pip": ["pyannote.audio", "transformers>=5.20,<6"]},
}

_lock = threading.Lock()
# Checks import heavy packages (torch takes seconds).  The transcribe dialog checks the components while a
# transcription started from it checks them too: two threads importing torch/pyannote at once can fail
# half-way, which used to look like "not installed" and started a reinstall over the loaded copy.
_check_lock = threading.RLock()
_ok = set()      # components that imported fine in this process
errors = {}      # name -> why the last check failed (shown to the user instead of a silent reinstall)
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class ComponentError(Exception):
    pass


def data_dir():
    """The program's folder (components/, models/, downloads/ live here), see storage.root()."""
    from .storage import root
    return root()


def components_dir():
    return os.path.join(data_dir(), "components")


def component_dir(name):
    return os.path.join(components_dir(), name)


def activate():
    """Make downloaded components importable (call at start and after an install)."""
    for name in ORDER:  # removals requested while the component was loaded
        marker = component_dir(name) + ".remove"
        if os.path.exists(marker) and component_dir(name) not in sys.path:
            shutil.rmtree(component_dir(name), ignore_errors=True)
            os.remove(marker)
        if component_dir(name) not in sys.path:  # leftovers of an interrupted install or replacement
            for suffix in (".old", ".new"):
                if os.path.isdir(component_dir(name) + suffix) and not _lock.locked():
                    shutil.rmtree(component_dir(name) + suffix, ignore_errors=True)
    for name in reversed(ORDER):
        d = component_dir(name)
        if os.path.isdir(d) and d not in sys.path:
            sys.path.insert(0, d)
    importlib.invalidate_caches()


# Speaker detection on the graphics card: PyPI has only CPU builds of torch for Windows, the CUDA builds are on
# the PyTorch index.  Versions are pinned together (torch/torchaudio/torchcodec must match) and were checked
# with pyannote.audio 4.1 on an RTX 4050 (driver CUDA 13.5).
TORCH_CUDA = {"min_driver_cuda": (13, 0), "index": "https://download.pytorch.org/whl/cu130",
              "pins": ["torch==2.11.0+cu130", "torchaudio==2.11.0+cu130"], "size": "≈ 3 ГБ"}


def driver_cuda():
    """Highest CUDA version the installed NVIDIA driver supports, e.g. (13, 5), or None."""
    exe = shutil.which("nvidia-smi") or r"C:\Windows\System32\nvidia-smi.exe"
    try:
        out = subprocess.run([exe], capture_output=True, text=True, timeout=15, creationflags=NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"CUDA (?:UMD )?Version:\s*(\d+)\.(\d+)", out or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


_cuda_cache = []


def speakers_on_gpu():
    if not _cuda_cache:
        v = driver_cuda() if nvidia_gpu() else None
        _cuda_cache.append(bool(v and v >= TORCH_CUDA["min_driver_cuda"]))
    return _cuda_cache[0]


def _torch_is_cpu_build(path):
    """A downloaded torch without CUDA libraries (checked on disk: importing torch would lock its files)."""
    lib = os.path.join(path, "torch", "lib")
    if not os.path.isdir(lib):
        return False
    return not any(f.lower().startswith(("cudart64", "cublas64")) for f in os.listdir(lib))


def pip_spec(name):
    """(packages, arguments for resolving, arguments for installing) on this computer."""
    spec = COMPONENTS[name]
    packages = list(spec["pip"])
    deps = ["--no-deps"] if spec.get("no_deps") else []
    if name == "speakers" and speakers_on_gpu():
        return packages + TORCH_CUDA["pins"], ["--extra-index-url", TORCH_CUDA["index"]], deps
    return packages, deps, deps


def installed(name):
    if name in _ok:
        return True
    with _check_lock:  # one check at a time: concurrent first imports of torch can break each other
        if name in _ok:
            return True
        activate()
        d = component_dir(name)
        if name == "speakers" and os.path.isdir(d) and speakers_on_gpu() and _torch_is_cpu_build(d):
            # an older install (CPU-only torch) on a computer with a suitable NVIDIA card: replace it before use
            errors[name] = "встановлено версію лише для процесора"
            ok = False
        elif name == "speakers" and os.path.isdir(d):
            # torch is checked in a separate Python: a broken copy must not get half-loaded into the app
            # (that locks its files, so it could never be replaced without yet another restart)
            # ~10 s (it imports torch): done once per installed copy, remembered in a marker file
            if _verified_mark(d) and _read(os.path.join(d, VERIFIED)) == _verified_mark(d):
                problem = ""
            else:
                problem = _verify(name, d, need_cuda=False)
                if not problem:
                    _write_mark(d)
            if problem:
                errors[name] = problem
            ok = not problem
        else:
            missing = [m for m in COMPONENTS[name]["check"] if importlib.util.find_spec(m) is None]
            if missing:
                errors[name] = "немає: " + ", ".join(missing)
            ok = not missing
        if ok:
            _ok.add(name)
            errors.pop(name, None)
        return ok


VERIFIED = ".dd-verified"


def _verified_mark(folder):
    """What a verified copy looks like: the installed package versions (*.dist-info) and Python."""
    try:
        infos = sorted(n for n in os.listdir(folder) if n.endswith(".dist-info"))
    except OSError:
        return ""
    return "%s\n%s" % (sys.version.split()[0], "\n".join(infos)) if infos else ""


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _write_mark(folder):
    """Without the mark the next start only checks the component again (seconds)."""
    with open(os.path.join(folder, VERIFIED), "w", encoding="utf-8") as f:
        f.write(_verified_mark(folder))


def _loaded_from(path):
    """True if this process already runs code from the folder (its files are locked and in use)."""
    root = os.path.normcase(os.path.abspath(path)) + os.sep
    for m in list(sys.modules.values()):
        f = getattr(m, "__file__", None)
        if f and os.path.normcase(os.path.abspath(f)).startswith(root):
            return True
    return False


def nvidia_gpu():
    return bool(shutil.which("nvidia-smi")) or os.path.isfile(r"C:\Windows\System32\nvidia-smi.exe")


def folder_size(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def status(disk=False):
    """disk=True also measures the folders (seconds for torch: tens of thousands of files) — only for
    the components window, never on the way to transcription."""
    out = {}
    for k in ORDER:
        d = component_dir(k)
        size = TORCH_CUDA["size"] if k == "speakers" and speakers_on_gpu() else COMPONENTS[k]["size"]
        out[k] = {"installed": installed(k), "label": COMPONENTS[k]["label"], "size": size, "local": os.path.isdir(d),
                  "disk": downloader.human(folder_size(d)) if disk and os.path.isdir(d) else ""}
    out["nvidia_gpu"] = nvidia_gpu()
    return out


def _python_exe():
    exe = sys.executable
    if os.path.basename(exe).lower() == "pythonw.exe":
        cand = os.path.join(os.path.dirname(exe), "python.exe")
        if os.path.isfile(cand):
            return cand
    return exe


_children = set()  # running pip processes: they outlive us on Windows unless killed


def kill_children():
    """Stop installs in progress (the program is closing)."""
    for p in list(_children):
        try:
            p.kill()
        except OSError:
            pass


def _pip(args, cancel=None, env=None):
    if env is None:  # pip's temp files go into the program's folder too
        work = os.path.join(data_dir(), "downloads", "pip-tmp")
        os.makedirs(work, exist_ok=True)
        env = dict(os.environ, TEMP=work, TMP=work)
    # --no-cache-dir: no pip cache in the user profile (wheels are downloaded by us, then deleted)
    proc = subprocess.Popen([_python_exe(), "-m", "pip"] + args + ["--disable-pip-version-check", "--no-input", "--no-cache-dir"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                            errors="replace", creationflags=NO_WINDOW, env=env)
    _children.add(proc)
    try:
        tail = []
        for line in proc.stdout:
            tail.append(line.rstrip())
            del tail[:-20]
            if cancel and cancel():
                proc.kill()
                raise Cancelled()
        proc.wait()
        if cancel and cancel():
            raise Cancelled()
        return proc.returncode, tail
    finally:
        _children.discard(proc)


def _plan(spec, report_path, cancel, extra=()):
    """Exact files to download (with SHA-256) as pip would install them."""
    code, tail = _pip(["install", "--dry-run", "--ignore-installed", "--prefer-binary", "--quiet",
                       "--report", report_path] + list(extra) + spec + ["setuptools", "wheel"], cancel)
    if code != 0:
        raise ComponentError("Не вдалося визначити, що завантажувати (немає інтернету?):\n%s" % "\n".join(tail[-4:]))
    with open(report_path, encoding="utf-8") as f:
        report = json.load(f)
    items = []
    for entry in report.get("install", []):
        info = entry.get("download_info") or {}
        url = info.get("url")
        if not url:
            continue
        sha = ((info.get("archive_info") or {}).get("hashes") or {}).get("sha256")
        # the PyTorch index URL-encodes local versions ("torch-2.11.0%2Bcu130-…whl"); pip needs the real file name
        name = unquote(url.rsplit("/", 1)[-1].split("#")[0])
        items.append({"url": url, "name": name, "sha256": sha})
    return items


def _unpacked_size(paths):
    """Bytes the wheels take once installed (from their zip directories, nothing is extracted)."""
    import zipfile
    total = 0
    for p in paths:
        try:
            with zipfile.ZipFile(p) as z:
                total += sum(i.file_size for i in z.infolist())
        except (OSError, zipfile.BadZipFile):
            total += os.path.getsize(p) if os.path.exists(p) else 0
    return total


def _install(packages, wheels, files, tmp, label, progress, cancel, args=()):
    """pip install from the downloaded wheels into ``tmp``, reporting progress by bytes written.
    pip unpacks into its temp folder first and then copies into the target, so the work is ~2x the size."""
    work = os.path.join(data_dir(), "downloads", "pip-tmp")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    env = dict(os.environ, TEMP=work, TMP=work)
    expected = 2 * _unpacked_size(files) or 1
    done = threading.Event()

    def watch():
        while not done.wait(1.5):
            written = folder_size(work) + folder_size(tmp)
            if progress:
                progress(min(0.99, written / expected), "%s: встановлення %s з %s" % (
                    label, downloader.human(min(written, expected) // 2), downloader.human(expected // 2)))

    t = threading.Thread(target=watch, daemon=True)
    t.start()
    try:
        return _pip(["install", "--no-index", "--find-links", wheels, "--target", tmp, "--no-warn-script-location",
                     "--quiet"] + list(args) + packages, cancel, env=env)
    finally:
        done.set()
        t.join(5)
        shutil.rmtree(work, ignore_errors=True)


def _verify(name, folder, need_cuda=True):
    """Import a copy of the component in a separate Python.  Returns a problem or ''."""
    modules = COMPONENTS[name].get("verify", COMPONENTS[name]["check"])
    code = ["import sys", "sys.path.insert(0, %r)" % folder] + ["import %s" % m for m in modules]
    if name == "speakers":
        code.append("from transformers import AutoModelForTDT, AutoProcessor")  # Parakeet support in this version
    if need_cuda and name == "speakers" and speakers_on_gpu():
        code.append("import torch; assert torch.cuda.is_available(), 'CUDA недоступна в torch %s' % torch.__version__")
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    try:
        res = subprocess.run([_python_exe(), "-c", "; ".join(code)], capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=300, creationflags=NO_WINDOW, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        return str(e)
    if res.returncode == 0:
        return ""
    lines = [l for l in (res.stderr or res.stdout or "").strip().splitlines() if l.strip()]
    return "\n".join(lines[-3:]) or "код %s" % res.returncode


def _swap(tmp, target, label):
    """Put the new copy in place as a whole.  The old folder is moved aside first: if Windows refuses
    (its files are in use), nothing is changed — files of two versions are never mixed."""
    old = target + ".old"
    shutil.rmtree(old, ignore_errors=True)
    if os.path.exists(target):
        try:
            os.replace(target, old)
        except OSError:
            shutil.rmtree(tmp, ignore_errors=True)
            raise ComponentError("%s: попередня копія зараз використовується, тому її не можна замінити. "
                                 "Перезапустіть DaVinci Director і спробуйте ще раз." % label)
    os.replace(tmp, target)
    shutil.rmtree(old, ignore_errors=True)  # whatever is left is removed on the next start (activate)


DOWNLOAD_SHARE = 0.6  # of one progress bar; installing is the rest (unpacking 3 GB of torch takes minutes)


def _label(name):
    t = COMPONENTS[name]["label"]
    return t[:1].upper() + t[1:]


def _download(name, packages, plan_args, wheels, progress, cancel):
    """Wheels of the component into ``wheels``; returns their paths."""
    items = _plan(packages, os.path.join(data_dir(), "downloads", "%s-plan.json" % name), cancel, plan_args)
    try:
        downloader.download_many(
            [{"url": it["url"], "dest": os.path.join(wheels, it["name"]), "sha256": it["sha256"]} for it in items],
            progress=progress, cancel=cancel, label=_label(name))
    except downloader.DownloadError as e:
        raise ComponentError("%s: %s" % (_label(name), e))
    return [os.path.join(wheels, it["name"]) for it in items]


def _install_verified(name, packages, install_args, wheels, files, progress, cancel):
    """Installs into a fresh folder and puts it in place only when it imports fine."""
    tmp = component_dir(name) + ".new"
    shutil.rmtree(tmp, ignore_errors=True)
    code, tail = _install(packages, wheels, files, tmp, _label(name), progress, cancel, install_args)
    if code != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        raise ComponentError("Не вдалося встановити %s:\n%s" % (COMPONENTS[name]["label"], "\n".join(tail[-4:])))
    if progress:
        progress(0.98, "%s: перевірка…" % _label(name))
    problem = _verify(name, tmp)
    if problem:
        shutil.rmtree(tmp, ignore_errors=True)
        raise ComponentError("%s: встановлена копія не працює, попередню залишено без змін.\n%s" % (_label(name), problem))
    _write_mark(tmp)  # just verified: the next start does not import torch again to check it
    _swap(tmp, component_dir(name), _label(name))


def _remove_wheels(wheels):
    for f in os.listdir(wheels):
        if f.endswith((".whl", ".tar.gz", ".zip")):
            os.remove(os.path.join(wheels, f))


def ensure(name, progress=None, cancel=None):
    """Download and install a component if it is missing.  Returns True if installed now."""
    if installed(name):
        return False
    target = component_dir(name)
    wheels = os.path.join(data_dir(), "downloads", "wheels")
    with _lock:
        if installed(name):
            return False
        if os.path.isdir(target) and _loaded_from(target):
            # replacing it now would delete files of a library that is running; a fresh start does it cleanly
            raise ComponentError("%s: %s. Цю копію вже використано в поточній сесії, тому замінити її зараз не можна — "
                                 "перезапустіть DaVinci Director." % (_label(name), errors.get(name, "не завантажився")))
        packages, plan_args, install_args = pip_spec(name)
        os.makedirs(wheels, exist_ok=True)
        if progress:
            progress(0.0, "%s: визначаю, що завантажити…" % _label(name))

        def share(a, b):
            return (lambda x, msg: progress(a + (b - a) * x, msg)) if progress else None

        files = _download(name, packages, plan_args, wheels, share(0.0, DOWNLOAD_SHARE), cancel)
        _install_verified(name, packages, install_args, wheels, files, share(DOWNLOAD_SHARE, 0.97), cancel)
        _remove_wheels(wheels)
    activate()
    if progress:
        progress(1.0, "%s: встановлено" % _label(name))
    return True


def remove(name):
    d = component_dir(name)
    _ok.discard(name)
    if not os.path.isdir(d):
        return False
    if d in sys.path:  # loaded in this process: delete on the next start
        open(d + ".remove", "w").close()
        return "later"
    shutil.rmtree(d)
    return True
