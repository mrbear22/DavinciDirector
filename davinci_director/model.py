"""Project data model and the text-based editing engine.

Concepts
--------
token   One word (``k == "w"``) or a stretch of non-speech audio (``k == "g"``,
        "gap").  Tokens of a clip *tile* its used source range without holes,
        so the full token list reproduces the original timeline exactly and
        cutting a token removes precisely its media interval.  Gap tokens keep
        speech-less gameplay editable as text ("[··· 6.0s]").
        Fields: i, k, c (clip index), t0/t1 (seconds in the media file),
        r0/r1 (seconds on the original timeline), w (text), sp (speaker),
        p (confidence), hl (loudness highlight 0..3).
piece   A paragraph of the edit: a contiguous token range ``a..b`` of the
        original plus a set of cut tokens.  The edit is an ordered list of
        pieces; reordering / duplicating / pasting pieces rearranges media.
        ``off`` removes the whole piece (restorable).
render  Pieces -> ordered list of media segments -> frames for
        MediaPool.AppendToTimeline.
"""

import bisect
import copy
import json
import math
import re
import time
import uuid

GAP_MIN = 0.8          # silences longer than this become gap tokens
GAP_MAX = 6.0          # long silences are chunked so they can be trimmed
EDGE = 0.35            # breathing room kept around speech when pauses are removed
PARA_GAP = 2.5         # a gap run this long starts its own paragraph
PARA_WORDS = 55        # soft paragraph length (split at sentence end)
SENTENCE_END = re.compile(r"[.!?…]+[\"»”)]*$")

ROLE_COLORS = {
    "hook": "Red", "setup": "Blue", "development": "Cyan", "escalation": "Yellow",
    "climax": "Fuchsia", "resolution": "Green", "outro": "Lavender", "context": "Sky",
    "callback": "Pink", "montage": "Mint", "other": "Sand",
}
SPEAKER_PALETTE = ["#d9822b", "#4f9bd9", "#5fb36b", "#c0629b", "#c9b03c", "#7f75d6", "#4fb3ae", "#d35d50"]


def new_id(prefix=""):
    return prefix + uuid.uuid4().hex[:10]


