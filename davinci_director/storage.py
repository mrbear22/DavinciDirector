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
import threading


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
    """Point library caches into the program's folder.
    Call once at start, before importing huggingface_hub / torch / pyannote."""
    r = root()
    os.makedirs(r, exist_ok=True)
    for var, sub in (("HF_HOME", "hf"), ("MPLCONFIGDIR", os.path.join("data", "cache", "matplotlib")),
                     ("TORCH_HOME", os.path.join("data", "cache", "torch"))):
        os.environ.setdefault(var, os.path.join(r, sub))
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("PIP_NO_CACHE_DIR", "1")


def cache_dir(kind):
    d = os.path.join(base_dir(), "cache", kind)
    os.makedirs(d, exist_ok=True)
    return d


def projects_dir():
    d = os.path.join(base_dir(), "projects")
    os.makedirs(d, exist_ok=True)
    return d


def atomic_write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)  # atomic: a crash leaves the old file or the new one, never half of it


DEFAULT_SETTINGS = {
    "diarize": True,
    "hf_token": "",
    "num_speakers": 0,
}


class Settings:
    def __init__(self):
        self.path = os.path.join(base_dir(), "settings.json")
        self._lock = threading.Lock()
        self.data = dict(DEFAULT_SETTINGS)
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    saved = json.load(f)
                self.data.update({k: v for k, v in saved.items() if k in DEFAULT_SETTINGS})
            except (OSError, ValueError) as e:  # broken file: defaults (the token has to be entered again)
                print("Settings not read (%s): %r" % (self.path, e))

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

    def public(self):
        out = {k: v for k, v in self.data.items() if k != "hf_token"}
        out["has_hf_token"] = bool(self.get("hf_token"))
        return out
