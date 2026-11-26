"""Connection between the add-on and DaVinci Resolve.

Three ways to reach Resolve:

* ``BridgeClient`` – default.  The launcher script (Workspace > Scripts >
  DaVinci Director) runs inside Resolve, where the ``resolve`` object is
  always available (free and Studio).  It starts ``serve_bridge`` – a tiny
  stdlib HTTP JSON-RPC server bound to 127.0.0.1 and protected by a random
  token – and spawns the add-on application in its own Python environment.
* ``DirectClient`` – the app was started from a terminal and Resolve allows
  external scripting (Preferences > System > General > External scripting
  using: Local).  Uses the official ``DaVinciResolveScript`` module.
* ``MockResolve`` – an in-memory imitation used for tests and demo mode.

This module is stdlib-only (the bridge half runs in Resolve's Python).
"""

import json
import os
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

try:
    from . import resolve_ops
except ImportError:  # imported by path from the launcher
    import resolve_ops  # type: ignore


class ResolveUnavailable(Exception):
    pass


# ------------------------------------------------------------------ server

def serve_bridge(resolve, port, token, should_stop=None, on_ready=None):
    """Serve resolve_ops over HTTP until ``should_stop()`` returns True or a
    /shutdown request arrives.  Runs requests on the calling thread, which
    keeps every Resolve API call on the script's main thread."""

    state = {"stop": False}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            if self.headers.get("X-DD-Token") != token:
                self.send_response(403)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/shutdown":
                state["stop"] = True
                payload = {"ok": True}
            else:
                try:
                    payload = {"ok": True, "result": resolve_ops.call(resolve, body.get("op"), body.get("args"))}
                except resolve_ops.ResolveError as e:
                    payload = {"ok": False, "error": str(e), "kind": "resolve"}
                except Exception as e:  # report API failures to the app
                    payload = {"ok": False, "error": "%s: %s" % (type(e).__name__, e), "kind": "internal"}
            data = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = HTTPServer(("127.0.0.1", port), Handler)
    server.timeout = 0.5
    if on_ready:
        on_ready(server.server_address[1])
    try:
        while not state["stop"]:
            server.handle_request()
            if should_stop and should_stop():
                break
    finally:
        server.server_close()


# ------------------------------------------------------------------ clients

class BaseClient:
    kind = "base"

    def call(self, op, **kwargs):
        raise NotImplementedError

    def available(self):
        try:
            self.call("get_info")
            return True
        except Exception:
            return False


