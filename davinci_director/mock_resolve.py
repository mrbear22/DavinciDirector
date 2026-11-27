"""In-memory stand-in for the DaVinci Resolve scripting API (tests + demo).

Reproduces the behaviour measured on DaVinci Resolve 21.2:
  * Get/SetLeftOffset, Get/SetRightOffset are in timeline frames; trimming the
    head moves the start (no ripple);
  * AppendToTimeline startFrame/endFrame are source frames, endFrame exclusive,
    recordFrame is an absolute timeline frame; video + linked audio are added;
  * DeleteClips removes only the given items (linked audio stays);
  * MoveItems moves the leftmost item to startFrame and the rest relative to it,
    linked items follow, a group may move into space it occupied itself;
  * Timeline.Export(EXPORT_OTIO) writes an OTIO JSON file.
"""

import itertools
import json
import os
import uuid

_ids = itertools.count(1)


def _uid():
    return str(uuid.UUID(int=next(_ids)))


class MockMediaPoolItem:
    def __init__(self, path, fps=25.0, frames=1500, has_audio=True, start_tc="00:00:00:00"):
        self.path, self.fps, self.frames, self.has_audio, self.start_tc = path, float(fps), int(frames), has_audio, start_tc
        self.uid = _uid()

    def GetUniqueId(self):
        return self.uid

    def GetName(self):
        return os.path.basename(self.path)

    def GetClipProperty(self, key=None):
        props = {"File Path": self.path, "FPS": self.fps, "Frames": str(self.frames), "Start TC": self.start_tc,
                 "Clip Name": self.GetName()}
        return props if key is None else props.get(key, "")


class MockTimelineItem:
    def __init__(self, tl, ttype, tindex, mpi, start, end, sf0):
        self.tl, self.ttype, self.tindex, self.mpi = tl, ttype, tindex, mpi
        self.start, self.end, self.sf0 = int(start), int(end), float(sf0)
        self.uid = _uid()
        self.linked = []

    # geometry helpers (timeline frames <-> source frames)
    def _ratio(self):
        return self.mpi.fps / self.tl.fps

    def GetUniqueId(self):
        return self.uid

    def GetName(self):
        return self.mpi.GetName()

    def GetType(self):
        return self.ttype

    def GetStart(self, subframe=False):
        return self.start

    def GetEnd(self, subframe=False):
        return self.end

    def GetDuration(self, subframe=False):
        return self.end - self.start

    def GetMediaPoolItem(self):
        return self.mpi

    def GetSourceStartFrame(self):
        return int(round(self.sf0))

    def GetSourceEndFrame(self):
        return int(round(self.sf0 + (self.end - self.start) * self._ratio())) - 1

    def GetSourceStartTime(self):
        return self.sf0 / self.mpi.fps

    def GetLeftOffset(self, subframe=False):
        return int(round(self.sf0 / self._ratio()))

    def GetRightOffset(self, subframe=False):
        used_end = self.sf0 + (self.end - self.start) * self._ratio()
        return int(round((self.mpi.frames - used_end) / self._ratio()))

    def GetLinkedItems(self):
        return list(self.linked)

    def GetTrackTypeAndIndex(self):
        return [self.ttype, self.tindex]

    def _group(self, linked=True):
        return [self] + (self.linked if linked else [])

    def SetRightOffset(self, offset, subframe=False, update_linked=True):
        delta = int(round(offset)) - self.GetRightOffset()  # >0 shortens
        if offset < 0 or self.end - delta <= self.start:
            return False
        for it in self._group(update_linked):
            if self.tl._collides(it, it.start, it.end - delta, ignore=self._group(update_linked)):
                return False
        for it in self._group(update_linked):
            it.end -= delta
        return True

    def SetLeftOffset(self, offset, subframe=False, update_linked=True):
        delta = int(round(offset)) - self.GetLeftOffset()  # >0 trims the head
        if offset < 0 or self.start + delta >= self.end:
            return False
        for it in self._group(update_linked):
            if self.tl._collides(it, it.start + delta, it.end, ignore=self._group(update_linked)):
                return False
        for it in self._group(update_linked):
            it.start += delta
            it.sf0 += delta * it._ratio()
        return True

    def GetPosition(self, subframe=False):
        return {"trackType": self.ttype, "trackIndex": self.tindex, "startFrame": self.start}


