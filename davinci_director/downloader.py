"""Parallel, resumable downloads for components (wheels) and models.

* many files at once, large files split into byte ranges fetched concurrently;
* resume: a ``.part`` file plus a small ``.part.json`` with the finished ranges;
* retries with back-off, cancel at any time;
* SHA-256 verification when the source publishes it (PyPI, Hugging Face LFS);
* free disk space is checked before starting;
* progress: bytes done / total, speed, time left.
"""

import hashlib
import json
import os
import shutil
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

USER_AGENT = "DaVinciDirector-downloader"
SEGMENT = 16 * 1024 * 1024  # bytes per range request
CHUNK = 256 * 1024


class DownloadError(Exception):
    pass


class Cancelled(DownloadError):
    pass


def human(n):
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return ("%.0f %s" if unit in ("Б", "КБ") else "%.1f %s") % (n, unit)
        n /= 1024.0


def _open(url, start=None, end=None, timeout=60):
    headers = {"User-Agent": USER_AGENT}
    if start is not None:
        headers["Range"] = "bytes=%d-%s" % (start, "" if end is None else end)
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout)


def probe_size(url):
    """(size, ranges_supported) via a 1-byte range request (works through redirects)."""
    try:
        with _open(url, 0, 0, timeout=30) as r:
            cr = r.headers.get("Content-Range", "")
            if r.status == 206 and "/" in cr:
                return int(cr.rsplit("/", 1)[1]), True
            return int(r.headers.get("Content-Length") or 0), False
    except urllib.error.HTTPError as e:
        if e.code == 416:
            return 0, False
        raise


class Progress:
    def __init__(self, total, callback, label):
        self.total, self.done, self.callback, self.label = total, 0, callback, label
        self.t0 = time.time()
        self._lock = threading.Lock()
        self._last = 0.0

    def add(self, n):
        with self._lock:
            self.done += n
            now = time.time()
            if self.callback and (now - self._last > 0.3 or self.done >= self.total):
                self._last = now
                el = max(now - self.t0, 0.001)
                speed = self.done / el
                left = (self.total - self.done) / speed if speed > 0 else 0
                frac = self.done / self.total if self.total else 0.0
                self.callback(frac, "%s: %s / %s · %s/с · ще %s" % (
                    self.label, human(self.done), human(self.total), human(speed), _eta(left)))


