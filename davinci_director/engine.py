"""Two-way sync between the transcript and the real Resolve timeline.

Design (the single source of truth is the Resolve timeline):

* A transcript belongs to a *source media file*: every word has a time in
  seconds from the start of that file.  Fragments (utterances) get stable IDs
  such as ``A0042`` (media tag + number) that never change, whatever happens
  on the timeline.
* The text the user sees is *derived* from the current timeline: for every
  clip of the edit track (in timeline order) we show the words whose source
  time lies inside the clip's source range.  Deleting, moving, trimming or
  adding clips in Resolve therefore changes the text automatically; words that
  are not used by any clip are shown as cut (strike-through) next to their
  source neighbour and can be restored.
* Text edits are translated to timeline operations (split = trim + append the
  remainder, ripple delete, ripple insert, block move) executed by the bridge
  inside Resolve; afterwards the new timeline state comes back and the text is
  rebuilt from it — text and timeline cannot drift apart.
"""

import copy
import json
import os
import re
import threading
import traceback

from .link import LinkError, fingerprint, frame_to_tc, norm_path
from .planner import Planner
from .storage import atomic_write, base_dir
from .transcript import Transcript, media_key, respell

SPEAKER_COLORS = ["#d8a657", "#7daea3", "#d3869b", "#a9b665", "#e78a4e", "#89b482", "#ea6962", "#b0a3d4"]


class EditError(Exception):
    pass


# ------------------------------------------------------------------ view


def content_sig(snap, track):
    """What the edit track plays: contiguous pieces of the same source merged
    (cuts that do not change the picture are ignored).  Times in seconds."""
    if not snap:
        return []
    fps = snap["fps"]
    out = []
    for it in snap["tracks"].get(track, []):
        src0, src1 = it["src0"], it["src0"] + (it["e"] - it["s"]) / fps
        if out and out[-1][0] == norm_path(it["path"]) and abs(out[-1][4] - it["s"] / fps) < 0.2                 and abs(out[-1][2] - src0) < 0.2:
            out[-1][2], out[-1][4] = src1, it["e"] / fps
        else:
            out.append([norm_path(it["path"]), src0, src1, it["s"] / fps, it["e"] / fps])
    return out


def same_content(a, b, tol=0.25):
    """Equal up to frame-rate rounding (a few frames)."""
    if len(a) != len(b):
        return False
    return all(x[0] == y[0] and all(abs(p - q) <= tol for p, q in zip(x[1:], y[1:])) for x, y in zip(a, b))


def _unused_runs(blocks, n_words):
    """Word ranges [a, z) of a file that no clip uses."""
    runs, pos = [], 0
    for a, z in sorted((b["i0"], b["i1"]) for b in blocks if b["i1"] > b["i0"]):
        if a > pos:
            runs.append((pos, a))
        pos = max(pos, z)
    if pos < n_words:
        runs.append((pos, n_words))
    return runs


def _place_cut_words(blocks, n_words):
    """Unused words are shown as cut next to their source neighbour: after the clip that ends where
    they start, else before the clip that starts where they end."""
    for a, z in _unused_runs(blocks, n_words):
        pred = next((b for b in blocks if b["i1"] == a and b["i1"] > b["i0"]), None)
        if pred is None:
            pred = next((b for b in blocks if b["i1"] <= a and b["i1"] > b["i0"] and
                         not any(c["i0"] < a and c["i1"] > b["i1"] for c in blocks)), None)
        if pred is not None:
            pred["cut"].append({"side": "after", "i0": a, "i1": z})
            continue
        succ = next((b for b in blocks if b["i0"] == z), None) or min(blocks, key=lambda b: abs(b["i0"] - z))
        succ["cut"].append({"side": "before", "i0": a, "i1": z})


