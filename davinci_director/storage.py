"""Persistent storage: projects, settings, caches.

Everything the program keeps lives in ONE folder — the one chosen in the installer (by default next to
DaVinci Resolve's own user files):

    %APPDATA%\\Blackmagic Design\\DaVinci Resolve\\Support\\DaVinci Director\\   (or the chosen folder)
        app\\, python\\        the program (installer)
        data\\                settings, projects, transcripts, logs, caches
        components\\          downloaded libraries (audio, torch/transformers/pyannote)
        models\\, hf\\         speech-recognition and speaker-detection models
        downloads\\           partial downloads (resumable)

Libraries that would otherwise write into the user profile (Hugging Face cache, pip cache, matplotlib,
torch) are pointed into this folder by ``setup_env``.  The only things outside it: the exchange folder
with the Resolve script in %TEMP% (Resolve's script sandbox can write nowhere else) and the autostart
entry in the registry.
"""

import json
import os
import re
import shutil
import threading
import time


def installed_root(pkg_dir=None):
    """The folder the user chose in the installer: <root>\\app\\davinci_director\\ next to <root>\\python\\."""
    pkg_dir = pkg_dir or os.path.dirname(os.path.abspath(__file__))
    app = os.path.dirname(pkg_dir)
    cand = os.path.dirname(app)
    if os.path.basename(app).lower() == "app" and os.path.isfile(os.path.join(cand, "python", "python.exe")):
        return cand
    return None


def root():
    """The program's folder: where it is installed (the user picks it in the installer); for a source
    checkout the default one next to Resolve's user files.  DD_ROOT overrides (tests)."""
    if os.environ.get("DD_ROOT"):
        return os.environ["DD_ROOT"]
    inst = installed_root()
    if inst:
        return inst
    if os.name == "nt" and os.environ.get("APPDATA"):
        return os.path.join(os.environ["APPDATA"], "Blackmagic Design", "DaVinci Resolve", "Support", "DaVinci Director")
    return os.path.join(os.path.expanduser("~"), ".davinci_director")


def base_dir():
    d = os.environ.get("DD_HOME") or os.path.join(root(), "data")
    os.makedirs(d, exist_ok=True)
    return d


def setup_env():
    """Point library caches into the program's folder and bring data over from older versions.
    Call once at start, before importing huggingface_hub / torch / pyannote."""
    r = root()
    os.makedirs(r, exist_ok=True)
    for var, sub in (("HF_HOME", "hf"), ("MPLCONFIGDIR", os.path.join("data", "cache", "matplotlib")),
                     ("TORCH_HOME", os.path.join("data", "cache", "torch"))):
        os.environ.setdefault(var, os.path.join(r, sub))
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("PIP_NO_CACHE_DIR", "1")
    try:
        migrate_old_locations()
    except Exception as e:  # never stop the program because of a leftover folder
        print("Old data not moved: %s" % e)


# what earlier versions kept elsewhere, and what is ours in the shared Hugging Face cache
_OLD_HF = ["models--ihnatov-yuri--parakeet-tdt-0.6b-v3-uk", "models--pyannote--speaker-diarization-community-1"]
_OBSOLETE = [os.path.join("components", "gpu"),  # CUDA for the former Whisper engine
             os.path.join("models", "ihnatov-yuri--whisper-large-v3-turbo-uk")]


def _move(src, dst):
    if not os.path.exists(src) or os.path.exists(dst):
        return False
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.replace(src, dst)  # same drive: instant
    except OSError:
        shutil.move(src, dst)
    return True