class MockTimeline:
    def __init__(self, project, name, fps=25.0, start=None):
        self.project, self.name, self.fps = project, name, float(fps)
        self.start_frame = int(start) if start is not None else 3600 * int(round(self.fps))  # 01:00:00:00
        self.items = []
        self.markers = {}
        self.playhead = self.start_frame
        self.uid = _uid()
        self.export_ok = True
        self.calls = []

    def GetName(self):
        return self.name

    def SetName(self, name):
        self.name = name
        return True

    def GetUniqueId(self):
        return self.uid

    def GetStartFrame(self):
        return self.start_frame

    def GetEndFrame(self):
        return max([it.end for it in self.items] + [self.start_frame])

    def GetSetting(self, key=None):
        if key == "timelineFrameRate":
            return self.fps
        return {"timelineFrameRate": self.fps}

    def GetTrackCount(self, ttype):
        idx = [it.tindex for it in self.items if it.ttype == ttype]
        return max(idx + [1])

    def GetItemListInTrack(self, ttype, index):
        return sorted((it for it in self.items if it.ttype == ttype and it.tindex == index), key=lambda x: x.start)

    def GetCurrentTimecode(self):
        from .link import frame_to_tc
        return frame_to_tc(self.playhead, self.fps)

    def SetCurrentTimecode(self, tc):
        from .link import tc_to_frame
        f = tc_to_frame(tc, self.fps)
        if f is None:
            return False
        self.playhead = f
        return True

    def _collides(self, item, start, end, ignore=()):
        for other in self.items:
            if other is item or other in ignore or other.ttype != item.ttype or other.tindex != item.tindex:
                continue
            if start < other.end and other.start < end:
                return True
        return False

    def DeleteClips(self, items, ripple=False):
        self.calls.append(("DeleteClips", len(items)))
        for it in items:
            if it in self.items:
                self.items.remove(it)
                for other in it.linked:
                    if it in other.linked:
                        other.linked.remove(it)
        return True

    def MoveItems(self, items, position, move_linked=True, subframe=False):
        self.calls.append(("MoveItems", len(items)))
        items = list(items)
        if not items:
            return False
        leftmost = min(items, key=lambda x: (x.start, 0 if x.ttype == "video" else 1))
        delta = int(round(position["startFrame"])) - leftmost.start
        group = []
        for it in items:
            for x in ([it] + (it.linked if move_linked else [])):
                if x not in group:
                    group.append(x)
        for it in group:
            if it.start + delta < 0 or self._collides(it, it.start + delta, it.end + delta, ignore=group):
                return False
        for it in group:
            it.start += delta
            it.end += delta
        return True

    def SetMarkInOut(self, mark_in, mark_out, mark_type=None):
        self.marks = {"video": {"in": int(mark_in), "out": int(mark_out)}, "audio": {"in": int(mark_in), "out": int(mark_out)}}
        return True

    def GetMarkInOut(self):
        return getattr(self, "marks", {})

    def AddMarker(self, frame, color, name, note, duration, custom=""):
        if frame in self.markers:
            return False
        self.markers[int(frame)] = {"color": color, "name": name, "note": note, "duration": duration, "customData": custom}
        return True

    def GetMarkers(self):
        return dict(self.markers)

    def DeleteMarkersByColor(self, color):
        self.markers = {f: m for f, m in self.markers.items() if color != "All" and m["color"] != color}
        return True

    def DeleteMarkerByCustomData(self, data):
        before = len(self.markers)
        self.markers = {f: m for f, m in self.markers.items() if m.get("customData") != data}
        return len(self.markers) != before

    def DuplicateTimeline(self, name):
        tl = MockTimeline(self.project, name, self.fps, self.start_frame)
        mapping = {}
        for it in self.items:
            c = MockTimelineItem(tl, it.ttype, it.tindex, it.mpi, it.start, it.end, it.sf0)
            mapping[it] = c
            tl.items.append(c)
        for it, c in mapping.items():
            c.linked = [mapping[x] for x in it.linked if x in mapping]
        self.project.timelines.append(tl)
        return tl

    def Export(self, path, export_type, subtype=None):
        if not self.export_ok:
            return False
        if export_type == MockResolve.EXPORT_EDL:
            with open(path, "w", encoding="utf-8") as f:
                f.write("TITLE: %s\nFCM: NON-DROP FRAME\n" % self.name)
            return True
        if export_type != MockResolve.EXPORT_OTIO:
            return False
        tracks = []
        for ttype, kind in (("video", "Video"), ("audio", "Audio")):
            for ti in range(1, self.GetTrackCount(ttype) + 1):
                children, pos = [], self.start_frame
                for it in self.GetItemListInTrack(ttype, ti):
                    if it.start > pos:
                        children.append({"OTIO_SCHEMA": "Gap.1", "source_range": {"OTIO_SCHEMA": "TimeRange.1",
                                         "duration": {"OTIO_SCHEMA": "RationalTime.1", "rate": self.fps, "value": float(it.start - pos)},
                                         "start_time": {"OTIO_SCHEMA": "RationalTime.1", "rate": self.fps, "value": 0.0}}})
                    from .link import tc_to_frame
                    tc0 = tc_to_frame(it.mpi.start_tc, it.mpi.fps) or 0
                    children.append({
                        "OTIO_SCHEMA": "Clip.2", "name": it.GetName(), "enabled": True,
                        "source_range": {"OTIO_SCHEMA": "TimeRange.1",
                                         "duration": {"OTIO_SCHEMA": "RationalTime.1", "rate": self.fps, "value": float(it.end - it.start)},
                                         # like Resolve: source times expressed in the timeline rate
                                         "start_time": {"OTIO_SCHEMA": "RationalTime.1", "rate": self.fps,
                                                        "value": (tc0 + it.sf0) * self.fps / it.mpi.fps}},
                        "media_references": {"DEFAULT_MEDIA": {
                            "OTIO_SCHEMA": "ExternalReference.1", "target_url": it.mpi.path,
                            "available_range": {"OTIO_SCHEMA": "TimeRange.1",
                                                "duration": {"OTIO_SCHEMA": "RationalTime.1", "rate": self.fps, "value": it.mpi.frames * self.fps / it.mpi.fps},
                                                "start_time": {"OTIO_SCHEMA": "RationalTime.1", "rate": self.fps, "value": tc0 * self.fps / it.mpi.fps}}}},
                        "active_media_reference_key": "DEFAULT_MEDIA"})
                    pos = it.end
                tracks.append({"OTIO_SCHEMA": "Track.1", "kind": kind, "name": "%s %d" % (kind, ti), "children": children})
        doc = {"OTIO_SCHEMA": "Timeline.1", "name": self.name,
               "global_start_time": {"OTIO_SCHEMA": "RationalTime.1", "rate": self.fps, "value": float(self.start_frame)},
               "tracks": {"OTIO_SCHEMA": "Stack.1", "children": tracks}}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        return True


