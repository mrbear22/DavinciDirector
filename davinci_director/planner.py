"""Timeline operations for a text edit, planned against a simulated copy of the edit track."""

import os
import re

from .link import norm_path

PARK_GAP = 50  # frames between the end of the timeline and a block parked there while moving it


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

    def _move_items(self, group, to):
        """Move ``group`` (sorted by start) so that its first item starts at ``to``."""
        if not group:
            return
        off = to - group[0]["s"]
        self.ops.append({"op": "move", "refs": [self.ref(it) for it in group], "to": to})
        for it in group:
            it["s"] += off
            it["e"] += off

    def shift_from(self, f, delta, exclude=()):
        group = [it for it in self.items if it["s"] >= f and it not in exclude]
        if group and delta:
            self._move_items(group, group[0]["s"] + delta)
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
        # park the block after everything, close the hole by shifting the clips in between, put it back
        self._move_items(block, max([self.max_end] + [it["e"] for it in self.items]) + PARK_GAP)
        if fd > f2:
            mid, dest = [it for it in self.items if f2 <= it["s"] < fd and it not in block], fd - d
            self._move_items(mid, mid[0]["s"] - d if mid else 0)
        else:
            mid, dest = [it for it in self.items if fd <= it["s"] < f1 and it not in block], fd
            self._move_items(mid, mid[0]["s"] + d if mid else 0)
        self._move_items(block, dest)
        self._sort()
        return dest