def fmt_ts(sec):
    sec = max(0.0, float(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return "%02d:%02d:%04.1f" % (h, m, s)


def parse_ts(value):
    """'HH:MM:SS.s' | 'MM:SS' | seconds -> float seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    v = str(value).strip().replace(",", ".")
    if not v:
        raise ValueError("empty timestamp")
    parts = v.split(":")
    total = 0.0
    for p in parts:
        total = total * 60 + float(p)
    return total


# ======================================================================
# token construction

def build_tokens(clips, clip_words, clip_fps_default=25.0):
    """Create tiled tokens.

    clips:      project clips (with rec_start/src_in/src_out frames, fps)
    clip_words: {clip_index: [{"t0","t1","w","sp","p"}]} with times in
                *media* seconds (absolute position within the file).
    """
    tokens = []
    for ci, clip in enumerate(clips):
        mfps = clip.get("media_fps") or clip_fps_default
        tfps = clip.get("fps") or mfps
        c0 = clip["src_in"] / mfps
        c1 = clip["src_out"] / mfps
        rec0 = clip["rec_start"] / tfps
        words = sorted((w for w in clip_words.get(ci, []) if w["t1"] > c0 and w["t0"] < c1 and w["w"].strip()),
                       key=lambda w: w["t0"])
        out = []
        cursor = c0

        def gap(a, b):
            # short "breathing" edges next to speech + chunked middle, so
            # pauses can be shortened without touching the words around them
            pieces_ = []
            if b - a > 3 * EDGE:
                pieces_.append((a, a + EDGE))
                mid0, mid1 = a + EDGE, b - EDGE
            else:
                mid0, mid1 = a, b
            n = max(1, int(math.ceil((mid1 - mid0) / GAP_MAX)))
            step = (mid1 - mid0) / n
            for j in range(n):
                pieces_.append((mid0 + j * step, mid0 + (j + 1) * step if j < n - 1 else mid1))
            if b - a > 3 * EDGE:
                pieces_.append((b - EDGE, b))
            for x0, x1 in pieces_:
                out.append({"k": "g", "t0": x0, "t1": x1, "w": "", "sp": None, "p": 1.0})

        for w in words:
            t0 = max(cursor, max(c0, float(w["t0"])))
            t1 = min(c1, max(t0 + 0.02, float(w["t1"])))
            if t1 <= cursor:
                continue
            if t0 - cursor > GAP_MIN:
                gap(cursor, t0)
            elif out:
                out[-1]["t1"] = t0      # short pause belongs to previous word
            else:
                t0 = cursor             # leading short pause belongs to word
            out.append({"k": "w", "t0": t0, "t1": t1, "w": w["w"].strip(), "sp": w.get("sp"),
                        "p": round(float(w.get("p", 1.0)), 3)})
            cursor = t1
        if c1 - cursor > GAP_MIN or not out:
            if c1 > cursor:
                gap(cursor, c1)
        elif out:
            out[-1]["t1"] = c1
        for t in out:
            t["c"] = ci
            t["r0"] = round(rec0 + (t["t0"] - c0) * 1.0, 4)
            t["r1"] = round(rec0 + (t["t1"] - c0) * 1.0, 4)
            t["t0"] = round(t["t0"], 4)
            t["t1"] = round(t["t1"], 4)
            t["hl"] = 0
            tokens.append(t)
    for i, t in enumerate(tokens):
        t["i"] = i
    return tokens


def initial_pieces(tokens):
    pieces = []
    if not tokens:
        return pieces
    start = 0
    words_in = 0

    def flush(end):
        nonlocal start, words_in
        if end >= start:
            pieces.append({"id": new_id("p"), "a": start, "b": end, "cut": [], "off": False, "label": ""})
        start = end + 1
        words_in = 0

    for i, t in enumerate(tokens):
        if i == start:
            words_in = 1 if t["k"] == "w" else 0
            continue
        prev = tokens[i - 1]
        brk = False
        if t["c"] != prev["c"]:
            brk = True
        elif t["k"] == "w" and prev["k"] == "w" and t.get("sp") != prev.get("sp") and t.get("sp") and prev.get("sp"):
            brk = True
        else:
            # gap runs longer than PARA_GAP form their own paragraph
            if t["k"] != prev["k"]:
                if t["k"] == "g":
                    run = _gap_run(tokens, i)
                    brk = run >= PARA_GAP
                else:
                    run = _gap_run_back(tokens, i - 1)
                    brk = run >= PARA_GAP
            elif t["k"] == "w" and words_in >= PARA_WORDS and SENTENCE_END.search(prev["w"]):
                brk = True
            elif t["k"] == "w" and words_in >= PARA_WORDS * 2:
                brk = True
        if brk:
            flush(i - 1)
            words_in = 1 if t["k"] == "w" else 0
        elif t["k"] == "w":
            words_in += 1
    flush(len(tokens) - 1)
    return pieces


def _gap_run(tokens, i):
    total, c = 0.0, tokens[i]["c"]
    while i < len(tokens) and tokens[i]["k"] == "g" and tokens[i]["c"] == c:
        total += tokens[i]["t1"] - tokens[i]["t0"]
        i += 1
    return total


def _gap_run_back(tokens, i):
    total, c = 0.0, tokens[i]["c"]
    while i >= 0 and tokens[i]["k"] == "g" and tokens[i]["c"] == c:
        total += tokens[i]["t1"] - tokens[i]["t0"]
        i -= 1
    return total


def apply_loudness(tokens, clip_loudness):
    """clip_loudness: {clip_index: (hop_seconds, [dB per hop])} measured from
    the extracted audio (times relative to the clip's media start).  Marks
    tokens whose peak is far above the clip median (screams, explosions...)."""
    for ci, (hop, values) in clip_loudness.items():
        if not values:
            continue
        srt = sorted(v for v in values if v > -90)
        if not srt:
            continue
        median = srt[len(srt) // 2]
        p95 = srt[int(len(srt) * 0.95)]
        span = max(6.0, p95 - median)
        for t in tokens:
            if t["c"] != ci:
                continue
            a = int(t["t0"] / hop)
            b = max(a + 1, int(math.ceil(t["t1"] / hop)))
            seg = values[a:b]
            if not seg:
                continue
            peak = max(seg)
            rel = (peak - median) / span
            t["hl"] = 3 if rel > 1.25 else 2 if rel > 1.0 else 1 if rel > 0.8 else 0


# ======================================================================
# project

class Project:
    VERSION = 2

    def __init__(self, data=None):
        self.data = data or self.empty()
        self._undo = []
        self._redo = []
        self._index_cache = None

    @staticmethod
    def empty():
        return {
            "version": Project.VERSION,
            "created": time.time(),
            "updated": time.time(),
            "resolve": {},
            "clips": [],
            "tokens": [],
            "speakers": {},
            "edit": {"pieces": [], "story_link": None, "synced_timeline_id": None,
                     "synced_timeline": None, "last_sync": None, "dirty": False},
            "analyses": [],
            "chat": [],
            "transcription": {"status": "none"},
            "settings": {"end_inclusive": True},
        }

    # ------------------------------------------------------------ basics
    @property
    def tokens(self):
        return self.data["tokens"]

    @property
    def pieces(self):
        return self.data["edit"]["pieces"]

    def touch(self, edit_changed=True):
        self.data["updated"] = time.time()
        if edit_changed:
            self.data["edit"]["dirty"] = True

    def to_json(self):
        return json.dumps(self.data, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json(cls, text):
        data = json.loads(text)
        base = cls.empty()
        for k, v in base.items():
            data.setdefault(k, v)
        for k, v in base["edit"].items():
            data["edit"].setdefault(k, v)
        return cls(data)

    def set_transcript(self, tokens, speakers):
        self.data["tokens"] = tokens
        self.data["speakers"] = speakers
        self.data["edit"]["pieces"] = initial_pieces(tokens)
        self.data["edit"]["story_link"] = None
        self._undo.clear()
        self._redo.clear()
        self._index_cache = None
        self.touch()

    def rec_starts(self):
        if self._index_cache is None or len(self._index_cache) != len(self.tokens):
            self._index_cache = [t["r0"] for t in self.tokens]
        return self._index_cache

    def tokens_in_range(self, start, end):
        """Token indices whose midpoint lies within [start, end) record secs."""
        starts = self.rec_starts()
        lo = max(0, bisect.bisect_right(starts, start) - 1)
        out = []
        for i in range(lo, len(self.tokens)):
            t = self.tokens[i]
            if t["r0"] >= end:
                break
            mid = (t["r0"] + t["r1"]) / 2
            if start <= mid < end:
                out.append(i)
        return out

    def piece(self, pid):
        for p in self.pieces:
            if p["id"] == pid:
                return p
        raise KeyError("piece %s not found" % pid)

    def piece_index(self, pid):
        for n, p in enumerate(self.pieces):
            if p["id"] == pid:
                return n
        raise KeyError("piece %s not found" % pid)

    # ------------------------------------------------------------ undo
    def _snapshot(self):
        return {"kind": "state", "edit": copy.deepcopy(self.data["edit"]["pieces"]),
                "link": copy.deepcopy(self.data["edit"]["story_link"]),
                "analyses": copy.deepcopy(self.data["analyses"])}

    def checkpoint(self, label=""):
        snap = self._snapshot()
        snap["label"] = label
        self._undo.append(snap)
        if len(self._undo) > 150:
            self._undo.pop(0)
        self._redo.clear()

    def _restore(self, snap):
        if snap["kind"] == "state":
            cur = self._snapshot()
            cur["label"] = snap.get("label", "")
            self.data["edit"]["pieces"] = snap["edit"]
            self.data["edit"]["story_link"] = snap["link"]
            self.data["analyses"] = snap["analyses"]
            return cur
        # word-level change list
        inverse = {"kind": "words", "label": snap.get("label", ""), "changes": []}
        for i, field, old, new in reversed(snap["changes"]):
            self.tokens[i][field] = old
            inverse["changes"].insert(0, [i, field, new, old])
        return inverse

    def undo(self):
        if not self._undo:
            return None
        snap = self._undo.pop()
        self._redo.append(self._restore(snap))
        self.touch()
        return snap.get("label", "")

    def redo(self):
        if not self._redo:
            return None
        snap = self._redo.pop()
        self._undo.append(self._restore(snap))
        self.touch()
        return snap.get("label", "")

    def history_state(self):
        return {"undo": len(self._undo), "redo": len(self._redo),
                "undo_label": self._undo[-1].get("label", "") if self._undo else "",
                "redo_label": self._redo[-1].get("label", "") if self._redo else ""}

    # ------------------------------------------------------------ edit ops
    def _norm_sel(self, sel):
        """[{pid, a, b}] -> [(piece, a, b)] clamped to piece range."""
        out = []
        for s in sel or []:
            p = self.piece(s["pid"])
            a = max(p["a"], int(s.get("a", p["a"])))
            b = min(p["b"], int(s.get("b", p["b"])))
            if a <= b:
                out.append((p, a, b))
        return out

    def op(self, name, **kw):
        fn = getattr(self, "op_" + name, None)
        if not fn:
            raise ValueError("unknown edit operation %r" % name)
        self.checkpoint(name)
        try:
            result = fn(**kw)
        except Exception:
            self._restore(self._undo.pop())
            raise
        self.touch()
        return result

    def op_cut(self, sel):
        for p, a, b in self._norm_sel(sel):
            cut = set(p["cut"])
            cut.update(range(a, b + 1))
            p["cut"] = sorted(cut)

    def op_restore(self, sel):
        for p, a, b in self._norm_sel(sel):
            p["cut"] = [i for i in p["cut"] if not (a <= i <= b)]
            p["off"] = False

    def op_delete_pieces(self, pids):
        for pid in pids:
            self.piece(pid)["off"] = True

    def op_restore_pieces(self, pids):
        for pid in pids:
            p = self.piece(pid)
            p["off"] = False

    def op_move(self, pids, before=None):
        moving = [p for p in self.pieces if p["id"] in set(pids)]
        rest = [p for p in self.pieces if p["id"] not in set(pids)]
        if before is None:
            idx = len(rest)
        else:
            idx = next((n for n, p in enumerate(rest) if p["id"] == before), len(rest))
        self.data["edit"]["pieces"] = rest[:idx] + moving + rest[idx:]

    def op_move_step(self, pids, delta):
        order = self.pieces
        ids = set(pids)
        rng = range(len(order)) if delta < 0 else range(len(order) - 1, -1, -1)
        for n in rng:
            if order[n]["id"] in ids:
                m = n + (-1 if delta < 0 else 1)
                if 0 <= m < len(order) and order[m]["id"] not in ids:
                    order[n], order[m] = order[m], order[n]

    def _clone(self, p, a=None, b=None):
        a = p["a"] if a is None else a
        b = p["b"] if b is None else b
        return {"id": new_id("p"), "a": a, "b": b, "cut": [i for i in p["cut"] if a <= i <= b],
                "off": False, "label": p.get("label", ""), "scene": p.get("scene"), "dup": True}

    def op_duplicate(self, pids=None, sel=None):
        new = []
        if sel:
            for p, a, b in self._norm_sel(sel):
                c = self._clone(p, a, b)
                self.pieces.insert(self.piece_index(p["id"]) + 1, c)
                new.append(c["id"])
        for pid in pids or []:
            p = self.piece(pid)
            c = self._clone(p)
            self.pieces.insert(self.piece_index(pid) + 1, c)
            new.append(c["id"])
        return {"new": new}

    def op_split(self, pid, at):
        p = self.piece(pid)
        at = int(at)
        if not (p["a"] < at <= p["b"]):
            return {"left": pid, "right": None}
        right = {"id": new_id("p"), "a": at, "b": p["b"], "cut": [i for i in p["cut"] if i >= at],
                 "off": p["off"], "label": "", "scene": p.get("scene")}
        p["b"] = at - 1
        p["cut"] = [i for i in p["cut"] if i < at]
        self.pieces.insert(self.piece_index(pid) + 1, right)
        return {"left": pid, "right": right["id"]}

    def op_merge(self, pids):
        idxs = sorted(self.piece_index(pid) for pid in pids)
        if len(idxs) < 2:
            return
        for x, y in zip(idxs, idxs[1:]):
            if y != x + 1 or self.pieces[y]["a"] != self.pieces[x]["b"] + 1:
                raise ValueError("Об'єднувати можна лише сусідні фрагменти, що йдуть підряд в оригіналі.")
        first = self.pieces[idxs[0]]
        for n in idxs[1:]:
            q = self.pieces[n]
            first["b"] = q["b"]
            first["cut"] = sorted(set(first["cut"]) | set(q["cut"]))
        del self.pieces[idxs[1]:idxs[-1] + 1]

    def op_paste(self, items, after=None, at=None):
        """items: [{a, b, cut}] token ranges (clipboard).  Inserted after piece
        ``after`` – or, if ``at`` (token index inside ``after``) is given, the
        piece is split there and items go between the halves."""
        insert_at = len(self.pieces)
        if after:
            if at is not None:
                p = self.piece(after)
                if p["a"] < int(at) <= p["b"]:
                    self.op_split(after, at)
            insert_at = self.piece_index(after) + 1
        new = []
        for it in items:
            a, b = int(it["a"]), int(it["b"])
            if not (0 <= a <= b < len(self.tokens)):
                continue
            c = {"id": new_id("p"), "a": a, "b": b, "cut": [i for i in it.get("cut", []) if a <= i <= b],
                 "off": False, "label": it.get("label", ""), "dup": True}
            self.pieces.insert(insert_at, c)
            insert_at += 1
            new.append(c["id"])
        return {"new": new}

    def op_extract(self, sel, after=None):
        """Cut-to-clipboard + paste: moves a token range to a new position."""
        items = [{"a": a, "b": b, "cut": [i for i in p["cut"] if a <= i <= b]} for p, a, b in self._norm_sel(sel)]
        self.op_cut(sel)
        return self.op_paste(items, after=after)

    def op_cut_tokens(self, indices):
        """Cut the given token indices wherever they appear (search results)."""
        idx = set(int(i) for i in indices)
        for p in self.pieces:
            hit = [i for i in idx if p["a"] <= i <= p["b"]]
            if hit:
                p["cut"] = sorted(set(p["cut"]) | set(hit))

    def op_remove_pauses(self, min_len=2.0):
        """Shorten every silence run longer than ``min_len`` seconds: cut its
        inner gap tokens, keep the short edges next to speech."""
        toks = self.tokens
        for p in self.pieces:
            cut = set(p["cut"])
            i = p["a"]
            while i <= p["b"]:
                if toks[i]["k"] != "g":
                    i += 1
                    continue
                j = i
                while j + 1 <= p["b"] and toks[j + 1]["k"] == "g" and toks[j + 1]["c"] == toks[i]["c"]:
                    j += 1
                if toks[j]["t1"] - toks[i]["t0"] >= min_len:
                    lo = i + 1 if (i > p["a"] and toks[i]["t1"] - toks[i]["t0"] <= EDGE + 1e-3) else i
                    hi = j - 1 if (j < p["b"] and toks[j]["t1"] - toks[j]["t0"] <= EDGE + 1e-3) else j
                    cut.update(range(lo, hi + 1))
                i = j + 1
            p["cut"] = sorted(cut)

    def op_set_label(self, pid, label):
        self.piece(pid)["label"] = label

    def op_reset(self):
        self.data["edit"]["pieces"] = initial_pieces(self.tokens)
        self.data["edit"]["story_link"] = None

    # ------------------------------------------------------------ words
    def edit_words(self, changes, label="text"):
        """changes: [{i, w?: text, sp?: speaker}]"""
        rec = {"kind": "words", "label": label, "changes": []}
        for ch in changes:
            t = self.tokens[int(ch["i"])]
            for field in ("w", "sp"):
                if field in ch and ch[field] != t.get(field):
                    rec["changes"].append([t["i"], field, t.get(field), ch[field]])
                    t[field] = ch[field]
        if rec["changes"]:
            self._undo.append(rec)
            self._redo.clear()
            self.touch(edit_changed=False)
        return len(rec["changes"])

    def assign_speaker(self, sel, speaker):
        if speaker not in self.data["speakers"]:
            self.add_speaker(speaker_id=speaker)
        changes = []
        for p, a, b in self._norm_sel(sel):
            for i in range(a, b + 1):
                if self.tokens[i]["k"] == "w":
                    changes.append({"i": i, "sp": speaker})
        return self.edit_words(changes, "speaker")

    def add_speaker(self, name=None, speaker_id=None):
        sid = speaker_id or "S%d" % (len(self.data["speakers"]) + 1)
        while speaker_id is None and sid in self.data["speakers"]:
            sid = "S%d" % (int(sid[1:]) + 1)
        n = len(self.data["speakers"])
        self.data["speakers"][sid] = {"name": name or "Мовець %d" % (n + 1),
                                      "color": SPEAKER_PALETTE[n % len(SPEAKER_PALETTE)]}
        self.touch(edit_changed=False)
        return sid

    def rename_speaker(self, sid, name=None, color=None):
        sp = self.data["speakers"][sid]
        if name is not None:
            sp["name"] = name.strip() or sp["name"]
        if color:
            sp["color"] = color
        self.touch(edit_changed=False)

    # ------------------------------------------------------------ render
    def render(self):
        """Return the ordered media segments of the current edit.

        Each segment: {c, s0, s1 (media secs), r0 (edit secs), dur, pid,
        a, b (first/last kept token)}.  Contiguous runs are merged.
        """
        toks = self.tokens
        segs = []
        for p in self.pieces:
            if p["off"]:
                continue
            cut = set(p["cut"])
            run = None
            for i in range(p["a"], p["b"] + 1):
                if i in cut:
                    run = None
                    continue
                t = toks[i]
                if run and run["c"] == t["c"] and abs(run["s1"] - t["t0"]) < 1e-3 and run["pid"] == p["id"]:
                    run["s1"] = t["t1"]
                    run["b"] = i
                else:
                    run = {"c": t["c"], "s0": t["t0"], "s1": t["t1"], "pid": p["id"], "a": i, "b": i}
                    segs.append(run)
        # merge across pieces when media is contiguous (keeps timeline tidy)
        merged = []
        for s in segs:
            m = merged[-1] if merged else None
            if m and m["c"] == s["c"] and abs(m["s1"] - s["s0"]) < 1e-3:
                m["s1"] = s["s1"]
                m["parts"].append({"pid": s["pid"], "a": s["a"], "b": s["b"], "s0": s["s0"]})
            else:
                merged.append({"c": s["c"], "s0": s["s0"], "s1": s["s1"],
                               "parts": [{"pid": s["pid"], "a": s["a"], "b": s["b"], "s0": s["s0"]}]})
        pos = 0.0
        for m in merged:
            m["r0"] = round(pos, 4)
            m["dur"] = round(m["s1"] - m["s0"], 4)
            pos += m["dur"]
        return merged

    def render_frames(self, segments=None):
        """Segments converted to media frames + edit-record frames."""
        segments = segments if segments is not None else self.render()
        clips = self.data["clips"]
        tfps = self.data["resolve"].get("fps") or 25.0
        out = []
        rec = 0
        for s in segments:
            clip = clips[s["c"]]
            mfps = clip.get("media_fps") or tfps
            f0 = int(round(s["s0"] * mfps))
            f1 = int(round(s["s1"] * mfps))
            if f1 <= f0:
                continue
            dur_rec = int(round((f1 - f0) * tfps / mfps))
            out.append({"media_id": clip["media_id"], "path": clip["path"], "start": f0, "end": f1,
                        "rec": rec, "rec_dur": dur_rec, "parts": s["parts"]})
            rec += dur_rec
        return out

    def edit_markers(self, frames):
        """Timeline markers for scene starts and voice-over notes."""
        markers = []
        if not frames:
            return markers
        tfps = self.data["resolve"].get("fps") or 25.0
        story = self.linked_story()
        scenes = {s["id"]: s for s in (story or {}).get("scenes", [])}
        seen_scene = set()
        used = set()

        def put(frame, color, name, note, duration=1):
            while frame in used:
                frame += 1
            used.add(frame)
            markers.append({"frame": frame, "color": color, "name": name, "note": note, "duration": duration})

        # locate each piece's first record frame
        piece_rec = {}
        for f in frames:
            mfps_ratio = (f["rec_dur"] / max(1, f["end"] - f["start"])) if f["end"] > f["start"] else 1
            for part in f["parts"]:
                if part["pid"] not in piece_rec:
                    clip = self.data["clips"][self.tokens[part["a"]]["c"]]
                    mfps = clip.get("media_fps") or tfps
                    off = int(round((part["s0"] * mfps - f["start"]) * mfps_ratio))
                    piece_rec[part["pid"]] = f["rec"] + max(0, off)
        for p in self.pieces:
            if p["off"] or p["id"] not in piece_rec:
                continue
            frame = piece_rec[p["id"]]
            sc = scenes.get(p.get("scene"))
            if sc and sc["id"] not in seen_scene:
                seen_scene.add(sc["id"])
                note = sc.get("description", "")
                if sc.get("edit_notes"):
                    note += "\nМонтаж: " + sc["edit_notes"]
                if sc.get("techniques"):
                    note += "\nПрийоми: " + ", ".join(sc["techniques"])
                put(frame, ROLE_COLORS.get(sc.get("role", "other"), "Sand"),
                    "%s — %s" % (sc.get("role", "").upper(), sc.get("title", "")), note.strip())
                for vo in sc.get("voiceover", []) or []:
                    put(frame + 1, "Yellow", "VO", vo.get("text", ""))
            elif p.get("label") and not sc:
                put(frame, "Blue", p["label"], "")
        return markers

    def edit_duration(self):
        return sum(s["dur"] for s in self.render())

    # ------------------------------------------------------------ stories
    def find_variant(self, analysis_id, variant_id):
        for an in self.data["analyses"]:
            if an["id"] == analysis_id:
                for v in an["variants"]:
                    if v["id"] == variant_id:
                        return an, v
        raise KeyError("варіант не знайдено")

    def linked_story(self):
        link = self.data["edit"].get("story_link")
        if not link:
            return None
        try:
            return self.find_variant(link["analysis"], link["variant"])[1]
        except KeyError:
            return None

    def story_pieces(self, variant, previous=None):
        """Build pieces for every scene range of a story variant.  Cuts made
        by the user inside a scene are carried over from ``previous``."""
        prev_cuts = {}
        for p in previous or []:
            if p.get("scene"):
                prev_cuts.setdefault(p["scene"], set()).update(p["cut"])
        pieces = []
        for sc in variant.get("scenes", []):
            if sc.get("disabled"):
                continue
            for rg in sc.get("ranges", []):
                idx = self.tokens_in_range(float(rg["start"]), float(rg["end"]))
                if not idx:
                    continue
                a, b = idx[0], idx[-1]
                pieces.append({"id": new_id("p"), "a": a, "b": b,
                               "cut": sorted(i for i in prev_cuts.get(sc["id"], ()) if a <= i <= b),
                               "off": False, "label": sc.get("title", ""), "scene": sc["id"],
                               "role": sc.get("role", "")})
        return pieces

    def apply_story(self, analysis_id, variant_id):
        _, v = self.find_variant(analysis_id, variant_id)
        self.checkpoint("apply_story")
        self.data["edit"]["pieces"] = self.story_pieces(v)
        self.data["edit"]["story_link"] = {"analysis": analysis_id, "variant": variant_id}
        self.touch()
        return len(self.pieces)

    def reapply_linked(self):
        story = self.linked_story()
        if story is None:
            return False
        self.data["edit"]["pieces"] = self.story_pieces(story, self.pieces)
        self.touch()
        return True

    def story_op(self, analysis_id, variant_id, action, **kw):
        """Edit the structure of a story variant."""
        an, v = self.find_variant(analysis_id, variant_id)
        self.checkpoint("story_" + action)
        scenes = v["scenes"]
        if action == "reorder":
            order = kw["order"]
            by_id = {s["id"]: s for s in scenes}
            v["scenes"] = [by_id[i] for i in order if i in by_id] + [s for s in scenes if s["id"] not in order]
        elif action == "delete_scene":
            v["scenes"] = [s for s in scenes if s["id"] != kw["scene"]]
        elif action == "update_scene":
            sc = next(s for s in scenes if s["id"] == kw["scene"])
            for k in ("title", "description", "role", "edit_notes", "techniques", "voiceover", "ranges", "disabled"):
                if k in kw.get("fields", {}):
                    val = kw["fields"][k]
                    if k == "ranges":
                        val = [{"start": parse_ts(r["start"]), "end": parse_ts(r["end"])} for r in val]
                        val = [r for r in val if r["end"] > r["start"]]
                    sc[k] = val
        elif action == "add_scene":
            sc = {"id": new_id("s"), "role": kw.get("role", "other"), "title": kw.get("title", "Нова сцена"),
                  "description": "", "ranges": [{"start": parse_ts(r["start"]), "end": parse_ts(r["end"])}
                                                for r in kw.get("ranges", [])],
                  "edit_notes": "", "voiceover": [], "techniques": []}
            pos = kw.get("index")
            v["scenes"].insert(len(v["scenes"]) if pos is None else int(pos), sc)
        elif action == "update_variant":
            for k in ("title", "logline"):
                if k in kw.get("fields", {}):
                    v[k] = kw["fields"][k]
        elif action == "delete_variant":
            an["variants"] = [x for x in an["variants"] if x["id"] != variant_id]
            link = self.data["edit"].get("story_link")
            if link and link["variant"] == variant_id:
                self.data["edit"]["story_link"] = None
            self.touch(edit_changed=False)
            return {"relinked": False}
        else:
            raise ValueError("unknown story action %r" % action)
        relinked = False
        link = self.data["edit"].get("story_link")
        if link and link["variant"] == variant_id:
            relinked = self.reapply_linked()
        self.touch(edit_changed=relinked)
        return {"relinked": relinked}

    # ------------------------------------------------------------ summaries
    def edit_summary_for_ai(self, max_pieces=400):
        """Compact description of the current edit (for Claude)."""
        toks = self.tokens
        lines = []
        for n, p in enumerate(self.pieces[:max_pieces]):
            if p["off"]:
                continue
            cut = set(p["cut"])
            kept = [i for i in range(p["a"], p["b"] + 1) if i not in cut]
            if not kept:
                continue
            text = " ".join(toks[i]["w"] for i in kept if toks[i]["k"] == "w")
            if len(text) > 160:
                text = text[:157] + "…"
            lines.append("%d. [%s–%s]%s %s%s" % (
                n + 1, fmt_ts(toks[p["a"]]["r0"]), fmt_ts(toks[p["b"]]["r1"]),
                " (%s)" % p["label"] if p.get("label") else "",
                text or "[без мови / геймплей]",
                " (вирізано %d ток.)" % len(cut & set(range(p["a"], p["b"] + 1))) if cut else ""))
        dur = self.edit_duration()
        head = "Поточний монтаж: %d фрагментів, тривалість %s." % (len(lines), fmt_ts(dur))
        return head + "\n" + "\n".join(lines)