def _eta(sec):
    sec = int(sec)
    if sec >= 3600:
        return "%d год %02d хв" % (sec // 3600, sec // 60 % 60)
    if sec >= 60:
        return "%d хв %02d с" % (sec // 60, sec % 60)
    return "%d с" % sec


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _fetch_range(url, path, start, end, prog, cancel, retries=6):
    """Download bytes [start, end] into ``path`` at offset ``start``; resumes within the range."""
    pos = start
    for attempt in range(retries):
        try:
            with _open(url, pos, end) as r, open(path, "r+b") as f:
                if r.status != 206 and pos > 0:
                    raise DownloadError("сервер не підтримує докачування")
                f.seek(pos)
                while pos <= end:
                    if cancel and cancel():
                        raise Cancelled("cancelled")
                    block = r.read(min(CHUNK, end - pos + 1))
                    if not block:
                        break
                    f.write(block)
                    pos += len(block)
                    prog.add(len(block))
            if pos > end:
                return
        except Cancelled:
            raise
        except (OSError, urllib.error.URLError, DownloadError) as e:
            if attempt == retries - 1:
                raise DownloadError("обрив завантаження (%s)" % e)
            time.sleep(min(2 ** attempt, 20))
    if pos <= end:
        raise DownloadError("неповна відповідь сервера")


def _fetch_whole(url, path, prog, cancel, retries=6):
    for attempt in range(retries):
        try:
            got = 0
            with _open(url) as r, open(path, "wb") as f:
                while True:
                    if cancel and cancel():
                        raise Cancelled("cancelled")
                    block = r.read(CHUNK)
                    if not block:
                        return
                    f.write(block)
                    got += len(block)
                    prog.add(len(block))
        except Cancelled:
            raise
        except (OSError, urllib.error.URLError) as e:
            prog.add(-got)
            if attempt == retries - 1:
                raise DownloadError("обрив завантаження (%s)" % e)
            time.sleep(min(2 ** attempt, 20))


def download_many(items, progress=None, cancel=None, label="Завантаження", connections=8):
    """items: [{"url", "dest", "size"?, "sha256"?}].  Already complete files are skipped.
    Up to ``connections`` parallel HTTP connections in total."""
    todo = []
    for it in items:
        dest = it["dest"]
        if os.path.isfile(dest) and (not it.get("size") or os.path.getsize(dest) == it["size"]):
            continue
        todo.append(dict(it))
    if not todo:
        if progress:
            progress(1.0, "%s: уже є" % label)
        return

    # sizes (parallel probes for those the source did not give)
    with ThreadPoolExecutor(max_workers=min(connections, len(todo))) as pool:
        probes = {i: pool.submit(probe_size, it["url"]) for i, it in enumerate(todo)}
        for i, it in enumerate(todo):
            size, ranges = probes[i].result()
            it["size"] = it.get("size") or size
            it["ranges"] = ranges and it["size"] > 2 * SEGMENT

    total = sum(it["size"] for it in todo)
    dest_dir = os.path.dirname(todo[0]["dest"])
    os.makedirs(dest_dir, exist_ok=True)
    free = shutil.disk_usage(dest_dir).free
    if total and free < total * 1.1 + 200 * 1024 * 1024:
        raise DownloadError("Недостатньо місця на диску: потрібно %s, вільно %s" % (human(total * 1.1), human(free)))

    prog = Progress(total, progress, label)
    tasks = []  # (callable)
    states = []
    for it in todo:
        part = it["dest"] + ".part"
        meta_path = part + ".json"
        os.makedirs(os.path.dirname(it["dest"]), exist_ok=True)
        if it["ranges"]:
            meta = {"size": it["size"], "url_key": it.get("sha256") or it["url"], "done": []}
            try:
                with open(meta_path, encoding="utf-8") as f:
                    old = json.load(f)
                if old.get("size") == it["size"] and old.get("url_key") == meta["url_key"] and os.path.isfile(part):
                    meta = old
            except (OSError, ValueError):
                pass
            if not os.path.isfile(part) or os.path.getsize(part) != it["size"]:
                with open(part, "wb") as f:
                    f.truncate(it["size"])
                meta["done"] = []
            done = set(meta["done"])
            segs = [(s, min(s + SEGMENT, it["size"]) - 1) for s in range(0, it["size"], SEGMENT)]
            prog.add(sum(e - s + 1 for s, e in segs if s in done))
            st = {"it": it, "part": part, "meta": meta, "meta_path": meta_path, "lock": threading.Lock(),
                  "left": len([s for s in segs if s[0] not in done])}
            states.append(st)
            for s, e in segs:
                if s not in done:
                    tasks.append((st, s, e))
        else:
            st = {"it": it, "part": part, "meta": None, "meta_path": None, "lock": threading.Lock(), "left": 1}
            states.append(st)
            tasks.append((st, None, None))

    stop = threading.Event()
    user_cancel = cancel

    def cancel():  # noqa: F811 — stop every worker when one fails or the user cancels
        return stop.is_set() or bool(user_cancel and user_cancel())

    def run(task):
        try:
            _run(task)
        except BaseException:
            stop.set()
            raise

    def _run(task):
        st, s, e = task
        it = st["it"]
        if s is None:
            _fetch_whole(it["url"], st["part"], prog, cancel)
        else:
            _fetch_range(it["url"], st["part"], s, e, prog, cancel)
            with st["lock"]:
                st["meta"]["done"].append(s)
                with open(st["meta_path"], "w", encoding="utf-8") as f:
                    json.dump(st["meta"], f)
        with st["lock"]:
            st["left"] -= 1
            finished = st["left"] == 0
        if finished:
            _finish(st)

    def _finish(st):
        it = st["it"]
        if it.get("sha256") and _sha256(st["part"]) != it["sha256"].lower():
            for p in (st["part"], st["meta_path"]):
                if p and os.path.exists(p):
                    os.remove(p)
            raise DownloadError("пошкоджений файл %s (не збігся SHA-256) — запустіть ще раз" % os.path.basename(it["dest"]))
        os.replace(st["part"], it["dest"])
        if st["meta_path"] and os.path.exists(st["meta_path"]):
            os.remove(st["meta_path"])

    # big files first so their segments spread over all connections
    tasks.sort(key=lambda t: -t[0]["it"]["size"])
    with ThreadPoolExecutor(max_workers=connections) as pool:
        futures = [pool.submit(run, t) for t in tasks]
        errors = []
        for fu in futures:
            try:
                fu.result()
            except DownloadError as e:
                errors.append(e)
        real = [e for e in errors if not isinstance(e, Cancelled)]
        if real:
            raise real[0]
        if errors:
            raise errors[0]
    if progress:
        progress(1.0, "%s: готово (%s)" % (label, human(total)))
