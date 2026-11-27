"""Connection to DaVinci Resolve.

Two interchangeable links expose the same small API:

    link.run(ops) -> {"ok", "done", "err", ...}   execute timeline operations
    link.snapshot                                   last known timeline state
    link.connected                                  bridge alive?

* ``FileBridgeLink`` talks to ``DaVinci Director.lua`` running inside Resolve
  (Workspace > Scripts).  Works with the free version: commands are folder
  names in ``<bridge>/in``, answers are file names in ``<bridge>/out``, the
  timeline state is an OTIO export in ``<bridge>/state``.
* ``DirectLink`` calls the scripting API in-process (Resolve Studio external
  scripting, or the mock used by tests/demo) with identical semantics.

Operations (dicts) — item references are ``{"t": "video", "i": 1, "s": startFrame}``:

    trim_end   {ref, n}        SetRightOffset(GetRightOffset()+n)  (n>0 shortens)
    trim_start {ref, n}        SetLeftOffset(GetLeftOffset()+n)    (no ripple)
    append     {clips:[{via|path, s, e, rec, track, mt}]}  source frames, e exclusive
    delete     {refs}          DeleteClips(items + linked items)
    move       {refs, to}      MoveItems(items, {startFrame: to})  leftmost item -> to
    go         {tc}            SetCurrentTimecode
    page / new_timeline / duplicate_timeline / set_timeline / timelines / marker / delete_markers / state / quit
"""

import json
import os
import shutil
import threading
import time
import urllib.parse

# ------------------------------------------------------------------ snapshot


def tc_to_frame(tc, fps):
    """'01:00:05:12' -> absolute frame number (non-drop)."""
    try:
        parts = [int(x) for x in str(tc).replace(";", ":").split(":")]
    except ValueError:
        return None
    if len(parts) != 4:
        return None
    nominal = int(round(fps)) or 25
    h, m, s, f = parts
    return ((h * 60 + m) * 60 + s) * nominal + f