def migrate_old_locations():
    """Move data of versions ≤ 2.5 into the program's folder (once; nothing is downloaded again)."""
    if os.environ.get("DD_ROOT") or os.environ.get("DD_HOME"):
        return  # tests / custom setups
    r = root()
    marker = os.path.join(r, "data", ".migrated")
    if os.path.exists(marker):
        return
    moved = []
    home = os.path.expanduser("~")
    if _move(os.path.join(home, ".davinci_director"), os.path.join(r, "data")):
        moved.append("data")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        old = os.path.join(local, "DaVinciDirector")
        for rel in _OBSOLETE:  # unused since the switch to Parakeet: not moved, removed
            shutil.rmtree(os.path.join(old, rel), ignore_errors=True)
        for sub in ("components", "models", "downloads"):
            if _move(os.path.join(old, sub), os.path.join(r, sub)):
                moved.append(sub)
        try:
            if os.path.isdir(old) and not os.listdir(old):
                os.rmdir(old)
        except OSError:
            pass
    hub = os.path.join(os.environ.get("HF_HOME") or os.path.join(r, "hf"), "hub")
    for name in _OLD_HF:  # models this program downloaded into the shared cache
        if _move(os.path.join(home, ".cache", "huggingface", "hub", name), os.path.join(hub, name)):
            moved.append(name)
    os.makedirs(os.path.dirname(marker), exist_ok=True)
    with open(marker, "w", encoding="utf-8") as f:
        f.write("\n".join(moved))
    if moved:
        print("Moved into %s: %s" % (r, ", ".join(moved)))


def cache_dir(kind):
    d = os.path.join(base_dir(), "cache", kind)
    os.makedirs(d, exist_ok=True)
    return d


def projects_dir():
    d = os.path.join(base_dir(), "projects")
    os.makedirs(d, exist_ok=True)
    return d


def project_path(project_name, timeline_id):
    safe = re.sub(r"[^\w\-]+", "_", "%s__%s" % (project_name or "project", timeline_id or "timeline"))[:150]
    return os.path.join(projects_dir(), safe + ".json")


def atomic_write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
    # keep one rolling backup to survive a corrupted write
    try:
        if os.path.getsize(path) > 0:
            bak = path + ".bak"
            if not os.path.exists(bak) or time.time() - os.path.getmtime(bak) > 300:
                with open(path, "rb") as src, open(bak, "wb") as dst:
                    dst.write(src.read())
    except OSError:
        pass


DEFAULT_SETTINGS = {
    "whisper_model": "large-v3",
    "whisper_device": "auto",
    "whisper_compute_type": "auto",
    "language": "",
    "diarize": True,
    "hf_token": "",
    "num_speakers": 0,
    "use_gpu": True,
}


class Settings:
    def __init__(self):
        self.path = os.path.join(base_dir(), "settings.json")
        self._lock = threading.Lock()
        self.data = dict(DEFAULT_SETTINGS)
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    self.data.update(json.load(f))
            except (OSError, ValueError):
                pass

    def get(self, key):
        if key == "hf_token":
            return self.data.get(key) or os.environ.get("HF_TOKEN", "")
        return self.data.get(key, DEFAULT_SETTINGS.get(key))

    def update(self, values):
        with self._lock:
            for k, v in values.items():
                if k in DEFAULT_SETTINGS:
                    self.data[k] = v
            atomic_write(self.path, json.dumps(self.data, indent=2, ensure_ascii=False))
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    def public(self):
        out = {k: v for k, v in self.data.items() if k != "hf_token"}
        out["has_hf_token"] = bool(self.get("hf_token"))
        return out


class DebouncedSaver:
    """Writes the project to disk at most every ``delay`` seconds."""

    def __init__(self, delay=1.0):
        self.delay = delay
        self._pending = None
        self._lock = threading.Lock()
        self._timer = None

    def schedule(self, path, producer):
        with self._lock:
            self._pending = (path, producer)
            if self._timer is None:
                self._timer = threading.Timer(self.delay, self.flush)
                self._timer.daemon = True
                self._timer.start()

    def flush(self):
        with self._lock:
            pending, self._pending, self._timer = self._pending, None, None
        if pending:
            path, producer = pending
            atomic_write(path, producer())