class BridgeClient(BaseClient):
    kind = "bridge"

    def __init__(self, url, token, timeout=600):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._lock = threading.Lock()

    def call(self, op, **kwargs):
        req = urllib.request.Request(
            self.url + "/call", data=json.dumps({"op": op, "args": kwargs}).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-DD-Token": self.token}, method="POST")
        with self._lock:  # Resolve API is not re-entrant
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = json.loads(resp.read())
            except OSError as e:
                raise ResolveUnavailable("Міст до DaVinci Resolve недоступний (%s). Запустіть аддон з меню Workspace > Scripts." % e)
        if not payload.get("ok"):
            raise resolve_ops.ResolveError(payload.get("error", "unknown error"))
        return payload.get("result")


class DirectClient(BaseClient):
    kind = "direct"

    def __init__(self, resolve=None):
        self._resolve = resolve
        self._lock = threading.Lock()

    def _get(self):
        if self._resolve is None:
            self._resolve = connect_external()
        return self._resolve

    def call(self, op, **kwargs):
        with self._lock:
            return resolve_ops.call(self._get(), op, kwargs)


def connect_external():
    """Connect with the official DaVinciResolveScript module."""
    try:
        import DaVinciResolveScript as dvr  # noqa: N813
    except ImportError:
        candidates = []
        if os.environ.get("RESOLVE_SCRIPT_API"):
            candidates.append(os.path.join(os.environ["RESOLVE_SCRIPT_API"], "Modules"))
        if sys.platform.startswith("win"):
            candidates.append(os.path.expandvars(r"%PROGRAMDATA%\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting\Modules"))
        elif sys.platform == "darwin":
            candidates.append("/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules")
        else:
            candidates += ["/opt/resolve/Developer/Scripting/Modules", "/home/resolve/Developer/Scripting/Modules"]
        for c in candidates:
            if os.path.isdir(c) and c not in sys.path:
                sys.path.append(c)
        try:
            import DaVinciResolveScript as dvr  # noqa: N813
        except ImportError as e:
            raise ResolveUnavailable("Модуль DaVinciResolveScript не знайдено: %s" % e)
    resolve = dvr.scriptapp("Resolve")
    if not resolve:
        raise ResolveUnavailable(
            "Не вдалося підключитися до Resolve. Запустіть аддон з меню Workspace > Scripts "
            "або увімкніть Preferences > System > General > External scripting using: Local.")
    return resolve


# ------------------------------------------------------------------ mock

class _MockMPI:
    def __init__(self, uid, path, fps, frames):
        self.uid, self.path, self.fps, self.frames = uid, path, fps, frames

    def GetUniqueId(self): return self.uid
    def GetName(self): return os.path.basename(self.path)

    def GetClipProperty(self, key=None):
        props = {"File Path": self.path, "FPS": str(self.fps), "Frames": str(self.frames)}
        return props.get(key, "") if key else props


class _MockItem:
    def __init__(self, mpi, start, end, left):
        self.mpi, self.start, self.end, self.left = mpi, start, end, left

    def GetName(self): return self.mpi.GetName()
    def GetStart(self): return self.start
    def GetEnd(self): return self.end
    def GetDuration(self): return self.end - self.start
    def GetLeftOffset(self): return self.left
    def GetMediaPoolItem(self): return self.mpi


class _MockTimeline:
    _n = 0

    def __init__(self, name, fps, start_frame=None):
        _MockTimeline._n += 1
        self.uid = "mock-tl-%d" % _MockTimeline._n
        self.name, self.fps = name, fps
        self.start_frame = int(round(3600 * fps)) if start_frame is None else start_frame
        self.items = []
        self.markers = {}
        self.tc_frame = start_frame

    def GetName(self): return self.name
    def SetName(self, n): self.name = n; return True
    def GetUniqueId(self): return self.uid
    def GetStartFrame(self): return self.start_frame
    def GetEndFrame(self): return self.items[-1].end if self.items else self.start_frame
    def GetSetting(self, key=None): return {"timelineFrameRate": str(self.fps), "timelineDropFrameTimecode": "0"}.get(key, "")
    def GetTrackCount(self, kind): return 1 if kind in ("video", "audio") else 0
    def GetItemListInTrack(self, kind, idx): return list(self.items) if idx == 1 else []

    def AddMarker(self, frame, color, name, note, duration, custom=""):
        if frame in self.markers:
            return False
        self.markers[frame] = {"color": color, "name": name, "note": note, "duration": duration}
        return True

    def GetMarkers(self): return dict(self.markers)

    def GetCurrentTimecode(self):
        return resolve_ops.frames_to_timecode(self.tc_frame, self.fps)

    def SetCurrentTimecode(self, tc):
        self.tc_frame = resolve_ops.timecode_to_frames(tc, self.fps)
        return True


class _MockFolder:
    def __init__(self, clips): self.clips = clips
    def GetClipList(self): return list(self.clips)
    def GetSubFolderList(self): return []


class _MockMediaPool:
    def __init__(self, project): self.project = project
    def GetRootFolder(self): return _MockFolder(self.project.media)

    def CreateEmptyTimeline(self, name):
        if any(t.name == name for t in self.project.timelines):
            return None
        tl = _MockTimeline(name, self.project.fps)
        self.project.timelines.append(tl)
        return tl

    def AppendToTimeline(self, infos):
        tl = self.project.current
        out = []
        for info in infos:
            mpi = info["mediaPoolItem"]
            dur = int(info["endFrame"]) - int(info["startFrame"]) + self.project.inclusive
            start = tl.GetEndFrame()
            item = _MockItem(mpi, start, start + dur, int(info["startFrame"]))
            tl.items.append(item)
            out.append(item)
        return out

    def DeleteTimelines(self, tls):
        ids = {t.uid for t in tls}
        self.project.timelines = [t for t in self.project.timelines if t.uid not in ids]
        return True


class _MockProject:
    def __init__(self, fps):
        self.fps = fps
        self.media = []
        self.timelines = []
        self.current = None
        self.inclusive = 1  # Resolve treats endFrame inclusively
        self.pool = _MockMediaPool(self)

    def GetName(self): return "Mock Project"
    def GetMediaPool(self): return self.pool
    def GetCurrentTimeline(self): return self.current
    def SetCurrentTimeline(self, tl): self.current = tl; return True
    def GetTimelineCount(self): return len(self.timelines)
    def GetTimelineByIndex(self, i): return self.timelines[i - 1]
    def GetSetting(self, key=None): return {"timelineFrameRate": str(self.fps)}.get(key, "")


class _MockPM:
    def __init__(self, project): self.project = project
    def GetCurrentProject(self): return self.project


class MockResolveApp:
    """Scriptable imitation of the Resolve object used in tests/demo."""

    def __init__(self, media_files, fps=30.0):
        self.project = _MockProject(fps)
        self.page = "edit"
        tl = _MockTimeline("Stream VOD", fps)
        for i, (path, seconds) in enumerate(media_files):
            frames = int(seconds * fps)
            mpi = _MockMPI("mock-media-%d" % i, path, fps, frames)
            self.project.media.append(mpi)
            start = tl.GetEndFrame()
            tl.items.append(_MockItem(mpi, start, start + frames, 0))
        self.project.timelines.append(tl)
        self.project.current = tl

    def GetProjectManager(self): return _MockPM(self.project)
    def GetProductName(self): return "DaVinci Resolve (mock)"
    def GetVersionString(self): return "19.1.0"
    def GetCurrentPage(self): return self.page
    def OpenPage(self, p): self.page = p; return True


def make_client(args_bridge=None, token=None, mock_media=None):
    if mock_media:
        return DirectClient(MockResolveApp(mock_media))
    if args_bridge:
        return BridgeClient(args_bridge, token)
    return DirectClient()


def wait_until(predicate, timeout=10.0, interval=0.1):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(interval)
    return False