def frame_to_tc(frame, fps):
    nominal = int(round(fps)) or 25
    frame = max(0, int(frame))
    f = frame % nominal
    s = frame // nominal
    return "%02d:%02d:%02d:%02d" % (s // 3600, (s // 60) % 60, s % 60, f)


def norm_path(p):
    return (p or "").replace("\\", "/").lower()


def _rt(d):
    """OTIO RationalTime dict -> (value, rate)."""
    if not isinstance(d, dict):
        return 0.0, 0.0
    return float(d.get("value") or 0.0), float(d.get("rate") or 0.0)


def _secs(d, fallback_rate):
    v, r = _rt(d)
    r = r or fallback_rate or 1.0
    return v / r


def _url_to_path(url):
    if not url:
        return ""
    if url.startswith("file:"):
        p = urllib.parse.unquote(urllib.parse.urlparse(url).path)
        if len(p) > 2 and p[0] == "/" and p[2] == ":":  # /C:/...
            p = p[1:]
        return p.replace("/", os.sep) if os.sep == "\\" else p
    return url


def _media_ref(clip):
    refs = clip.get("media_references")
    if isinstance(refs, dict):
        key = clip.get("active_media_reference_key") or "DEFAULT_MEDIA"
        return refs.get(key) or next(iter(refs.values()), {}) or {}
    return clip.get("media_reference") or {}


def parse_otio(text, fps_hint=None):
    """Resolve OTIO export -> snapshot dict (see DirectLink.read_snapshot)."""
    data = json.loads(text)
    g_val, g_rate = _rt(data.get("global_start_time"))
    fps = g_rate or float(fps_hint or 0) or 24.0
    start = int(round(g_val))
    tracks = {}
    counters = {"Video": 0, "Audio": 0}
    for track in (data.get("tracks") or {}).get("children", []):
        kind = track.get("kind") or "Video"
        if kind not in counters:
            continue
        counters[kind] += 1
        ttype = kind.lower()
        tindex = counters[kind]
        pos = 0.0  # seconds from timeline start
        out = []
        for child in track.get("children", []):
            schema = child.get("OTIO_SCHEMA", "")
            if schema.startswith("Transition"):
                continue
            sr = child.get("source_range") or {}
            dur = _secs(sr.get("duration"), fps)
            if schema.startswith("Clip"):
                ref = _media_ref(child)
                avail = ref.get("available_range") or {}
                st_v, st_r = _rt(sr.get("start_time"))
                rate = st_r or fps
                a_v, a_r = _rt(avail.get("start_time"))
                media_start = (a_v / a_r) if a_r else 0.0
                src0 = st_v / rate - media_start
                mfps = a_r or _rt(avail.get("duration"))[1] or rate
                s = start + int(round(pos * fps))
                e = start + int(round((pos + dur) * fps))
                out.append({
                    "t": ttype, "i": tindex, "s": s, "e": e,
                    "name": child.get("name") or "",
                    "path": _url_to_path(ref.get("target_url") or ""),
                    "src0": round(src0, 6), "src1": round(src0 + dur, 6),
                    "mfps": mfps, "sf0": int(round(src0 * mfps)),
                    "enabled": child.get("enabled", True),
                })
            pos += dur
        tracks["%s%d" % (ttype, tindex)] = out
    return {"tl": data.get("name") or "", "fps": fps, "start": start, "tracks": tracks}


def snapshot_from_compact(c):
    fps = float(c.get("fps") or 24)
    media = c.get("media") or []
    out = []
    for row in c.get("items") or []:
        s, e, sf, ef, m = row[0], row[1], row[2], row[3], row[4]
        md = media[m - 1] if m and m <= len(media) else {}
        mfps = float(md.get("fps") or fps)
        src0 = sf / mfps
        out.append({"t": c.get("t", "video"), "i": c.get("i", 1), "s": int(s), "e": int(e),
                    "name": row[5] if len(row) > 5 else "", "path": md.get("path") or "",
                    "src0": src0, "src1": src0 + (e - s) / fps, "mfps": mfps, "sf0": int(sf), "enabled": True})
    return {"tl": c.get("tl") or "", "fps": fps, "start": int(c.get("start") or 0),
            "tracks": {"%s%d" % (c.get("t", "video"), c.get("i", 1)): out}}


def fingerprint(snap):
    if not snap:
        return ""
    parts = [snap.get("tlid") or snap.get("tl") or ""]
    for key in sorted(snap.get("tracks", {})):
        parts.append(key + ":" + ",".join("%d-%d-%d" % (it["s"], it["e"], it["sf0"]) for it in snap["tracks"][key]))
    return "|".join(parts)


class LinkError(Exception):
    pass


class _Base:
    async_events = True  # deliver events on a separate thread (never block the receiver)

    def __init__(self):
        self.snapshot = None
        self.info = {}
        self.playhead = None  # absolute timeline frame
        self.listeners = []  # fn(kind, data)
        self._lock = threading.RLock()
        self._events = None

    def on(self, fn):
        self.listeners.append(fn)

    def _deliver(self, kind, data):
        for fn in list(self.listeners):
            try:
                fn(kind, data)
            except Exception:  # listener errors must not kill the link
                import traceback
                traceback.print_exc()

    def _fire(self, kind, data):
        if not self.async_events:
            self._deliver(kind, data)
            return
        if self._events is None:
            import queue
            self._events = queue.Queue()

            def pump():
                while True:
                    k, d = self._events.get()
                    self._deliver(k, d)
            threading.Thread(target=pump, daemon=True, name="dd-events").start()
        self._events.put((kind, data))

    def _set_snapshot(self, snap, reason=""):
        changed = fingerprint(snap) != fingerprint(self.snapshot)
        self.snapshot = snap
        if changed or reason == "force":
            self._fire("snapshot", snap)


# ------------------------------------------------------------------ file bridge


def default_bridge_dir():
    """%TEMP%/DaVinciDirector/bridge — Resolve's script sandbox may write there
    (writing into %APPDATA% is refused, verified on Resolve 21.2)."""
    import tempfile
    base = os.environ.get("TEMP") or os.environ.get("TMP") or tempfile.gettempdir()
    return os.path.join(base, "DaVinciDirector", "bridge")


class FileBridgeLink(_Base):
    CHUNK = 150  # hex chars per command folder name

    def __init__(self, base_dir=None, poll=0.05):
        super().__init__()
        self.base = base_dir or default_bridge_dir()
        self.dir_in = os.path.join(self.base, "in")
        self.dir_out = os.path.join(self.base, "out")
        self.dir_state = os.path.join(self.base, "state")
        self.poll = poll
        self._seq = int(time.time() * 10) % 100000000
        self._parts = {}  # (sid, seq) -> {kind, n, parts}
        self._done = {}  # cmd id -> result
        self._cv = threading.Condition()
        self._stop = False
        self.last_beat = 0.0
        self.sid = None
        self.bridge_up = False  # the script in Resolve said hello and has not said goodbye
        self.ui_open = False
        self._pending = 0
        self._thread = None
        self.errors = []

    # lifecycle
    def start(self):
        for d in (self.dir_in, self.dir_out, self.dir_state):
            os.makedirs(d, exist_ok=True)
        # stale commands from a previous run must not execute
        for name in os.listdir(self.dir_in):
            shutil.rmtree(os.path.join(self.dir_in, name), ignore_errors=True)
        self._thread = threading.Thread(target=self._loop, daemon=True, name="dd-bridge")
        self._thread.start()
        return self

    def stop(self):
        self._stop = True
        self.set_ui(False)

    def set_preview(self, on):
        """<in>/preview: the script reports the playhead ~30x per second while a part plays."""
        path = os.path.join(self.dir_in, "preview")
        if on:
            os.makedirs(path, exist_ok=True)
        else:
            shutil.rmtree(path, ignore_errors=True)

    def set_ui(self, open_):
        """<in>/ui-open tells the script to watch playhead and timeline; without it the
        script makes no Resolve calls at all (no busy cursor while the window is closed)."""
        self.ui_open = bool(open_)
        path = os.path.join(self.dir_in, "ui-open")
        if open_:
            os.makedirs(path, exist_ok=True)
        else:
            shutil.rmtree(path, ignore_errors=True)

    @property
    def connected(self):
        if not self.bridge_up:
            return False
        # while the window is open the script reports every 5 s; silence = Resolve closed
        return not self.ui_open or time.time() - self.last_beat < 15.0

    # commands
    @staticmethod
    def lua_literal(v):
        if v is None:
            return "nil"
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, int):
            return str(v)
        if isinstance(v, float):
            return repr(round(v, 6)) if v == v else "0"
        if isinstance(v, str):
            # raw UTF-8 is fine inside a Lua string; escape quotes, backslashes, controls
            return '"' + "".join(c if c not in '"\\' and ord(c) >= 32 else "\\%d" % ord(c) for c in v) + '"'
        if isinstance(v, (list, tuple)):
            return "{" + ",".join(FileBridgeLink.lua_literal(x) for x in v) + "}"
        if isinstance(v, dict):
            return "{" + ",".join("%s=%s" % (k, FileBridgeLink.lua_literal(x)) for k, x in v.items()) + "}"
        raise TypeError("cannot encode %r" % (v,))

    def run(self, ops, timeout=60.0):
        if not self.connected:
            raise LinkError("Міст із Resolve не запущено: Workspace ▸ Scripts ▸ DaVinci Director")
        with self._cv:
            self._seq += 1
            seq = self._seq
        payload = self.lua_literal(list(ops)).encode("utf-8").hex()
        chunks = [payload[i:i + self.CHUNK] for i in range(0, len(payload), self.CHUNK)] or [""]
        made = []
        for k, ch in enumerate(chunks, 1):
            d = os.path.join(self.dir_in, "%d-%dof%d-%s" % (seq, k, len(chunks), ch))
            os.makedirs(d, exist_ok=True)
            made.append(d)
        ring = os.path.join(self.dir_in, "ring")
        with self._cv:
            self._pending += 1
        os.makedirs(ring, exist_ok=True)  # doorbell: the script lists commands only when it rings
        deadline = time.time() + timeout
        answered = time.time() + min(timeout, 8.0)
        try:
            with self._cv:
                while seq not in self._done:
                    now = time.time()
                    if now >= deadline or (now >= answered and now - self.last_beat > 8.0):
                        self.bridge_up = False  # nobody answers: the script is not running
                        self._fire("link", {"connected": False})
                        raise LinkError("Resolve не відповідає. Запустіть Workspace > Scripts > DaVinci Director.")
                    self._cv.wait(0.25)
                res = self._done.pop(seq)
        finally:
            for d in made:
                shutil.rmtree(d, ignore_errors=True)
            with self._cv:
                self._pending -= 1
                if self._pending <= 0:
                    self._pending = 0
                    shutil.rmtree(ring, ignore_errors=True)
        return res

    # receiving
    def _loop(self):
        while not self._stop:
            try:
                self._scan()
            except Exception as e:  # keep polling whatever happens
                self.errors.append(repr(e))
                del self.errors[:-20]
            time.sleep(self.poll if self.connected else max(self.poll, 0.3))

    def _scan(self):
        if not os.path.isdir(self.dir_out):  # %TEMP% cleanup may remove the folders
            for d in (self.dir_in, self.dir_out, self.dir_state):
                os.makedirs(d, exist_ok=True)
            if self.ui_open:
                self.set_ui(True)
        try:
            names = sorted(os.listdir(self.dir_out))
        except OSError:
            return
        for name in names:
            path = os.path.join(self.dir_out, name)
            # Resolve may replace the extension (ExportMetadata writes .csv, Export .edl)
            stem, dot, ext = name.rpartition(".")
            if not dot or ext.lower() not in ("m", "csv", "edl"):
                continue
            bits = stem.split("-", 4)
            if len(bits) != 5:
                _rm(path)
                continue
            sid, seq, kind, part, h = bits
            try:
                k, n = (int(x) for x in part.split("of"))
            except ValueError:
                _rm(path)
                continue
            key = (sid, int(seq))
            entry = self._parts.setdefault(key, {"kind": kind, "n": n, "parts": {}})
            entry["parts"][k] = h
            _rm(path)
            if len(entry["parts"]) == n:
                del self._parts[key]
                try:
                    data = json.loads(bytes.fromhex("".join(entry["parts"][i] for i in range(1, n + 1))).decode("utf-8", "replace"))
                except ValueError:
                    continue
                self._handle(sid, kind, data)

    def _handle(self, sid, kind, data):
        self.last_beat = time.time()
        if sid != self.sid or (not self.bridge_up and kind != "x"):
            self.sid = sid
            self.bridge_up = True
            self._fire("link", {"connected": True, "new": True})
            if self.snapshot is None and kind != "s":
                # the app (re)started while the bridge was already running: ask for the state
                threading.Thread(target=self._request_state, daemon=True).start()
        if kind == "s":
            self.info["timeline_header"] = data
            snap = self._read_state(data)
            if snap is not None:
                self._set_snapshot(snap)
            elif data.get("none"):
                self._set_snapshot(None)
            if data.get("tc"):
                self._set_playhead(data["tc"])
        elif kind == "c":
            self._set_snapshot(snapshot_from_compact(data))
        elif kind == "r":
            with self._cv:
                self._done[int(data.get("id", -1))] = data
                self._cv.notify_all()
        elif kind == "p":
            self._set_playhead(data.get("tc"))
        elif kind == "i":
            self.info.update(data)
            self._fire("info", data)
        elif kind == "t":
            self.info["timelines"] = data
            self._fire("timelines", data)
        elif kind == "e":
            self.errors.append(data.get("err"))
            self._fire("bridge_error", data)
        elif kind == "x":
            self.last_beat = 0
            self.bridge_up = False
            self._fire("link", {"connected": False})

    def request_state(self):
        threading.Thread(target=self._request_state, daemon=True).start()

    def _request_state(self):
        if not self.connected:
            return
        try:
            self.run([{"op": "state"}, {"op": "timelines"}], timeout=20)
        except LinkError:
            pass

    def _set_playhead(self, tc):
        snap = self.snapshot
        fps = (snap or {}).get("fps") or 24
        f = tc_to_frame(tc, fps)
        if f is not None and f != self.playhead:
            self.playhead = f
            self._fire("playhead", f)

    def _read_state(self, header):
        name = header.get("file")
        if not name:
            if header.get("tl"):
                self.errors.append("Timeline:Export failed — using compact state")
            return None
        path = os.path.join(self.dir_state, name)
        text = None
        for _ in range(40):  # Resolve may still be writing
            try:
                with open(path, encoding="utf-8") as f:
                    text = f.read()
                json.loads(text)
                break
            except (OSError, ValueError):
                text = None
                time.sleep(0.05)
        # remove this and older states only — a newer one may already be waiting
        for old in os.listdir(self.dir_state):
            if old <= name or not old.startswith(name.split("-")[0] + "-"):
                _rm(os.path.join(self.dir_state, old))
        if text is None:
            return None
        try:
            snap = parse_otio(text, header.get("fps"))
        except (ValueError, KeyError, TypeError) as e:
            self.errors.append("OTIO parse: %r" % e)
            return None
        snap["tlid"] = header.get("tlid")
        snap["tl"] = header.get("tl") or snap["tl"]
        if header.get("start") is not None:
            snap["start"] = int(header["start"])
        apply_media_rates(snap, header.get("media") or [])
        snap["check"] = _check_summary(snap, header.get("tracks") or [])
        return snap


