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

from . import Cancelled

USER_AGENT = "DaVinciDirector-downloader"
SEGMENT = 16 * 1024 * 1024  # bytes per range request
CHUNK = 256 * 1024
RETRIES = 6
DISK_RESERVE = 200 * 1024 * 1024  # bytes kept free besides the download (+10 %)
PROGRESS_EVERY = 0.3  # s


class DownloadError(Exception):
    pass


def human(n):
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return ("%.0f %s" if unit in ("Б", "КБ") else "%.1f %s") % (n, unit)
        n /= 1024.0


def _eta(sec):
    sec = int(sec)
    if sec >= 3600:
        return "%d год %02d хв" % (sec // 3600, sec // 60 % 60)
    if sec >= 60:
        return "%d хв %02d с" % (sec // 60, sec % 60)
    return "%d с" % sec


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


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


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
            if not self.callback or (now - self._last < PROGRESS_EVERY and self.done < self.total):
                return
            self._last = now
            speed = self.done / max(now - self.t0, 0.001)
            left = (self.total - self.done) / speed if speed > 0 else 0
            self.callback(self.done / self.total if self.total else 0.0, "%s: %s / %s · %s/с · ще %s" % (
                self.label, human(self.done), human(self.total), human(speed), _eta(left)))


def _retrying(fetch):
    """Runs ``fetch()`` again after network errors, with back-off; cancelling is never retried."""
    for attempt in range(RETRIES):
        try:
            return fetch()
        except Cancelled:
            raise
        except (OSError, urllib.error.URLError, DownloadError) as e:
            if attempt == RETRIES - 1:
                raise DownloadError("обрив завантаження (%s)" % e)
            time.sleep(min(2 ** attempt, 20))


def _copy(response, f, limit, prog, cancel):
    """Response body (at most ``limit`` bytes, None = all) -> file; returns the number of bytes."""
    got = 0
    while limit is None or got < limit:
        if cancel():
            raise Cancelled()
        block = response.read(CHUNK if limit is None else min(CHUNK, limit - got))
        if not block:
            break
        f.write(block)
        got += len(block)
        prog.add(len(block))
    return got


class _File:
    """One file being downloaded: whole, or as byte ranges remembered in ``<dest>.part.json``."""

    def __init__(self, item, ranges):
        self.url, self.dest, self.size, self.sha256 = item["url"], item["dest"], item["size"], item.get("sha256")
        self.part, self.meta_path = self.dest + ".part", self.dest + ".part.json"
        self.ranged = ranges and self.size > 2 * SEGMENT
        self._lock = threading.Lock()
        self._have = {}  # range start -> bytes of it kept from a broken attempt
        self.done = self._resume() if self.ranged else set()
        self.segments = ([(s, min(s + SEGMENT, self.size) - 1) for s in range(0, self.size, SEGMENT)]
                         if self.ranged else [(None, None)])
        self.left = len([s for s in self.segments if s[0] not in self.done])

    def _resume(self):
        """Finished ranges of an earlier attempt of the very same file."""
        key = self.sha256 or self.url
        try:
            with open(self.meta_path, encoding="utf-8") as f:
                old = json.load(f)
            if old.get("size") == self.size and old.get("key") == key and os.path.getsize(self.part) == self.size:
                return set(old["done"])
        except (OSError, ValueError, KeyError):
            pass  # nothing to resume: start over
        with open(self.part, "wb") as f:
            f.truncate(self.size)
        return set()

    def bytes_done(self):
        return sum(e - s + 1 for s, e in self.segments if s in self.done)

    def todo(self):
        return [(self, s, e) for s, e in self.segments if s not in self.done]

    def fetch(self, start, end, prog, cancel):
        if start is None:
            _retrying(lambda: self._fetch_whole(prog, cancel))
        else:
            _retrying(lambda: self._fetch_range(start, end, prog, cancel))
        with self._lock:
            if start is not None:
                self.done.add(start)
                with open(self.meta_path, "w", encoding="utf-8") as f:
                    json.dump({"size": self.size, "key": self.sha256 or self.url, "done": sorted(self.done)}, f)
            self.left -= 1
            if self.left:
                return
        self._finish()

    def _fetch_range(self, start, end, prog, cancel):
        have = self._have.get(start, 0)
        pos = start + have
        with _open(self.url, pos, end) as r, open(self.part, "r+b") as f:
            if r.status != 206:
                raise DownloadError("сервер не підтримує докачування")
            f.seek(pos)
            got = _copy(r, f, end - pos + 1, prog, cancel)
        self._have[start] = have + got
        if pos + got <= end:
            raise DownloadError("неповна відповідь сервера")

    def _fetch_whole(self, prog, cancel):
        got = 0
        try:
            with _open(self.url) as r, open(self.part, "wb") as f:
                got = _copy(r, f, None, prog, cancel)
        except (OSError, urllib.error.URLError):
            prog.add(-got)
            raise

    def _finish(self):
        if self.sha256 and _sha256(self.part) != self.sha256.lower():
            for p in (self.part, self.meta_path):
                if os.path.exists(p):
                    os.remove(p)
            raise DownloadError("пошкоджений файл %s (не збігся SHA-256) — запустіть ще раз" % os.path.basename(self.dest))
        os.replace(self.part, self.dest)
        if os.path.exists(self.meta_path):
            os.remove(self.meta_path)


def _sizes(items, connections):
    """Fills in sizes the source did not give; returns [(item, ranges_supported)]."""
    with ThreadPoolExecutor(max_workers=min(connections, len(items))) as pool:
        probes = [pool.submit(probe_size, it["url"]) for it in items]
        out = []
        for it, fu in zip(items, probes):
            size, ranges = fu.result()
            it["size"] = it.get("size") or size
            out.append((it, ranges))
    return out


def _check_disk(path, total):
    os.makedirs(path, exist_ok=True)
    free = shutil.disk_usage(path).free
    if total and free < total * 1.1 + DISK_RESERVE:
        raise DownloadError("Недостатньо місця на диску: потрібно %s, вільно %s" % (human(total * 1.1), human(free)))


def _run_all(tasks, prog, user_cancel, connections):
    """Fetches every (file, start, end); the first failure (or the user) stops all of them."""
    stop = threading.Event()

    def cancel():
        return stop.is_set() or bool(user_cancel and user_cancel())

    def run(task):
        f, s, e = task
        try:
            f.fetch(s, e, prog, cancel)
        except BaseException:
            stop.set()
            raise

    tasks.sort(key=lambda t: -t[0].size)  # big files first so their ranges spread over all connections
    with ThreadPoolExecutor(max_workers=connections) as pool:
        futures = [pool.submit(run, t) for t in tasks]
        errors = []
        for fu in futures:
            try:
                fu.result()
            except (DownloadError, Cancelled) as e:
                errors.append(e)
    # the real cause, not the cancellations it triggered in the other workers
    real = [e for e in errors if not isinstance(e, Cancelled)]
    if real or errors:
        raise (real or errors)[0]


def download_many(items, progress=None, cancel=None, label="Завантаження", connections=8):
    """items: [{"url", "dest", "size"?, "sha256"?}].  Already complete files are skipped."""
    todo = [dict(it) for it in items
            if not (os.path.isfile(it["dest"]) and (not it.get("size") or os.path.getsize(it["dest"]) == it["size"]))]
    if not todo:
        if progress:
            progress(1.0, "%s: уже є" % label)
        return
    sized = _sizes(todo, connections)
    total = sum(it["size"] for it, _ in sized)
    _check_disk(os.path.dirname(todo[0]["dest"]), total)
    for it, _ in sized:
        os.makedirs(os.path.dirname(it["dest"]), exist_ok=True)
    files = [_File(it, ranges) for it, ranges in sized]
    prog = Progress(total, progress, label)
    prog.add(sum(f.bytes_done() for f in files))
    _run_all([t for f in files for t in f.todo()], prog, cancel, connections)
    if progress:
        progress(1.0, "%s: готово (%s)" % (label, human(total)))