def build_view(snap, track, transcripts, tags):
    """Blocks of the edit track in timeline order with their word ranges."""
    if not snap:
        return {"blocks": [], "fps": 25.0, "start": 0, "fp": ""}
    fps = snap["fps"]
    items = snap["tracks"].get(track, [])
    blocks = []
    by_media = {}
    for k, it in enumerate(items):
        key = media_key(it["path"]) if it.get("path") else None
        tr = transcripts.get(key) if key else None
        b = {"k": k, "s": it["s"], "e": it["e"], "src0": it["src0"], "src1": it["src1"], "name": it["name"],
             "m": key if tr else None, "tag": tags.get(key) if key else None, "path": it.get("path"),
             "ref": {"t": it["t"], "i": it["i"], "s": it["s"]}, "i0": 0, "i1": 0, "cut": []}
        if tr:
            b["i0"], b["i1"] = tr.range_for(it["src0"], it["src1"])
            b["covered"] = any(c[0] <= it["src0"] + 0.5 and c[1] >= it["src1"] - 0.5 for c in tr.covered)
            by_media.setdefault(key, []).append(b)
        blocks.append(b)
    for key, bl in by_media.items():
        _place_cut_words(bl, len(transcripts[key].words))
    return {"blocks": blocks, "fps": fps, "start": snap["start"], "fp": fingerprint(snap), "tl": snap.get("tl"),
            "track": track, "check": snap.get("check") or []}


# ------------------------------------------------------------------ engine