def apply_media_rates(snap, media):
    """Use the clip FPS reported by Resolve (OTIO carries source times in the
    timeline rate; AppendToTimeline wants frames of the media's own rate)."""
    rates = {}
    for m in media:
        try:
            rates[norm_path(m.get("p"))] = float(m.get("f"))
        except (TypeError, ValueError):
            pass
    for items in snap["tracks"].values():
        for it in items:
            fps = rates.get(norm_path(it["path"]))
            if fps:
                it["mfps"] = fps
                it["sf0"] = int(round(it["src0"] * fps))


def _check_summary(snap, tracks):
    """Compare the OTIO-derived positions with what the bridge measured."""
    problems = []
    for t in tracks:
        items = snap["tracks"].get("%s%d" % (t.get("t"), t.get("i")), [])
        if len(items) != t.get("n"):
            problems.append("%s%d: %d≠%d" % (t.get("t"), t.get("i"), len(items), t.get("n")))
        elif items and (items[0]["s"] != t.get("first") or items[-1]["e"] != t.get("last")):
            problems.append("%s%d: bounds %d-%d≠%s-%s" % (t.get("t"), t.get("i"), items[0]["s"], items[-1]["e"],
                                                          t.get("first"), t.get("last")))
    return problems


def _rm(path):
    try:
        os.remove(path)
    except OSError:
        pass


