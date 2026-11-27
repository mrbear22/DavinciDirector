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

import bisect
import copy
import hashlib
import json
import os
import re
import threading
import time

from .link import LinkError, fingerprint, frame_to_tc, norm_path
from .storage import atomic_write, base_dir

SPEAKER_COLORS = ["#d8a657", "#7daea3", "#d3869b", "#a9b665", "#e78a4e", "#89b482", "#ea6962", "#b0a3d4"]


class EditError(Exception):
    pass


# ------------------------------------------------------------------ transcripts


def transcripts_dir():
    d = os.path.join(base_dir(), "transcripts")
    os.makedirs(d, exist_ok=True)
    return d


def media_key(path):
    return hashlib.sha1(norm_path(path).encode("utf-8")).hexdigest()[:16]


class Transcript:
    """Words of one media file.  words: [t0, t1, text, speaker_index, prob]."""

    def __init__(self, path, words=None, speakers=None, frags=None, lang=None, covered=None, meta=None):
        self.path = path
        self.words = words or []
        self.speakers = speakers or [{"id": "S1", "name": "Мовець 1"}]
        self.frags = frags if frags is not None else segment(self.words)
        self.lang = lang
        self.covered = covered or ([[self.words[0][0], self.words[-1][1]]] if self.words else [])
        self.meta = meta or {}
        self._mids = None

    @property
    def key(self):
        return media_key(self.path)

    def mids(self):
        if self._mids is None or len(self._mids) != len(self.words):
            self._mids = [(w[0] + w[1]) / 2.0 for w in self.words]
        return self._mids

    def range_for(self, src0, src1):
        """Word index range [i0, i1) whose midpoints lie in [src0, src1)."""
        m = self.mids()
        return bisect.bisect_left(m, src0), bisect.bisect_left(m, src1)

    def frag_of(self, idx):
        starts = [f[0] for f in self.frags]
        k = bisect.bisect_right(starts, idx) - 1
        return k if 0 <= k < len(self.frags) and idx < self.frags[k][1] else None

    def gap_before(self, i, pad=0.3):
        """Source time of the cut point before word i."""
        w = self.words[i]
        if i == 0:
            return max(0.0, w[0] - pad)
        prev = self.words[i - 1]
        return max((prev[1] + w[0]) / 2.0, w[0] - pad)

    def gap_after(self, i, pad=0.3):
        w = self.words[i]
        if i + 1 >= len(self.words):
            return w[1] + pad
        nxt = self.words[i + 1]
        return min((w[1] + nxt[0]) / 2.0, w[1] + pad)

    def to_json(self):
        return {"path": self.path, "lang": self.lang, "speakers": self.speakers, "frags": self.frags,
                "covered": self.covered, "meta": self.meta, "words": self.words}

    @classmethod
    def from_json(cls, d):
        return cls(d["path"], [list(w) for w in d.get("words", [])], d.get("speakers"), d.get("frags"),
                   d.get("lang"), d.get("covered"), d.get("meta"))

    def save(self):
        atomic_write(os.path.join(transcripts_dir(), self.key + ".json"), json.dumps(self.to_json(), ensure_ascii=False))

    @classmethod
    def load(cls, path):
        f = os.path.join(transcripts_dir(), media_key(path) + ".json")
        if not os.path.exists(f):
            return None
        with open(f, encoding="utf-8") as fh:
            return cls.from_json(json.load(fh))


_SENT_END = re.compile(r"[.!?…]+[\"»”')\]]*$")


def segment(words, max_words=60, max_len=25.0, pause=1.0):
    """Split words into fragments: speaker change, long pause, sentence end."""
    frags, start = [], 0
    for i in range(1, len(words) + 1):
        if i == len(words):
            frags.append([start, i])
            break
        prev, cur = words[i - 1], words[i]
        n = i - start
        dur = prev[1] - words[start][0]
        brk = (cur[3] != prev[3] or cur[0] - prev[1] >= pause or n >= max_words or dur >= max_len
               or (n >= 4 and _SENT_END.search(prev[2] or "") and cur[0] - prev[1] >= 0.15)
               or (n >= 12 and _SENT_END.search(prev[2] or "")))
        if brk:
            frags.append([start, i])
            start = i
    return frags if words else []


def _norm_word(w):
    return re.sub(r"[\W_]+", "", (w or "").lower())