class MockFolder:
    def __init__(self, name):
        self.name, self.clips, self.subs = name, [], []

    def GetClipList(self):
        return list(self.clips)

    def GetSubFolderList(self):
        return list(self.subs)

    def GetName(self):
        return self.name


class MockMediaPool:
    def __init__(self, project):
        self.project = project
        self.root = MockFolder("Master")

    def GetRootFolder(self):
        return self.root

    def ImportMedia(self, paths, **kw):
        out = []
        for p in paths:
            mpi = self.project.app.media.get(p) or MockMediaPoolItem(p)
            self.root.clips.append(mpi)
            out.append(mpi)
        return out

    def ExportMetadata(self, path, clips=None):
        if not clips:
            return False  # like Resolve 21.2: nothing to export
        path = os.path.splitext(path)[0] + ".csv"  # Resolve 21.2 forces the extension
        with open(path, "w", encoding="utf-8") as f:
            f.write("File Name,Clip Directory\n" + "\n".join("%s,%s" % (c.GetName(), os.path.dirname(c.path)) for c in clips))
        return True

    def CreateEmptyTimeline(self, name):
        if any(t.name == name for t in self.project.timelines):
            return None
        tl = MockTimeline(self.project, name, self.project.fps)
        self.project.timelines.append(tl)
        return tl

    def AppendToTimeline(self, infos):
        tl = self.project.current
        if tl is None:
            return []
        made = []
        for info in infos:
            mpi = info["mediaPoolItem"]
            s = float(info.get("startFrame", 0))
            e = float(info.get("endFrame", mpi.frames))
            ti = int(info.get("trackIndex", 1))
            dur = int(round((e - s) * tl.fps / mpi.fps))
            if dur <= 0 or e > mpi.frames or s < 0:
                return made
            if "recordFrame" in info:
                rec = int(round(info["recordFrame"]))
            else:
                rec = max([it.end for it in tl.items if it.ttype == "video" and it.tindex == ti] + [tl.start_frame])
            mtype = int(info.get("mediaType", 0) or 0)
            kinds = []
            if mtype in (0, 1):
                kinds.append("video")
            if mtype in (0, 2) and mpi.has_audio:
                kinds.append("audio")
            new = [MockTimelineItem(tl, k, ti, mpi, rec, rec + dur, s) for k in kinds]
            for it in new:
                if tl._collides(it, it.start, it.end):
                    return made
            for it in new:
                it.linked = [x for x in new if x is not it]
                tl.items.append(it)
            made.extend(new)
        return made