# ------------------------------------------------------------------ direct API


class DirectLink(_Base):
    """Executes the same operations against a Resolve scripting object."""

    async_events = False

    def __init__(self, resolve):
        super().__init__()
        self.resolve = resolve
        self.connected = resolve is not None
        self._last_fp = None
        self._media = {}

    def start(self):
        self.refresh("force")
        return self

    def stop(self):
        pass

    ui_open = False

    def set_ui(self, open_):
        self.ui_open = bool(open_)

    def set_preview(self, on):
        pass

    def request_state(self):
        self.refresh("force")

    # helpers
    def _project(self):
        return self.resolve.GetProjectManager().GetCurrentProject()

    def _timeline(self):
        p = self._project()
        return p.GetCurrentTimeline() if p else None

    @staticmethod
    def _items(tl, tt, ti):
        return list(tl.GetItemListInTrack(tt, ti) or [])

    def _find(self, tl, ref):
        for it in self._items(tl, ref.get("t", "video"), ref.get("i", 1)):
            if int(round(it.GetStart())) == ref["s"]:
                return it
        raise LinkError("timeline item not found: %r" % (ref,))

    def _media_for(self, tl, spec):
        if spec.get("via"):
            try:
                mpi = self._find(tl, spec["via"]).GetMediaPoolItem()
                if mpi:
                    return mpi
            except LinkError:
                pass
        want = norm_path(spec.get("path"))
        if want in self._media:
            return self._media[want]
        for tt in ("video", "audio"):
            for ti in range(1, tl.GetTrackCount(tt) + 1):
                for it in self._items(tl, tt, ti):
                    mpi = it.GetMediaPoolItem()
                    if mpi and norm_path(mpi.GetClipProperty("File Path")) == want:
                        self._media[want] = mpi
                        return mpi

        def scan(folder):
            for c in folder.GetClipList() or []:
                if norm_path(c.GetClipProperty("File Path")) == want:
                    return c
            for sub in folder.GetSubFolderList() or []:
                found = scan(sub)
                if found:
                    return found
            return None

        found = scan(self._project().GetMediaPool().GetRootFolder())
        if not found:
            raise LinkError("media not in media pool: %s" % spec.get("path"))
        self._media[want] = found
        return found

    def read_snapshot(self):
        tl = self._timeline()
        if not tl:
            return None
        fps = float(tl.GetSetting("timelineFrameRate") or 24)
        tracks = {}
        for tt in ("video", "audio"):
            for ti in range(1, tl.GetTrackCount(tt) + 1):
                out = []
                for it in self._items(tl, tt, ti):
                    mpi = it.GetMediaPoolItem()
                    path, mfps = "", fps
                    if mpi:
                        path = mpi.GetClipProperty("File Path") or ""
                        try:
                            mfps = float(mpi.GetClipProperty("FPS") or fps)
                        except (TypeError, ValueError):
                            mfps = fps
                    s, e = int(round(it.GetStart())), int(round(it.GetEnd()))
                    sf0 = int(it.GetSourceStartFrame())
                    src0 = sf0 / mfps
                    out.append({"t": tt, "i": ti, "s": s, "e": e, "name": it.GetName(), "path": path,
                                "src0": src0, "src1": src0 + (e - s) / fps, "mfps": mfps, "sf0": sf0, "enabled": True})
                tracks["%s%d" % (tt, ti)] = out
        return {"tl": tl.GetName(), "tlid": tl.GetUniqueId(), "fps": fps, "start": int(tl.GetStartFrame()),
                "tracks": tracks, "check": []}

    def refresh(self, reason=""):
        with self._lock:
            snap = self.read_snapshot()
            self._set_snapshot(snap, reason)
            tl = self._timeline()
            if tl and snap:
                f = tc_to_frame(tl.GetCurrentTimecode(), snap["fps"])
                if f != self.playhead:
                    self.playhead = f
                    self._fire("playhead", f)
            p = self._project()
            info = {"product": self.resolve.GetProductName(), "version": self.resolve.GetVersionString(),
                    "project": p.GetName() if p else None, "tl": tl.GetName() if tl else None, "v": "direct"}
            if info != {k: self.info.get(k) for k in info}:
                self.info.update(info)
                self._fire("info", info)
        return snap

    def run(self, ops, timeout=60.0):
        with self._lock:
            done, err, results = 0, None, []
            for k, op in enumerate(ops, 1):
                tl = self._timeline()
                try:
                    if not tl and op["op"] not in ("set_timeline", "quit"):
                        raise LinkError("no current timeline")
                    res = getattr(self, "_op_" + op["op"])(tl, op)
                except (LinkError, AttributeError, KeyError, TypeError) as e:
                    err = str(e)
                    break
                results.append(bool(res))
                if res is False and not op.get("soft"):
                    err = op["op"] + " failed"
                    break
                done = k
            self.refresh()
            return {"ok": err is None, "done": done, "n": len(ops), "err": err, "res": results}

    # ops
    def _op_trim_end(self, tl, op):
        it = self._find(tl, op["ref"])
        return it.SetRightOffset(it.GetRightOffset() + op["n"])

    def _op_trim_start(self, tl, op):
        it = self._find(tl, op["ref"])
        return it.SetLeftOffset(it.GetLeftOffset() + op["n"])

    def _op_append(self, tl, op):
        infos = []
        for c in op["clips"]:
            info = {"mediaPoolItem": self._media_for(tl, c), "startFrame": c["s"], "endFrame": c["e"]}
            for src, dst in (("rec", "recordFrame"), ("track", "trackIndex"), ("mt", "mediaType")):
                if c.get(src) is not None:
                    info[dst] = c[src]
            infos.append(info)
        res = self._project().GetMediaPool().AppendToTimeline(infos)
        return bool(res)

    def _op_delete(self, tl, op):
        items = [self._find(tl, r) for r in op["refs"]]
        if not items:
            return True
        seen, out = set(), []
        for it in items:
            for x in [it] + list(it.GetLinkedItems() or []):
                if x.GetUniqueId() not in seen:
                    seen.add(x.GetUniqueId())
                    out.append(x)
        return tl.DeleteClips(out, False)

    def _op_move(self, tl, op):
        items = [self._find(tl, r) for r in op["refs"]]
        if not items:
            return True
        return tl.MoveItems(items, {"startFrame": op["to"]}, True, False)

    def _op_go(self, tl, op):
        return tl.SetCurrentTimecode(op["tc"])

    def _op_page(self, tl, op):
        return self.resolve.OpenPage(op["name"])

    def _op_new_timeline(self, tl, op):
        p = self._project()
        ntl = p.GetMediaPool().CreateEmptyTimeline(op["name"])
        return bool(ntl) and p.SetCurrentTimeline(ntl)

    def _op_duplicate_timeline(self, tl, op):
        ntl = tl.DuplicateTimeline(op["name"])
        return bool(ntl) and self._project().SetCurrentTimeline(ntl)

    def _op_set_timeline(self, tl, op):
        p = self._project()
        for i in range(1, p.GetTimelineCount() + 1):
            t = p.GetTimelineByIndex(i)
            if t and (t.GetUniqueId() == op.get("id") or (op.get("id") is None and t.GetName() == op.get("name"))):
                return p.SetCurrentTimeline(t)
        return False

    def _op_timelines(self, tl, op):
        p = self._project()
        out = []
        for i in range(1, p.GetTimelineCount() + 1):
            t = p.GetTimelineByIndex(i)
            out.append({"id": t.GetUniqueId(), "name": t.GetName()})
        self.info["timelines"] = out
        self._fire("timelines", out)
        return True

    def _op_marker(self, tl, op):
        return tl.AddMarker(op["f"], op.get("color", "Blue"), op.get("name", ""), op.get("note", ""),
                            op.get("dur", 1), op.get("data", ""))

    def _op_delete_markers(self, tl, op):
        if op.get("data"):
            return tl.DeleteMarkerByCustomData(op["data"])
        return tl.DeleteMarkersByColor(op.get("color", "All"))

    def _op_state(self, tl, op):
        return True

    def _op_quit(self, tl, op):
        return True


class PollingDirectLink(DirectLink):
    """DirectLink that also watches the timeline (edits made in Resolve)."""

    async_events = True

    def __init__(self, resolve, interval=0.4):
        super().__init__(resolve)
        self.interval = interval
        self._stop = False

    def start(self):
        super().start()
        threading.Thread(target=self._watch, daemon=True, name="dd-watch").start()
        return self

    def stop(self):
        self._stop = True

    def _watch(self):
        while not self._stop:
            try:
                self.refresh()
            except Exception:
                pass
            time.sleep(self.interval)


def connect_external():
    """Resolve Studio external scripting (Preferences > General > External
    scripting = Local).  Returns a resolve object or None."""
    try:
        import DaVinciResolveScript as dvr  # noqa: N813
    except ImportError:
        mod = os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
                           r"Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting\Modules")
        import sys
        if os.path.isdir(mod) and mod not in sys.path:
            sys.path.append(mod)
        try:
            import DaVinciResolveScript as dvr  # noqa: N813
        except ImportError:
            return None
    try:
        return dvr.scriptapp("Resolve")
    except Exception:
        return None