def _spread(slots, texts):
    """New words over the time of `slots` (old words), proportional to their length; speaker of the
    slot each new word lands in."""
    t0, t1 = slots[0][0], slots[-1][1]
    if len(slots) == len(texts):
        return [[s[0], s[1], t, s[3], 1.0] for s, t in zip(slots, texts)]
    weights = [max(1, len(t)) for t in texts]
    total, pos, out = float(sum(weights)), t0, []
    for t, wgt in zip(texts, weights):
        end = pos + (t1 - t0) * wgt / total
        mid = (pos + end) / 2
        spk = next((s[3] for s in slots if s[0] <= mid <= s[1]), slots[0][3])
        out.append([round(pos, 3), round(end, 3), t, spk, 1.0])
        pos = end
    out[-1][1] = t1
    return out


def _respell(old, new):
    """Words for a corrected phrase: words the user kept keep their exact timing."""
    import difflib
    ops = difflib.SequenceMatcher(None, [_norm_word(w[2]) for w in old], [_norm_word(t) for t in new],
                                  autojunk=False).get_opcodes()
    out, pending = [], None  # inserted words wait for the next old word to share its time
    for tag, a, b, c, d in ops:
        if tag == "equal":
            for k in range(b - a):
                w = list(old[a + k])
                w[2] = new[c + k]
                if pending:
                    out.extend(_spread([w], pending + [w[2]]))
                    pending = None
                else:
                    out.append(w)
        elif tag == "delete":
            continue  # the sound stays in the video, it just has no text now
        elif tag == "insert":
            pending = (pending or []) + new[c:d]
        else:  # replace
            out.extend(_spread(old[a:b], (pending or []) + new[c:d]))
            pending = None
    if pending:  # words added at the very end share the last word's time
        if out:
            last = out.pop()
            out.extend(_spread([last], [last[2]] + pending))
        else:
            out.extend(_spread(old, pending))
    if not out:
        out = _spread(old, new)
    return out


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
    # words of transcribed media that no clip uses -> show as cut next to their source neighbour
    for key, bl in by_media.items():
        tr = transcripts[key]
        used = sorted((b["i0"], b["i1"]) for b in bl if b["i1"] > b["i0"])
        runs, pos = [], 0
        for a, z in used:
            if a > pos:
                runs.append((pos, a))
            pos = max(pos, z)
        if pos < len(tr.words):
            runs.append((pos, len(tr.words)))
        for a, z in runs:
            pred = next((b for b in bl if b["i1"] == a and b["i1"] > b["i0"]), None)
            if pred is None:
                pred = next((b for b in bl if b["i1"] <= a and b["i1"] > b["i0"] and
                             not any(c["i0"] < a and c["i1"] > b["i1"] for c in bl)), None)
            if pred is not None:
                pred["cut"].append({"side": "after", "i0": a, "i1": z})
                continue
            succ = next((b for b in bl if b["i0"] == z), None) or min(bl, key=lambda b: abs(b["i0"] - z))
            succ["cut"].append({"side": "before", "i0": a, "i1": z})
    return {"blocks": blocks, "fps": fps, "start": snap["start"], "fp": fingerprint(snap), "tl": snap.get("tl"),
            "track": track, "check": snap.get("check") or []}


# ------------------------------------------------------------------ planner