class MockProject:
    def __init__(self, app, name="Stream Project", fps=25.0):
        self.app, self.name, self.fps = app, name, float(fps)
        self.timelines = []
        self.current = None
        self.pool = MockMediaPool(self)

    def GetName(self):
        return self.name

    def GetMediaPool(self):
        return self.pool

    def GetCurrentTimeline(self):
        return self.current

    def SetCurrentTimeline(self, tl):
        if tl not in self.timelines:
            return False
        self.current = tl
        return True

    def GetTimelineCount(self):
        return len(self.timelines)

    def GetTimelineByIndex(self, i):
        return self.timelines[i - 1] if 1 <= i <= len(self.timelines) else None


class MockProjectManager:
    def __init__(self, project):
        self.project = project

    def GetCurrentProject(self):
        return self.project


class MockMediaStorage:
    def GetSubFolderList(self, path):
        try:
            return [os.path.join(path, n) for n in sorted(os.listdir(path)) if os.path.isdir(os.path.join(path, n))]
        except OSError:
            return []


class MockResolve:
    EXPORT_OTIO = 23
    EXPORT_EDL = 1
    EXPORT_NONE = 0

    def __init__(self, fps=25.0):
        self.media = {}
        self.project = MockProject(self, fps=fps)
        self.page = "edit"

    def GetProjectManager(self):
        return MockProjectManager(self.project)

    def GetMediaStorage(self):
        return MockMediaStorage()

    def GetProductName(self):
        return "DaVinci Resolve (mock)"

    def GetVersionString(self):
        return "21.2.0"

    def OpenPage(self, name):
        self.page = name
        return True

    # convenience for tests / demo
    def add_media(self, path, fps=25.0, frames=1500, has_audio=True, start_tc="00:00:00:00"):
        mpi = MockMediaPoolItem(path, fps, frames, has_audio, start_tc)
        self.media[path] = mpi
        self.project.pool.root.clips.append(mpi)
        return mpi

    def timeline_with(self, mpi, name="Stream", ranges=None):
        tl = self.project.pool.CreateEmptyTimeline(name)
        self.project.current = tl
        for s, e in ranges or [(0, mpi.frames)]:
            self.project.pool.AppendToTimeline([{"mediaPoolItem": mpi, "startFrame": s, "endFrame": e}])
        return tl
