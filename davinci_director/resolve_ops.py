"""Operations on the DaVinci Resolve scripting API.

This module must stay **stdlib-only**: it is imported both by the add-on
application and by the tiny launcher/bridge that runs inside Resolve's own
embedded Python interpreter (Workspace > Scripts).

Only functions documented in Resolve's scripting README
(Developer/Scripting/README.txt) are used:

  Resolve.GetProjectManager, Resolve.OpenPage, Resolve.GetCurrentPage,
  Resolve.GetProductName, Resolve.GetVersionString
  ProjectManager.GetCurrentProject
  Project.GetName, GetMediaPool, GetCurrentTimeline, SetCurrentTimeline,
          GetTimelineCount, GetTimelineByIndex, GetSetting
  MediaPool.CreateEmptyTimeline, AppendToTimeline, DeleteTimelines
  Timeline.GetName, GetUniqueId, GetStartFrame, GetEndFrame, GetSetting,
           GetTrackCount, GetItemListInTrack, AddMarker,
           GetCurrentTimecode, SetCurrentTimecode
  TimelineItem.GetName, GetStart, GetEnd, GetDuration, GetLeftOffset,
               GetMediaPoolItem
  MediaPoolItem.GetName, GetClipProperty, GetUniqueId

Every public function takes the ``resolve`` object first and returns plain
JSON-serialisable data, so the same calls can be proxied over the bridge.
"""

import math

MARKER_COLORS = [
    "Blue", "Cyan", "Green", "Yellow", "Red", "Pink", "Purple", "Fuchsia",
    "Rose", "Lavender", "Sky", "Mint", "Lemon", "Sand", "Cocoa", "Cream",
]


class ResolveError(Exception):
    pass


# ---------------------------------------------------------------- helpers

def _project(resolve):
    pm = resolve.GetProjectManager()
    project = pm.GetCurrentProject() if pm else None
    if not project:
        raise ResolveError("Немає відкритого проєкту в DaVinci Resolve.")
    return project


def _fps_value(raw, default=25.0):
    try:
        v = float(str(raw).replace(" DF", "").strip())
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def _timeline_fps(timeline, project):
    raw = None
    try:
        raw = timeline.GetSetting("timelineFrameRate")
    except Exception:
        raw = None
    if not raw:
        raw = project.GetSetting("timelineFrameRate")
    return _fps_value(raw)


def _drop_frame(timeline, project):
    for obj in (timeline, project):
        try:
            v = obj.GetSetting("timelineDropFrameTimecode")
            if v not in (None, ""):
                return str(v) in ("1", "true", "True")
        except Exception:
            pass
    return False


def _timeline_by_id(project, timeline_id):
    if not timeline_id:
        return None
    count = project.GetTimelineCount() or 0
    for idx in range(1, int(count) + 1):
        tl = project.GetTimelineByIndex(idx)
        if tl and tl.GetUniqueId() == timeline_id:
            return tl
    return None


def _all_media_pool_items(media_pool):
    """Yield all MediaPoolItems from every folder of the media pool."""
    root = media_pool.GetRootFolder()
    stack = [root] if root else []
    while stack:
        folder = stack.pop()
        for clip in folder.GetClipList() or []:
            yield clip
        stack.extend(folder.GetSubFolderList() or [])