class Engine:
    def __init__(self, link, state_dir=None):
        self.link = link
        self.transcripts = {}
        self.tags = {}
        self.track = "video1"
        self.view = build_view(None, self.track, {}, {})
        self.version = 0
        self.tversion = 0  # bumps when transcript text/speakers change
        self.undo_stack, self.redo_stack = [], []
        self.clipboard = []
        self.listeners = []
        self._lock = threading.RLock()
        self.project_state = {"stories": [], "tags": {}}
        self.state_dir = state_dir or os.path.join(base_dir(), "projects")
        os.makedirs(self.state_dir, exist_ok=True)
        self.project_name = None
        link.on(self._on_link)

    # events
    def on(self, fn):
        self.listeners.append(fn)

    def _fire(self, kind, data=None):
        for fn in list(self.listeners):
            try:
                fn(kind, data)
            except Exception:  # a failing listener must not stop the others
                traceback.print_exc()

    def _on_link(self, kind, data):
        if kind == "snapshot":
            self.rebuild()
        elif kind == "info":
            name = (self.link.info or {}).get("project")
            if name and name != self.project_name:
                self.load_project(name)
            self._fire("info", self.link.info)
        elif kind == "playhead":
            self._fire("playhead", data)
        elif kind == "link":
            self._fire("link", data)
        elif kind == "timelines":
            self._fire("timelines", data)

    # project state (tags + stories), keyed by Resolve project name
    def _state_path(self):
        safe = re.sub(r"[^\w\-]+", "_", self.project_name or "default")[:120]
        return os.path.join(self.state_dir, safe + ".json")

    def load_project(self, name):
        with self._lock:
            self.project_name = name
            self.project_state = {"stories": [], "tags": {}}
            p = self._state_path()
            if os.path.exists(p):
                try:
                    with open(p, encoding="utf-8") as f:
                        self.project_state.update(json.load(f))
                except (OSError, ValueError) as e:  # the stories of this project are lost, the program goes on
                    print("Project state not read (%s): %r" % (p, e))
            self.tags = dict(self.project_state.get("tags") or {})
            self.track = self.project_state.get("track") or self.track
            for key in list(self.transcripts):
                self.tag_for(key)
            self.rebuild(force=True)

    def save_project(self):
        with self._lock:
            self.project_state["tags"] = self.tags
            self.project_state["track"] = self.track
            atomic_write(self._state_path(), json.dumps(self.project_state, ensure_ascii=False, indent=1))

    def tag_for(self, key):
        """Letter of a media file in fragment IDs (A0001).  It is stored with the
        transcript, so IDs stay the same in every Resolve project; a clash with
        another media of the current project gets a new letter."""
        if key in self.tags:
            return self.tags[key]
        tr = self.transcripts.get(key)
        used = set(self.tags.values())
        tag = (tr.meta.get("tag") if tr else None)
        if not tag or tag in used:
            n = 0
            while True:
                n += 1
                tag, x = "", n
                while x:
                    x, r = divmod(x - 1, 26)
                    tag = chr(65 + r) + tag
                if tag not in used:
                    break
        self.tags[key] = tag
        if tr is not None and tr.meta.get("tag") != tag:
            tr.meta["tag"] = tag
            tr.save()
        if self.project_name:
            self.save_project()
        return tag

    # transcripts
    def media_in_track(self):
        snap = self.link.snapshot
        seen, out = set(), []
        for it in (snap or {}).get("tracks", {}).get(self.track, []):
            k = media_key(it["path"]) if it.get("path") else None
            if k and k not in seen:
                seen.add(k)
                ranges = [(x["src0"], x["src1"]) for x in snap["tracks"][self.track] if media_key(x["path"]) == k]
                out.append({"key": k, "path": it["path"], "name": it["name"], "mfps": it["mfps"], "ranges": ranges})
        return out

    def load_transcripts(self):
        for m in self.media_in_track():
            if m["key"] not in self.transcripts:
                tr = Transcript.load(m["path"])
                if tr:
                    self.transcripts[m["key"]] = tr
                    self.tag_for(m["key"])
                    self.tversion += 1

    def set_transcript(self, tr):
        with self._lock:
            tr._mids = None  # word times may have changed
            self.transcripts[tr.key] = tr
            tr.meta["tag"] = self.tag_for(tr.key)
            tr.save()
            self.tversion += 1
            self.rebuild(force=True)

    def rebuild(self, force=False):
        with self._lock:
            self.load_transcripts()
            view = build_view(self.link.snapshot, self.track, self.transcripts, self.tags)
            if force or view["fp"] != self.view.get("fp") or view["blocks"] != self.view.get("blocks"):
                self.version += 1
                view["v"] = self.version
                view["tv"] = self.tversion
                self.view = view
                self._fire("view", view)

    def set_track(self, track):
        self.track = track
        self.save_project()
        self.rebuild(force=True)

    def fid(self, key, k):
        return "%s%04d" % (self.tags.get(key) or self.tag_for(key), k + 1)

    def parse_fid(self, fid):
        m = re.match(r"^\s*([A-Za-z]+)0*(\d+)\s*$", str(fid))
        if not m:
            return None
        tag, n = m.group(1).upper(), int(m.group(2)) - 1
        key = next((k for k, t in self.tags.items() if t == tag), None)
        if key is None or key not in self.transcripts or not 0 <= n < len(self.transcripts[key].frags):
            return None
        return key, n

    # helpers for edits
    def _block(self, k, v=None):
        if v is not None and v != self.view.get("v"):
            raise EditError("Таймлайн змінився — дія скасована, текст оновлено.")
        blocks = self.view["blocks"]
        if not 0 <= k < len(blocks):
            raise EditError("Фрагмент не знайдено (таймлайн змінився).")
        return blocks[k]

    def _frame(self, b, t):
        f = b["s"] + int(round((t - b["src0"]) * self.view["fps"]))
        return min(max(f, b["s"]), b["e"])

    def frame_before(self, k, w):
        b = self._block(k)
        if not b["m"] or w <= b["i0"]:
            return b["s"]
        return self._frame(b, self.transcripts[b["m"]].gap_before(w))

    def frame_after(self, k, w):
        b = self._block(k)
        if not b["m"] or w >= b["i1"] - 1:
            return b["e"]
        return self._frame(b, self.transcripts[b["m"]].gap_after(w))

    def _pause_edge(self, k, w, start):
        """Pause token = the silence after word w of block k (w = i0-1: before the first word;
        w = i1-1: after the last one).  Returns its start or end frame."""
        b = self._block(k)
        words = self.transcripts[b["m"]].words if b["m"] else []
        if start:
            return b["s"] if (not words or w < b["i0"]) else self._frame(b, words[w][1] + 0.05)
        return b["e"] if (not words or w >= b["i1"] - 1) else self._frame(b, words[w + 1][0] - 0.05)

    def selection_frames(self, sel):
        """sel = {a: {b, w, p?}, z: {b, w, p?}} (inclusive tokens): a word occurrence, or with
        p a pause token (silence after word w); w = null → whole block."""
        a, z = sel["a"], sel["z"]

        def key(t):
            return (t["b"], -1 if t.get("w") is None else t["w"] + (0.5 if t.get("p") else 0))
        if key(a) > key(z):
            a, z = z, a
        if a.get("p"):
            f1 = self._pause_edge(a["b"], a["w"], True)
        else:
            f1 = self._block(a["b"])["s"] if a.get("w") is None else self.frame_before(a["b"], a["w"])
        if z.get("p"):
            f2 = self._pause_edge(z["b"], z["w"], False)
        else:
            f2 = self._block(z["b"])["e"] if z.get("w") is None else self.frame_after(z["b"], z["w"])
        if f2 <= f1:
            raise EditError("Порожнє виділення.")
        return f1, f2

    def insertion_frame(self, pos):
        """pos = {b, w} → before word w of block b; {b, end: true} → after the block; {end: true} → track end."""
        if pos.get("b") is None:
            blocks = self.view["blocks"]
            return blocks[-1]["e"] if blocks else self.view["start"]
        if pos.get("end"):
            return self._block(pos["b"])["e"]
        if pos.get("w") is None:
            return self._block(pos["b"])["s"]
        if pos.get("p"):  # before a pause token = right after the word that precedes it
            return self._pause_edge(pos["b"], pos["w"], True)
        return self.frame_before(pos["b"], pos["w"])

    # Every edit runs in phases, each re-reading the real timeline from Resolve:
    #   1. cut clips at the needed frames;
    #   2. delete / move / insert, using the actual clip boundaries;
    #   3. close the 1–few-frame gaps Resolve's frame rounding may leave
    #      (media and timeline frame rates often differ: 60 fps stream on 24 fps).
    def _run(self, ops):
        if not ops:
            return {"ok": True, "noop": True}
        try:
            res = self.link.run(ops)
        except LinkError as e:
            raise EditError(str(e))
        self.rebuild()
        if not res.get("ok"):
            self.undo_stack.clear()
            self.redo_stack.clear()
            raise EditError("Resolve: %s (виконано %s з %s операцій)" % (res.get("err"), res.get("done"), len(ops)))
        return res

    def _exec(self, planner, undo=None, label=""):
        res = self._run(planner.ops)
        self._close_gaps()
        self._record(undo, label)
        return res

    def _record(self, undo, label):
        if undo is not None:
            self.undo_stack.append({"label": label, "undo": undo, "fp": content_sig(self.link.snapshot, self.track)})
            del self.undo_stack[:-100]

    def _planner(self):
        snap = self.link.snapshot
        if not snap:
            raise EditError("Немає активного таймлайна в Resolve.")
        return Planner(snap, self.track)

    def _split_first(self, points):
        p = self._planner()
        for f in sorted(set(points), reverse=True):
            p.split(f)
        self._run(p.ops)
        items = self._planner().items
        bounds = sorted({x for it in items for x in (it["s"], it["e"])})
        out = []
        for f in points:
            near = min(bounds, key=lambda x: abs(x - f)) if bounds else f
            out.append(near if abs(near - f) <= 3 else f)
        return out

    def _close_gaps(self, max_gap=3):
        p = self._planner()
        items = p.items
        for i in range(1, len(items)):
            gap = items[i]["s"] - items[i - 1]["e"]
            if 0 < gap <= max_gap:
                p.shift_from(items[i]["s"], -gap)
        self._run(p.ops)

    def close_all_gaps(self):
        """Ripple-close every gap between clips of the edit track."""
        with self._lock:
            p = self._planner()
            items = p.items
            for i in range(1, len(items)):
                gap = items[i]["s"] - items[i - 1]["e"]
                if gap > 0:
                    p.shift_from(items[i]["s"], -gap)
            res = self._run(p.ops)
            self.undo_stack.clear()
            self.redo_stack.clear()
            return res

    def _track_end(self):
        items = self._planner().items
        return items[-1]["e"] if items else 0

    def _do_delete(self, f1, f2):
        f1, f2 = self._split_first([f1, f2])
        p = self._planner()
        clips = p.delete(f1, f2)
        self._run(p.ops)
        self._close_gaps()
        return f1, clips

    def _do_insert(self, f, clips):
        if not clips:
            raise EditError("Нічого вставляти.")
        f = self._split_first([f])[0]
        before = self._track_end()
        p = self._planner()
        slack = 2 * len(clips) if any(it["s"] >= f for it in p.items) else 0
        p.insert(f, clips, slack)
        self._run(p.ops)
        self._close_gaps(max_gap=slack + 3)
        return f, self._track_end() - before

    def _do_move(self, f1, f2, fd):
        f1, f2, fd = self._split_first([f1, f2, fd])
        p = self._planner()
        dest = p.move(f1, f2, fd)
        if dest is None:
            return None
        self._run(p.ops)
        self._close_gaps()
        d = f2 - f1
        return {"op": "move", "f1": dest, "f2": dest + d, "fd": f2 if fd < f1 else f1}

    # ------------------------------------------------------------ text edits

    def delete(self, sel, v=None, record=True):
        with self._lock:
            self._block(sel["a"]["b"], v)
            f1, f2 = self.selection_frames(sel)
            return self.delete_frames(f1, f2, record, "Вирізати")

    def delete_frames(self, f1, f2, record=True, label="Вирізати"):
        with self._lock:
            f1, clips = self._do_delete(f1, f2)
            if record:
                self.redo_stack.clear()
                self._record({"op": "insert", "f": f1, "clips": clips}, label)
            return {"ok": True}

    def insert_clips(self, f, clips, record=True, label="Вставити"):
        with self._lock:
            f, total = self._do_insert(f, clips)
            if record:
                self.redo_stack.clear()
                self._record({"op": "delete", "f1": f, "f2": f + total}, label)
            return {"ok": True}

    def move(self, sel, pos, v=None):
        with self._lock:
            self._block(sel["a"]["b"], v)
            f1, f2 = self.selection_frames(sel)
            fd = self.insertion_frame(pos)
            return self.move_frames(f1, f2, fd)

    def move_frames(self, f1, f2, fd, record=True, label="Перемістити"):
        with self._lock:
            undo = self._do_move(f1, f2, fd)
            if undo is None:
                return {"ok": True, "noop": True}
            if record:
                self.redo_stack.clear()
                self._record(undo, label)
            return {"ok": True}

    def copy(self, sel):
        f1, f2 = self.selection_frames(sel)
        self.clipboard = self._planner().clips_between(f1, f2)
        return {"ok": True, "clips": len(self.clipboard)}

    def paste(self, pos, v=None):
        with self._lock:
            if pos.get("b") is not None:
                self._block(pos["b"], v)
            return self.insert_clips(self.insertion_frame(pos), copy.deepcopy(self.clipboard), label="Вставити")

    def duplicate(self, sel, v=None):
        with self._lock:
            self._block(sel["a"]["b"], v)
            f1, f2 = self.selection_frames(sel)
            clips = self._planner().clips_between(f1, f2)
            return self.insert_clips(f2, clips, label="Дублювати")

    def restore(self, k, i0, i1, v=None):
        """Bring cut words [i0, i1) back next to their source neighbour."""
        with self._lock:
            b = self._block(k, v)
            tr = self.transcripts.get(b["m"])
            run = next((c for c in b["cut"] if c["i0"] <= i0 and i1 <= c["i1"]), None)
            if not tr or run is None or i1 <= i0:
                raise EditError("Ці слова не вирізані.")
            p = self._planner()
            it = next(x for x in p.items if x["s"] == b["s"])
            mfps = it["mfps"]
            t0 = tr.gap_before(i0)
            t1 = tr.gap_after(i1 - 1)
            if run["side"] == "after":
                if i0 == run["i0"]:
                    t0 = b["src1"]
                nxt = self._next_use(b["m"], i1) if i1 == run["i1"] else None
                if nxt is not None:
                    t1 = min(t1, nxt)
                s, e = int(round(t0 * mfps)), int(round(t1 * mfps))
                if i0 == run["i0"] and abs(t0 - b["src1"]) < 1.0 / mfps + 1e-6:
                    d = p.extend_end(it, e)
                    self.redo_stack.clear()
                    return self._exec(p, {"op": "delete", "f1": b["e"], "f2": b["e"] + d}, "Відновити")
                return self.insert_clips(b["e"], [{"path": b["path"], "s": s, "e": e, "mfps": mfps}], label="Відновити")
            if i1 == run["i1"]:
                t1 = b["src0"]
            s, e = int(round(t0 * mfps)), int(round(t1 * mfps))
            return self.insert_clips(b["s"], [{"path": b["path"], "s": s, "e": e, "mfps": mfps}], label="Відновити")

    def _next_use(self, key, idx):
        starts = [b["src0"] for b in self.view["blocks"] if b["m"] == key and b["i0"] >= idx]
        return min(starts) if starts else None

    def _check_history(self, entry, stack, message):
        if not same_content(entry["fp"], content_sig(self.link.snapshot, self.track)):
            stack.clear()
            raise EditError(message)

    def _revert(self, u):
        """Undo one recorded action; returns how to redo it (or None)."""
        if u["op"] == "text":
            redo = self._text_snapshot(u["keys"])
            self._text_restore(u)
            return redo
        if u["op"] == "insert":
            f, total = self._do_insert(u["f"], u["clips"])
            return {"op": "delete", "f1": f, "f2": f + total}
        if u["op"] == "delete":
            f1, clips = self._do_delete(u["f1"], u["f2"])
            return {"op": "insert", "f": f1, "clips": clips}
        if u["op"] == "move":
            return self._do_move(u["f1"], u["f2"], u["fd"])
        if u["op"] == "timeline":
            self._run([{"op": "set_timeline", "id": u["id"]}])
            return None
        raise EditError("Невідома дія скасування.")

    def undo(self):
        with self._lock:
            if not self.undo_stack:
                raise EditError("Нічого скасовувати.")
            entry = self.undo_stack[-1]
            self._check_history(entry, self.undo_stack,
                                "Таймлайн змінено в Resolve — історію скасувань очищено (використайте Ctrl+Z у Resolve).")
            self.undo_stack.pop()
            redo = self._revert(entry["undo"])
            if redo:
                self.redo_stack.append({"label": entry["label"], "undo": redo, "fp": content_sig(self.link.snapshot, self.track)})
            return {"ok": True}

    def redo(self):
        with self._lock:
            if not self.redo_stack:
                raise EditError("Нічого повторювати.")
            entry = self.redo_stack.pop()
            self._check_history(entry, self.redo_stack, "Таймлайн змінено в Resolve.")
            saved, self.redo_stack = self.redo_stack, []
            try:
                u = entry["undo"]
                if u["op"] == "text":
                    self._record(self._text_snapshot(u["keys"]), entry["label"])
                    self._text_restore(u)
                    return {"ok": True}
                if u["op"] == "insert":
                    return self.insert_clips(u["f"], u["clips"], label=entry["label"])
                if u["op"] == "delete":
                    return self.delete_frames(u["f1"], u["f2"], label=entry["label"])
                return self.move_frames(u["f1"], u["f2"], u["fd"], label=entry["label"])
            finally:
                self.redo_stack = saved

    # ------------------------------------------------------------ navigation

    def goto_frame(self, frame):
        fps = self.view.get("fps") or 25
        return self.link.run([{"op": "go", "tc": frame_to_tc(frame, fps)}], timeout=10)

    def goto_word(self, k, w):
        b = self._block(k)
        if b["m"] and b["i0"] <= w < b["i1"]:
            t0 = self.transcripts[b["m"]].words[w][0]
            return self.goto_frame(self._frame(b, t0))
        return self.goto_frame(b["s"])

    # ------------------------------------------------------------ transcript editing

    def _text_snapshot(self, keys):
        """Undo point for transcript edits: the words, fragments and speakers of these files + topics."""
        import copy as _copy
        return {"op": "text", "keys": list(keys),
                "tr": {k: _copy.deepcopy({"words": self.transcripts[k].words, "frags": self.transcripts[k].frags,
                                          "speakers": self.transcripts[k].speakers})
                       for k in keys if k in self.transcripts},
                "topics": _copy.deepcopy(self.project_state.get("topics") or [])}

    def _text_restore(self, snap):
        restored = []
        for k, d in snap["tr"].items():
            tr = self.transcripts.get(k)
            if tr is not None:
                tr.words, tr.frags, tr.speakers = d["words"], d["frags"], d["speakers"]
                tr._mids = None
                restored.append(tr)
        self.project_state["topics"] = snap["topics"]
        self.save_project()
        self._text_changed(restored)

    def _text_changed(self, transcripts):
        """Words or speakers of these transcripts changed (the video did not)."""
        for tr in transcripts:
            tr.save()
        self.tversion += 1
        self.rebuild(force=True)

    def _text_undo(self, keys, label):
        self._record(self._text_snapshot(keys), label)
        self.redo_stack.clear()
        # a text undo point holds a copy of the whole transcript: keep the last 30 of them
        texts = [e for e in self.undo_stack if e["undo"].get("op") == "text"]
        for e in texts[:-30]:
            self.undo_stack.remove(e)

    def edit_word(self, key, idx, text):
        with self._lock:
            tr = self.transcripts[key]
            text = (text or "").strip()
            if not text:
                raise EditError("Порожнє слово — щоб прибрати слово з відео, виріжте його.")
            self._text_undo([key], "Виправлення")
            tr.words[idx][2] = text
            self._text_changed([tr])

    def edit_words(self, key, i0, i1, text):
        """Replace the text of words[i0:i1] (a phrase or a whole sentence) with `text`.
        Unchanged words keep their timing; changed ones share the time of the words they replace.
        The video is not touched; fragment numbering stays the same."""
        with self._lock:
            tr = self.transcripts[key]
            i0, i1 = max(0, int(i0)), min(len(tr.words), int(i1))
            new = (text or "").split()
            if i1 <= i0:
                raise EditError("Нічого не виділено.")
            if not new:
                raise EditError("Порожній текст — щоб прибрати слова з відео, виріжте їх.")
            self._text_undo([key], "Виправлення")
            old = tr.words[i0:i1]
            out = respell(old, new)
            d = len(out) - len(old)
            tr.words[i0:i1] = out
            tr._mids = None
            if d:
                n = len(out)
                shift = lambda x: x if x <= i0 else x + d if x >= i1 else min(x, i0 + n)  # noqa: E731
                tr.frags = [[shift(a), shift(z)] for a, z in tr.frags]
                for t in self.project_state.get("topics") or []:
                    if t.get("m") == key and t.get("i", 0) >= i1:
                        t["i"] += d
                    elif t.get("m") == key and t.get("i", 0) > i0:
                        t["i"] = i0
                self.save_project()
            self._text_changed([tr])

    def rename_speaker(self, key, sid, name):
        with self._lock:
            tr = self.transcripts[key]
            self._text_undo([key], "Ім'я мовця")
            for s in tr.speakers:
                if s["id"] == sid:
                    s["name"] = (name or "").strip() or s["name"]
            self._text_changed([tr])

    def _speaker_index(self, tr, sid):
        if sid == "new":
            n = len(tr.speakers) + 1
            while any(s["id"] == "S%d" % n for s in tr.speakers):
                n += 1
            sid = "S%d" % n
            tr.speakers.append({"id": sid, "name": "Мовець %d" % n})
        for i, s in enumerate(tr.speakers):
            if s["id"] == sid:
                return i
        raise EditError("Мовця %s немає." % sid)

    def set_words_speaker(self, ranges, sid):
        """Give exactly these words [{m, i0, i1}] to speaker ``sid`` ("new" = a new speaker per file).
        Fragments (the ids that stories refer to) are left as they are."""
        with self._lock:
            keys = sorted({r["m"] for r in ranges if r.get("m") in self.transcripts})
            if keys:
                self._text_undo(keys, "Мовець")
            touched = {}
            for r in ranges:
                key = r["m"]
                if key not in self.transcripts:
                    raise EditError("Транскрипцію не знайдено.")
                tr = self.transcripts[key]
                if key not in touched:
                    touched[key] = self._speaker_index(tr, sid)
                i0, i1 = max(0, int(r["i0"])), min(len(tr.words), int(r["i1"]))
                for w in tr.words[i0:i1]:
                    w[3] = touched[key]
            if not touched:
                raise EditError("Виділіть слова.")
            self._text_changed([self.transcripts[key] for key in touched])

    def transcript_payload(self):
        out = {}
        for key, tr in self.transcripts.items():
            out[key] = {"path": tr.path, "tag": self.tags.get(key), "lang": tr.lang,
                        "speakers": [dict(s, color=SPEAKER_COLORS[i % len(SPEAKER_COLORS)]) for i, s in enumerate(tr.speakers)],
                        "frags": tr.frags, "words": [[round(w[0], 3), round(w[1], 3), w[2], w[3]] for w in tr.words]}
        return out