class Planner:
    """Simulates the edit track while emitting bridge operations, so that every
    item reference ({t, i, s}) is valid at the moment the bridge executes it."""

    def __init__(self, snap, track):
        self.fps = snap["fps"]
        self.track = track
        self.items = [dict(it) for it in snap["tracks"].get(track, [])]
        self.ttype = self.items[0]["t"] if self.items else ("audio" if track.startswith("audio") else "video")
        self.tindex = self.items[0]["i"] if self.items else int(re.sub(r"\D", "", track) or 1)
        ends = [it["e"] for key in snap["tracks"] for it in snap["tracks"][key]]
        self.max_end = max(ends + [snap["start"]])
        self.start = snap["start"]
        self.ops = []

    def ref(self, it):
        return {"t": it["t"], "i": it["i"], "s": it["s"]}

    def _sort(self):
        self.items.sort(key=lambda x: x["s"])

    def tl_frames(self, src_frames, mfps):
        return int(round(src_frames * self.fps / mfps))

    def src_frames(self, tl_frames, mfps):
        return int(round(tl_frames * mfps / self.fps))

    def at(self, f):
        return next((it for it in self.items if it["s"] < f < it["e"]), None)

    def split(self, f):
        x = self.at(f)
        if x is None:
            return
        cut = x["sf0"] + self.src_frames(f - x["s"], x["mfps"])
        end = x["sf0"] + self.src_frames(x["e"] - x["s"], x["mfps"])
        self.ops.append({"op": "trim_end", "ref": self.ref(x), "n": x["e"] - f})
        old_e = x["e"]
        x["e"] = f
        x["src1"] = x["src0"] + (f - x["s"]) / self.fps
        self.ops.append({"op": "append", "clips": [self._clip(x["path"], cut, end, f, via=x)]})
        y = dict(x, s=f, e=old_e, sf0=cut, src0=cut / x["mfps"], src1=cut / x["mfps"] + (old_e - f) / self.fps)
        self.items.append(y)
        self._sort()

    def _clip(self, path, s, e, rec=None, via=None):
        c = {"path": path, "s": int(s), "e": int(e), "track": self.tindex}
        if via is None:
            via = next((it for it in self.items if norm_path(it["path"]) == norm_path(path)), None)
        if via is not None:
            c["via"] = self.ref(via)
        if rec is not None:
            c["rec"] = int(rec)
        if self.ttype == "audio":
            c["mt"] = 2
        return c

    def shift_from(self, f, delta, exclude=()):
        group = [it for it in self.items if it["s"] >= f and it not in exclude]
        if not group or not delta:
            return
        self.ops.append({"op": "move", "refs": [self.ref(it) for it in group], "to": group[0]["s"] + delta})
        for it in group:
            it["s"] += delta
            it["e"] += delta
        self._sort()

    def clips_between(self, f1, f2):
        """Source ranges (path, src frames) of the edit track in [f1, f2)."""
        out = []
        for it in self.items:
            a, z = max(it["s"], f1), min(it["e"], f2)
            if z > a:
                s = it["sf0"] + self.src_frames(a - it["s"], it["mfps"])
                out.append({"path": it["path"], "s": s, "e": s + self.src_frames(z - a, it["mfps"]), "mfps": it["mfps"]})
        return out

    def delete(self, f1, f2):
        clips = self.clips_between(f1, f2)
        self.split(f1)
        self.split(f2)
        victims = [it for it in self.items if it["s"] >= f1 and it["e"] <= f2]
        if victims:
            self.ops.append({"op": "delete", "refs": [self.ref(it) for it in victims]})
            for it in victims:
                self.items.remove(it)
        self.shift_from(f2, -(f2 - f1))
        return clips

    def insert(self, f, clips, slack=0):
        self.split(f)
        durs = [self.tl_frames(c["e"] - c["s"], c["mfps"]) for c in clips]
        total = sum(durs)
        self.shift_from(f, total + slack)
        rec, specs = f, []
        for c, d in zip(clips, durs):
            specs.append(self._clip(c["path"], c["s"], c["e"], rec))
            self.items.append({"t": self.ttype, "i": self.tindex, "s": rec, "e": rec + d, "sf0": c["s"],
                               "mfps": c["mfps"], "path": c["path"], "name": os.path.basename(c["path"]),
                               "src0": c["s"] / c["mfps"], "src1": c["s"] / c["mfps"] + d / self.fps})
            rec += d
        if specs:
            self.ops.append({"op": "append", "clips": specs})
        self._sort()
        return total

    def extend_end(self, item, src_end):
        """Lengthen ``item`` to source frame ``src_end`` rippling later clips."""
        cur_end = item["sf0"] + self.src_frames(item["e"] - item["s"], item["mfps"])
        d = self.tl_frames(src_end - cur_end, item["mfps"])
        if d <= 0:
            return 0
        self.shift_from(item["e"], d, exclude=(item,))
        self.ops.append({"op": "trim_end", "ref": self.ref(item), "n": -d})
        item["e"] += d
        return d

    def move(self, f1, f2, fd):
        if f1 <= fd <= f2 or f2 <= f1:
            return None
        self.split(f1)
        self.split(f2)
        self.split(fd)
        d = f2 - f1
        block = [it for it in self.items if it["s"] >= f1 and it["e"] <= f2]
        if not block:
            return None
        park = max([self.max_end] + [it["e"] for it in self.items]) + 50
        self.ops.append({"op": "move", "refs": [self.ref(it) for it in block], "to": park})
        off = park - block[0]["s"]
        for it in block:
            it["s"] += off
            it["e"] += off
        if fd > f2:
            mid = [it for it in self.items if f2 <= it["s"] < fd and it not in block]
            dest = fd - d
            if mid:
                self.ops.append({"op": "move", "refs": [self.ref(it) for it in mid], "to": mid[0]["s"] - d})
                for it in mid:
                    it["s"] -= d
                    it["e"] -= d
        else:
            mid = [it for it in self.items if fd <= it["s"] < f1 and it not in block]
            dest = fd
            if mid:
                self.ops.append({"op": "move", "refs": [self.ref(it) for it in mid], "to": mid[0]["s"] + d})
                for it in mid:
                    it["s"] += d
                    it["e"] += d
        first = min(block, key=lambda x: x["s"])
        self.ops.append({"op": "move", "refs": [self.ref(it) for it in block], "to": dest})
        off = dest - first["s"]
        for it in block:
            it["s"] += off
            it["e"] += off
        self._sort()
        return dest


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
            except Exception:
                import traceback
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
                except (OSError, ValueError):
                    pass
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

    def undo(self):
        with self._lock:
            if not self.undo_stack:
                raise EditError("Нічого скасовувати.")
            entry = self.undo_stack[-1]
            if not same_content(entry["fp"], content_sig(self.link.snapshot, self.track)):
                self.undo_stack.clear()
                raise EditError("Таймлайн змінено в Resolve — історію скасувань очищено (використайте Ctrl+Z у Resolve).")
            self.undo_stack.pop()
            u = entry["undo"]
            if u["op"] == "text":
                redo = self._text_snapshot(u["keys"])
                self._text_restore(u)
            elif u["op"] == "insert":
                f, total = self._do_insert(u["f"], u["clips"])
                redo = {"op": "delete", "f1": f, "f2": f + total}
            elif u["op"] == "delete":
                f1, clips = self._do_delete(u["f1"], u["f2"])
                redo = {"op": "insert", "f": f1, "clips": clips}
            elif u["op"] == "move":
                redo = self._do_move(u["f1"], u["f2"], u["fd"])
            elif u["op"] == "timeline":
                self._run([{"op": "set_timeline", "id": u["id"]}])
                redo = None
            else:
                raise EditError("Невідома дія скасування.")
            if redo:
                self.redo_stack.append({"label": entry["label"], "undo": redo, "fp": content_sig(self.link.snapshot, self.track)})
            return {"ok": True}

    def redo(self):
        with self._lock:
            if not self.redo_stack:
                raise EditError("Нічого повторювати.")
            entry = self.redo_stack.pop()
            if not same_content(entry["fp"], content_sig(self.link.snapshot, self.track)):
                self.redo_stack.clear()
                raise EditError("Таймлайн змінено в Resolve.")
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
        for k, d in snap["tr"].items():
            tr = self.transcripts.get(k)
            if tr is None:
                continue
            tr.words, tr.frags, tr.speakers = d["words"], d["frags"], d["speakers"]
            tr._mids = None
            tr.save()
        self.project_state["topics"] = snap["topics"]
        self.save_project()
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
            tr.save()
            self.tversion += 1
            self.rebuild(force=True)

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
            out = _respell(old, new)
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
            tr.save()
            self.tversion += 1
            self.rebuild(force=True)

    def rename_speaker(self, key, sid, name):
        with self._lock:
            tr = self.transcripts[key]
            self._text_undo([key], "Ім'я мовця")
            for s in tr.speakers:
                if s["id"] == sid:
                    s["name"] = (name or "").strip() or s["name"]
            tr.save()
            self.tversion += 1
            self.rebuild(force=True)

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

    def set_fragment_speaker(self, key, frag, sid):
        a, z = self.transcripts[key].frags[frag]
        self.set_words_speaker([{"m": key, "i0": a, "i1": z}], sid)

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
            for key in touched:
                self.transcripts[key].save()
            self.tversion += 1
            self.rebuild(force=True)

    def transcript_payload(self):
        out = {}
        for key, tr in self.transcripts.items():
            out[key] = {"path": tr.path, "tag": self.tags.get(key), "lang": tr.lang,
                        "speakers": [dict(s, color=SPEAKER_COLORS[i % len(SPEAKER_COLORS)]) for i, s in enumerate(tr.speakers)],
                        "frags": tr.frags, "words": [[round(w[0], 3), round(w[1], 3), w[2], w[3]] for w in tr.words]}
        return out
