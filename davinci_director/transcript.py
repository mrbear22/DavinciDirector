"""Words of a source media file: times in seconds from the file's start, fragments with stable ids,
speakers.  Stored once per file (data\\transcripts), so every Resolve project can use them."""

import bisect
import difflib
import hashlib
import json
import os
import re

from .link import norm_path
from .storage import atomic_write, base_dir


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


def respell(old, new):
    """Words for a corrected phrase: words the user kept keep their exact timing."""
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
