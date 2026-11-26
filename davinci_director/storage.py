"""Persistent storage: projects, settings, caches (~/.davinci_director)."""

import json
import os
import re
import threading
import time


def base_dir():
    d = os.environ.get("DD_HOME") or os.path.join(os.path.expanduser("~"), ".davinci_director")
    os.makedirs(d, exist_ok=True)
    return d


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
    "anthropic_api_key": "",
    "claude_model": "claude-opus-5-5",
    "claude_effort": "high",
    "whisper_model": "large-v3",
    "whisper_device": "auto",
    "whisper_compute_type": "auto",
    "language": "",
    "diarize": True,
    "hf_token": "",
    "num_speakers": 0,
    "auto_sync": False,
    "edit_timeline_suffix": " — DD Edit",
    "ui_font_size": 13,
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
        if key == "anthropic_api_key":
            return self.data.get(key) or os.environ.get("ANTHROPIC_API_KEY", "")
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
        out = {k: v for k, v in self.data.items() if k not in ("anthropic_api_key", "hf_token")}
        out["has_api_key"] = bool(self.get("anthropic_api_key"))
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
