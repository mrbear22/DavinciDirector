"""Connection to DaVinci Resolve.

``FileBridgeLink`` talks to ``DaVinci Director.lua`` running inside Resolve
(Workspace > Scripts; works with the free version):

    link.run(ops) -> {"ok", "done", "err", ...}   execute timeline operations
    link.snapshot                                   last known timeline state
    link.connected                                  script alive?

Commands are folder names in ``<bridge>/in``, answers are file names in
``<bridge>/out``, the timeline state is an OTIO export in ``<bridge>/state``.

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
import queue
import threading
import time
import traceback
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


def _otio_clip(child, ttype, tindex, s, e, dur, fps):
    """One OTIO clip -> item; source times are in seconds from the start of the media file."""
    ref = _media_ref(child)
    avail = ref.get("available_range") or {}
    st_v, st_r = _rt((child.get("source_range") or {}).get("start_time"))
    rate = st_r or fps
    a_v, a_r = _rt(avail.get("start_time"))
    src0 = st_v / rate - ((a_v / a_r) if a_r else 0.0)
    mfps = a_r or _rt(avail.get("duration"))[1] or rate
    return {"t": ttype, "i": tindex, "s": s, "e": e, "name": child.get("name") or "",
            "path": _url_to_path(ref.get("target_url") or ""), "src0": round(src0, 6), "src1": round(src0 + dur, 6),
            "mfps": mfps, "sf0": int(round(src0 * mfps)), "enabled": child.get("enabled", True)}


def parse_otio(text, fps_hint=None):
    """Resolve OTIO export -> {"tl", "fps", "start", "tracks": {"video1": [item, ...]}}."""
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
            dur = _secs((child.get("source_range") or {}).get("duration"), fps)
            if schema.startswith("Clip"):
                out.append(_otio_clip(child, ttype, tindex, start + int(round(pos * fps)),
                                      start + int(round((pos + dur) * fps)), dur, fps))
            pos += dur
        tracks["%s%d" % (ttype, tindex)] = out
    return {"tl": data.get("name") or "", "fps": fps, "start": start, "tracks": tracks}


def snapshot_from_compact(c):
    fps = float(c.get("fps") or 24)
    media = c.get("media") or []
    out = []
    for row in c.get("items") or []:
        s, e, sf, m = row[0], row[1], row[2], row[4]  # row[3]: source end frame
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


SILENCE = 8.0  # s without any message from the script: it is not running
BEAT_TIMEOUT = 15.0  # s: while the window is open the script reports every 5 s


class LinkError(Exception):
    pass


# ------------------------------------------------------------------ file bridge


def default_bridge_dir():
    """%TEMP%/DaVinciDirector/bridge — Resolve's script sandbox may write there
    (writing into %APPDATA% is refused, verified on Resolve 21.2)."""
    import tempfile
    base = os.environ.get("TEMP") or os.environ.get("TMP") or tempfile.gettempdir()
    return os.path.join(base, "DaVinciDirector", "bridge")


class FileBridgeLink:
    CHUNK = 150  # hex chars per command folder name

    def __init__(self, base_dir=None, poll=0.05):
        self.snapshot = None
        self.info = {}
        self.playhead = None  # absolute timeline frame
        self.listeners = []  # fn(kind, data)
        self._events = queue.Queue()
        threading.Thread(target=self._pump, daemon=True, name="dd-events").start()
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
        self.errors = []

    # events: delivered on their own thread, so a listener never blocks the bridge
    def on(self, fn):
        self.listeners.append(fn)

    def _fire(self, kind, data):
        self._events.put((kind, data))

    def _pump(self):
        while True:
            kind, data = self._events.get()
            for fn in list(self.listeners):
                try:
                    fn(kind, data)
                except Exception:  # a failing listener must not stop the others
                    traceback.print_exc()

    def _set_snapshot(self, snap):
        changed = fingerprint(snap) != fingerprint(self.snapshot)
        self.snapshot = snap
        if changed:
            self._fire("snapshot", snap)

    # lifecycle
    def start(self):
        for d in (self.dir_in, self.dir_out, self.dir_state):
            os.makedirs(d, exist_ok=True)
        # stale commands from a previous run must not execute
        for name in os.listdir(self.dir_in):
            shutil.rmtree(os.path.join(self.dir_in, name), ignore_errors=True)
        threading.Thread(target=self._loop, daemon=True, name="dd-bridge").start()
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
        return not self.ui_open or time.time() - self.last_beat < BEAT_TIMEOUT

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
            self._pending += 1
        made = []
        try:
            self._post(seq, ops, made)
            return self._wait(seq, timeout)
        finally:
            for d in made:
                shutil.rmtree(d, ignore_errors=True)
            with self._cv:
                self._pending = max(0, self._pending - 1)
                if not self._pending:
                    shutil.rmtree(os.path.join(self.dir_in, "ring"), ignore_errors=True)

    def _post(self, seq, ops, made):
        """A command as folder names (the script can list folders, not read files), then the doorbell.
        Every folder made goes into ``made`` (removed once answered)."""
        payload = self.lua_literal(list(ops)).encode("utf-8").hex()
        chunks = [payload[i:i + self.CHUNK] for i in range(0, len(payload), self.CHUNK)] or [""]
        for k, ch in enumerate(chunks, 1):
            d = os.path.join(self.dir_in, "%d-%dof%d-%s" % (seq, k, len(chunks), ch))
            os.makedirs(d, exist_ok=True)
            made.append(d)
        os.makedirs(os.path.join(self.dir_in, "ring"), exist_ok=True)  # the script lists commands only when it rings

    def _wait(self, seq, timeout):
        deadline = time.time() + timeout
        answered = time.time() + min(timeout, SILENCE)
        with self._cv:
            while seq not in self._done:
                now = time.time()
                if now >= deadline or (now >= answered and now - self.last_beat > SILENCE):
                    self.bridge_up = False  # nobody answers: the script is not running
                    self._fire("link", {"connected": False})
                    raise LinkError("Resolve не відповідає. Запустіть Workspace > Scripts > DaVinci Director.")
                self._cv.wait(0.25)
            return self._done.pop(seq)

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
            parsed = _parse_message_name(name)
            if parsed is None:
                continue
            _rm(os.path.join(self.dir_out, name))
            if parsed is False:
                continue  # not one of ours
            message = self._collect(*parsed)
            if message is not None:
                self._handle(parsed[0], parsed[2], message)

    def _collect(self, sid, seq, kind, k, n, part):
        """Puts the chunks of one message together; returns its data once all of them arrived."""
        entry = self._parts.setdefault((sid, seq), {"n": n, "parts": {}})
        entry["parts"][k] = part
        if len(entry["parts"]) < n:
            return None
        del self._parts[(sid, seq)]
        try:
            return json.loads(bytes.fromhex("".join(entry["parts"][i] for i in range(1, n + 1))).decode("utf-8", "replace"))
        except ValueError:
            return None

    def _handle(self, sid, kind, data):
        self.last_beat = time.time()
        if sid != self.sid or (not self.bridge_up and kind != "x"):
            self._hello(sid, kind)
        handler = {"s": self._on_state, "c": self._on_compact, "r": self._on_reply, "p": self._on_playhead,
                   "i": self._on_info, "t": self._on_timelines, "e": self._on_error, "x": self._on_goodbye}.get(kind)
        if handler:
            handler(data)

    def _hello(self, sid, kind):
        """A (new) script run in Resolve."""
        self.sid = sid
        self.bridge_up = True
        self._fire("link", {"connected": True, "new": True})
        if self.snapshot is None and kind != "s":
            # the app (re)started while the bridge was already running: ask for the state
            self.request_state()

    def _on_state(self, data):
        self.info["timeline_header"] = data
        snap = self._read_state(data)
        if snap is not None or data.get("none"):
            self._set_snapshot(snap)
        if data.get("tc"):
            self._set_playhead(data["tc"])

    def _on_compact(self, data):
        self._set_snapshot(snapshot_from_compact(data))

    def _on_reply(self, data):
        with self._cv:
            self._done[int(data.get("id", -1))] = data
            self._cv.notify_all()

    def _on_playhead(self, data):
        self._set_playhead(data.get("tc"))

    def _on_info(self, data):
        self.info.update(data)
        self._fire("info", data)

    def _on_timelines(self, data):
        self.info["timelines"] = data
        self._fire("timelines", data)

    def _on_error(self, data):
        self.errors.append(data.get("err"))
        self._fire("bridge_error", data)

    def _on_goodbye(self, data):
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
            pass  # the script stopped meanwhile: "link" already told everyone

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


def _parse_message_name(name):
    """"<sid>-<seq>-<kind>-<k>of<n>-<hex>.<ext>" -> (sid, seq, kind, k, n, hex); None for files that are
    not messages (left alone), False for broken ones (removed).  Resolve may replace the extension:
    ExportMetadata writes .csv, Timeline:Export .edl."""
    stem, dot, ext = name.rpartition(".")
    if not dot or ext.lower() not in ("m", "csv", "edl"):
        return None
    bits = stem.split("-", 4)
    if len(bits) != 5:
        return False
    sid, seq, kind, part, h = bits
    try:
        k, n = (int(x) for x in part.split("of"))
        return sid, int(seq), kind, k, n, h
    except ValueError:
        return False


def _rm(path):
    try:
        os.remove(path)
    except OSError:
        pass