def frames_to_timecode(frame, fps, drop_frame=False):
    """Absolute frame number -> 'HH:MM:SS:FF' (';' separator for DF)."""
    frame = int(round(frame))
    nominal = int(round(fps))
    if drop_frame and nominal in (30, 60):
        drop = 2 if nominal == 30 else 4
        frames_per_10min = nominal * 600 - drop * 9
        frames_per_min = nominal * 60 - drop
        d, m = divmod(frame, frames_per_10min)
        if m > drop:
            frame += drop * 9 * d + drop * ((m - drop) // frames_per_min)
        else:
            frame += drop * 9 * d
        sep = ";"
    else:
        sep = ":"
    ff = frame % nominal
    total_s = frame // nominal
    return "%02d:%02d:%02d%s%02d" % (total_s // 3600, (total_s // 60) % 60, total_s % 60, sep, ff)


def timecode_to_frames(tc, fps, drop_frame=False):
    parts = tc.replace(";", ":").split(":")
    hh, mm, ss, ff = (int(p) for p in parts)
    nominal = int(round(fps))
    frames = ((hh * 3600 + mm * 60 + ss) * nominal) + ff
    if drop_frame and nominal in (30, 60):
        drop = 2 if nominal == 30 else 4
        total_min = hh * 60 + mm
        frames -= drop * (total_min - total_min // 10)
    return frames


# ---------------------------------------------------------------- reading

def get_info(resolve):
    info = {"product": None, "version": None, "project": None, "timeline": None, "page": None}
    try:
        info["product"] = resolve.GetProductName()
        info["version"] = resolve.GetVersionString()
        info["page"] = resolve.GetCurrentPage()
    except Exception:
        pass
    try:
        project = _project(resolve)
        info["project"] = project.GetName()
        tl = project.GetCurrentTimeline()
        if tl:
            info["timeline"] = {"name": tl.GetName(), "id": tl.GetUniqueId()}
    except ResolveError:
        pass
    return info


def read_timeline(resolve, timeline_id=None):
    """Describe the current (or given) timeline: settings and every clip on
    the video tracks (or audio tracks if there is no video) with the source
    media path and source in/out frames.
    """
    project = _project(resolve)
    tl = _timeline_by_id(project, timeline_id) if timeline_id else project.GetCurrentTimeline()
    if not tl:
        raise ResolveError("Немає активного таймлайну. Додайте відео на таймлайн і відкрийте його.")
    fps = _timeline_fps(tl, project)
    start_frame = int(tl.GetStartFrame())
    result = {
        "project": project.GetName(),
        "timeline": tl.GetName(),
        "timeline_id": tl.GetUniqueId(),
        "fps": fps,
        "drop_frame": _drop_frame(tl, project),
        "start_frame": start_frame,
        "end_frame": int(tl.GetEndFrame()),
        "clips": [],
    }
    seen = {}
    for track_type in ("video", "audio"):
        count = int(tl.GetTrackCount(track_type) or 0)
        for idx in range(1, count + 1):
            for item in tl.GetItemListInTrack(track_type, idx) or []:
                mpi = item.GetMediaPoolItem()
                if not mpi:
                    continue  # titles, generators, compound clips without media
                path = mpi.GetClipProperty("File Path") or ""
                if not path:
                    continue
                rec_start = int(item.GetStart()) - start_frame
                rec_end = int(item.GetEnd()) - start_frame
                key = (path, rec_start)
                if key in seen:
                    continue  # linked audio of a video clip already listed
                left = int(item.GetLeftOffset() or 0)
                clip_fps = _fps_value(mpi.GetClipProperty("FPS"), fps)
                seen[key] = True
                result["clips"].append({
                    "name": item.GetName(),
                    "track_type": track_type,
                    "track": idx,
                    "path": path,
                    "media_id": mpi.GetUniqueId(),
                    "media_name": mpi.GetName(),
                    "media_fps": clip_fps,
                    "start_tc": mpi.GetClipProperty("Start TC") or "00:00:00:00",
                    "rec_start": rec_start,
                    "rec_end": rec_end,
                    "src_in": left,
                    "src_out": left + int(round((rec_end - rec_start) * clip_fps / fps)),
                })
        if result["clips"] and track_type == "video":
            break  # video defines the edit; audio-only timelines fall through
    result["clips"].sort(key=lambda c: c["rec_start"])
    return result


# ---------------------------------------------------------------- navigation

def current_timeline_id(resolve):
    tl = _project(resolve).GetCurrentTimeline()
    return tl.GetUniqueId() if tl else None


def jump(resolve, timeline_id, frame_offset, open_timeline=False):
    """Move the playhead of ``timeline_id`` to ``frame_offset`` frames from
    the timeline start.  Returns the timecode or raises ResolveError."""
    project = _project(resolve)
    tl = _timeline_by_id(project, timeline_id)
    if not tl:
        raise ResolveError("Таймлайн не знайдено в проєкті.")
    current = project.GetCurrentTimeline()
    if not current or current.GetUniqueId() != timeline_id:
        if not open_timeline:
            raise ResolveError("not-current")
        project.SetCurrentTimeline(tl)
    page = resolve.GetCurrentPage()
    if page not in ("edit", "cut", "color", "fairlight"):
        resolve.OpenPage("edit")
    fps = _timeline_fps(tl, project)
    tc = frames_to_timecode(int(tl.GetStartFrame()) + int(frame_offset), fps, _drop_frame(tl, project))
    if not tl.SetCurrentTimecode(tc):
        raise ResolveError("Resolve не прийняв таймкод %s" % tc)
    return tc


def playhead(resolve):
    """Current playhead position of the current timeline as frame offset."""
    project = _project(resolve)
    tl = project.GetCurrentTimeline()
    if not tl:
        return None
    tc = tl.GetCurrentTimecode()
    fps = _timeline_fps(tl, project)
    frames = timecode_to_frames(tc, fps, _drop_frame(tl, project))
    return {"timeline_id": tl.GetUniqueId(), "frame": frames - int(tl.GetStartFrame()), "timecode": tc}


# ---------------------------------------------------------------- building

def build_timeline(resolve, name, segments, markers=None, replace_id=None, end_inclusive=True):
    """Create a timeline ``name`` from ``segments`` and return its id.

    segments: [{"media_id": str, "path": str, "start": int, "end": int}]
              start/end are source frames (end exclusive) in the media's own
              frame numbering starting at 0.
    markers:  [{"frame": int, "color": str, "name": str, "note": str,
                "duration": int}]  frame relative to timeline start.
    replace_id: id of a previously generated timeline to delete.
    end_inclusive: AppendToTimeline treats endFrame as inclusive.
    """
    project = _project(resolve)
    media_pool = project.GetMediaPool()
    items = {}
    wanted = {s["media_id"] for s in segments}
    for clip in _all_media_pool_items(media_pool):
        uid = clip.GetUniqueId()
        if uid in wanted:
            items[uid] = clip
    missing = wanted - set(items)
    if missing:
        # fall back to file path lookup (media may have been re-imported)
        by_path = {}
        for clip in _all_media_pool_items(media_pool):
            by_path[clip.GetClipProperty("File Path")] = clip
        for s in segments:
            if s["media_id"] in missing and s.get("path") in by_path:
                items[s["media_id"]] = by_path[s["path"]]
        missing = wanted - set(items)
    if missing:
        raise ResolveError("Медіа не знайдено в Media Pool: %d кліп(ів)." % len(missing))

    old = _timeline_by_id(project, replace_id) if replace_id else None
    new_name = name
    existing = set()
    for idx in range(1, int(project.GetTimelineCount() or 0) + 1):
        tl = project.GetTimelineByIndex(idx)
        if tl and (not old or tl.GetUniqueId() != old.GetUniqueId()):
            existing.add(tl.GetName())
    n = 2
    while new_name in existing:
        new_name = "%s (%d)" % (name, n)
        n += 1
    if old:
        # rename the old one so the new can take its name, delete afterwards
        try:
            old.SetName(new_name + " (old)")
        except Exception:
            pass

    timeline = media_pool.CreateEmptyTimeline(new_name)
    if not timeline:
        raise ResolveError("Не вдалося створити таймлайн «%s»." % new_name)
    project.SetCurrentTimeline(timeline)

    clip_infos = []
    for s in segments:
        if s["end"] <= s["start"]:
            continue
        clip_infos.append({
            "mediaPoolItem": items[s["media_id"]],
            "startFrame": int(s["start"]),
            "endFrame": int(s["end"]) - (1 if end_inclusive else 0),
        })
    appended = []
    # Append in chunks: huge single calls can be slow/unstable in Resolve.
    for i in range(0, len(clip_infos), 200):
        res = media_pool.AppendToTimeline(clip_infos[i:i + 200]) or []
        appended.extend(res)
    if clip_infos and not appended:
        raise ResolveError("AppendToTimeline не додав жодного кліпу.")

    measured = None
    if appended:
        try:
            measured = int(appended[0].GetDuration())
        except Exception:
            measured = None

    marker_count = 0
    for m in markers or []:
        try:
            ok = timeline.AddMarker(int(m["frame"]), m.get("color", "Blue"), m.get("name", "")[:200],
                                    m.get("note", ""), max(1, int(m.get("duration", 1))), "")
            marker_count += 1 if ok else 0
        except Exception:
            pass

    if old:
        media_pool.DeleteTimelines([old])
        try:
            if timeline.GetName() != name:
                timeline.SetName(name)
        except Exception:
            pass

    expected = (clip_infos[0]["endFrame"] - clip_infos[0]["startFrame"] + (1 if end_inclusive else 0)) if clip_infos else None
    return {
        "timeline_id": timeline.GetUniqueId(),
        "timeline": timeline.GetName(),
        "clips": len(appended),
        "markers": marker_count,
        "first_duration": measured,
        "first_expected": expected,
    }


OPERATIONS = {
    "get_info": get_info,
    "read_timeline": read_timeline,
    "current_timeline_id": current_timeline_id,
    "jump": jump,
    "playhead": playhead,
    "build_timeline": build_timeline,
}


def call(resolve, op, kwargs):
    fn = OPERATIONS.get(op)
    if not fn:
        raise ResolveError("Unknown operation %r" % op)
    return fn(resolve, **(kwargs or {}))


def ceil_frame(seconds, fps):
    return int(math.ceil(seconds * fps - 1e-6))


def floor_frame(seconds, fps):
    return int(math.floor(seconds * fps + 1e-6))
