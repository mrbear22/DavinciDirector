"""Local application server: JSON API + Server-Sent Events + static UI."""

import io
import json
import mimetypes
import os
import queue
import re
import threading
import time
import traceback
import urllib.parse
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import Cancelled, __version__, stories, transcribe
from .components import ComponentError
from .engine import EditError
from .link import LinkError, frame_to_tc
from .storage import Settings

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
mimetypes.add_type("text/javascript", ".js")  # Windows may map .js to text/plain: modules would not load


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class Job:
    def __init__(self, kind, label):
        self.id = uuid.uuid4().hex[:8]
        self.kind, self.label = kind, label
        self.status, self.progress, self.message = "running", 0.0, ""
        self.cancelled = False
        self.log = []

    def public(self):
        return {"id": self.id, "kind": self.kind, "label": self.label, "status": self.status,
                "progress": round(self.progress, 4), "message": self.message, "log": self.log[-5:]}


class App:
    def __init__(self, engine, settings=None):
        self.engine = engine
        self.link = engine.link
        self.settings = settings or Settings()
        self.jobs = {}
        self.subscribers = []
        self.sub_lock = threading.Lock()
        self.window_closed = self.quit_requested = False
        self.ph_moves = []  # [(time, frame)] of the last playhead changes: tells whether Resolve is playing
        engine.on(self._on_engine)

    def playback(self, now=None):
        """Is Resolve playing?  The script reports the playhead only when it moves (≈ every 0.4 s while
        the window is open): playing = it moved forward at least twice in a row within the last second.
        A single jump (the editor clicked somewhere) is not playback."""
        now = time.time() if now is None else now
        m = [x for x in self.ph_moves if now - x[0] < 1.2]
        playing = len(m) >= 3 and m[-1][1] > m[-2][1] > m[-3][1] and now - m[-1][0] < 0.9
        return {"playing": bool(playing), "frame": self.link.playhead}

    def _follow_playback(self):
        """While Resolve plays, ask the script for the playhead several times a second (not every 0.4 s),
        so the panel's playhead and the sounding word keep up with Resolve; back to slow when it stops."""
        if getattr(self, "_fast_ph", False) or not self.playback()["playing"]:
            return
        self._fast_ph = True
        self.link.set_preview(True)

        def watch():
            while True:
                time.sleep(0.5)
                last = self.ph_moves[-1][0] if self.ph_moves else 0
                if time.time() - last > 1.5:
                    break
            self._fast_ph = False
            self.link.set_preview(False)
        threading.Thread(target=watch, daemon=True, name="dd-follow").start()

    # ------------------------------------------------------------ events
    def emit(self, kind, **payload):
        payload["type"] = kind
        data = json.dumps(payload, ensure_ascii=False)
        with self.sub_lock:
            for q in list(self.subscribers):
                try:
                    q.put_nowait(data)
                except queue.Full:
                    pass

    def _on_engine(self, kind, data):
        if kind == "view":
            self.emit("view", view=data, undo=self.undo_state())
        elif kind == "playhead":
            self.ph_moves = self.ph_moves[-5:] + [(time.time(), data)]
            self.emit("playhead", frame=data)
            self._follow_playback()
        elif kind in ("link", "info", "timelines"):
            self.emit("link", link=self.link_state())

    def ui_changed(self):
        """Window opened -> the script starts watching Resolve and sends fresh state;
        all windows closed (after a grace period for reloads) -> it goes fully idle."""
        if self.subscribers:
            if not self.link.ui_open:
                self.link.set_ui(True)
                self.link.request_state()
            return

        def later():
            if not self.subscribers:
                self.link.set_ui(False)
        threading.Timer(5.0, later).start()

    def link_state(self):
        link = self.link
        snap = link.snapshot or {}
        return {"connected": bool(link.connected), "info": {k: v for k, v in (link.info or {}).items() if k != "timeline_header"},
                "tracks": sorted(snap.get("tracks", {}).keys(), key=lambda k: (k[0] != "v", k)),
                "track": self.engine.track, "errors": list(getattr(link, "errors", []))[-3:],
                "check": snap.get("check") or [], "playhead": link.playhead}

    def undo_state(self):
        e = self.engine
        return {"undo": e.undo_stack[-1]["label"] if e.undo_stack else None,
                "redo": e.redo_stack[-1]["label"] if e.redo_stack else None, "clipboard": len(e.clipboard)}

    # ------------------------------------------------------------ jobs
    def stop_all_jobs(self):
        """Cancel every running job and kill installs in progress."""
        from . import components
        for j in self.jobs.values():
            if j.status == "running":
                j.cancelled = True
        components.kill_children()

    def job_update(self, job, **kw):
        for k, v in kw.items():
            setattr(job, k, v)
        self.emit("job", job=job.public())

    def start_job(self, kind, label, fn):
        for j in self.jobs.values():
            if j.kind == kind and j.status == "running":
                raise ApiError("Задача «%s» вже виконується." % j.label, 409)
        job = Job(kind, label)
        self.jobs[job.id] = job
        self.emit("job", job=job.public())

        def run():
            try:
                fn(job)
                if job.status == "running":
                    self.job_update(job, status="done", progress=1.0)
            except Cancelled:
                self.job_update(job, status="cancelled", message="Скасовано")
            except Exception as e:  # every failure is shown to the user
                traceback.print_exc()
                self.job_update(job, status="error", message=str(e))

        threading.Thread(target=run, daemon=True).start()
        return job.public()

    def start_transcription(self, opts):
        def run(job):
            last = [0.0]

            def progress(x, msg=""):
                now = time.time()
                # a new stage is always shown, even right after the previous update
                if now - last[0] > 0.3 or x >= 1 or msg != job.message:
                    last[0] = now
                    self.job_update(job, progress=x, message=msg)

            transcribe.transcribe_track(self.engine, self.settings, opts, progress=progress,
                                        cancel=lambda: job.cancelled, log=lambda m: job.log.append(m))
        return self.start_job("transcribe", "Транскрибування", run)

    def start_diarization(self, opts):
        def run(job):
            last = [0.0]

            def progress(x, msg=""):
                now = time.time()
                if now - last[0] > 0.3 or x >= 1 or msg != job.message:
                    last[0] = now
                    self.job_update(job, progress=x, message=msg)

            transcribe.diarize_track(self.engine, self.settings, opts, progress=progress, cancel=lambda: job.cancelled)
        return self.start_job("transcribe", "Визначення мовців", run)

    def state(self):
        e = self.engine
        return {"app": "davinci-director", "version": __version__, "link": self.link_state(), "view": e.view,
                "transcripts": e.transcript_payload(), "stories": e.project_state.get("stories", []),
                "topics": e.project_state.get("topics", []),
                "project": e.project_name, "settings": self.settings.public(), "undo": self.undo_state(),
                "jobs": [j.public() for j in self.jobs.values() if j.status == "running"]}


# ------------------------------------------------------------------ exports


def edit_text(engine, srt=False):
    """Current edit as text/SRT (timeline time)."""
    view = engine.view
    fps = view.get("fps") or 25
    lines, n = [], 0
    for b in view["blocks"]:
        tr = engine.transcripts.get(b["m"])
        if not tr:
            continue
        for k, (a, z) in enumerate(tr.frags):
            a, z = max(a, b["i0"]), min(z, b["i1"])
            if z <= a:
                continue
            ws = tr.words[a:z]
            text = stories._join(w[2] for w in ws)
            sp = tr.speakers[ws[0][3]]["name"] if ws[0][3] < len(tr.speakers) else ""
            if srt:
                f0 = b["s"] + (ws[0][0] - b["src0"]) * fps - view["start"]
                f1 = b["s"] + (ws[-1][1] - b["src0"]) * fps - view["start"]
                n += 1
                lines.append("%d\n%s --> %s\n%s\n" % (n, _srt_tc(f0 / fps), _srt_tc(f1 / fps), text))
            else:
                lines.append("[%s] %s: %s" % (frame_to_tc(b["s"] + (ws[0][0] - b["src0"]) * fps, fps), sp, text))
    return "\n".join(lines) + "\n"


def _srt_tc(sec):
    sec = max(0.0, sec)
    ms = int(round(sec * 1000))
    return "%02d:%02d:%02d,%03d" % (ms // 3600000, ms // 60000 % 60, ms // 1000 % 60, ms % 1000)


# ------------------------------------------------------------------ HTTP


class Handler(BaseHTTPRequestHandler):
    app = None
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, body, ctype="application/json; charset=utf-8", status=200, headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status=200):
        self._send(json.dumps(obj, ensure_ascii=False), status=status)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            raise ApiError("Некоректний JSON")

    def _local_only(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            self._send("forbidden", "text/plain", 403)
            return False
        origin = self.headers.get("Origin")
        if origin and not re.match(r"^http://(127\.0\.0\.1|localhost)(:\d+)?$", origin):
            self._send("forbidden", "text/plain", 403)
            return False
        return True

    def do_GET(self):
        if not self._local_only():
            return
        url = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(url.query))
        try:
            if url.path == "/api/ping":
                return self._json({"app": "davinci-director", "version": __version__})
            if url.path == "/api/state":
                return self._json(self.app.state())
            if url.path == "/api/components":
                return self._json(_components_state(bool(q.get("disk"))))
            if url.path == "/api/transcripts":
                return self._json({"transcripts": self.app.engine.transcript_payload(), "tv": self.app.engine.tversion})
            if url.path == "/api/events":
                return self._events()
            if url.path.startswith("/api/export/"):
                return self._export(url.path.rsplit("/", 1)[1], q)
            return self._static(url.path)
        except (ApiError, EditError, LinkError) as e:
            self._json({"error": str(e)}, getattr(e, "status", 400))

    def _events(self):
        q = queue.Queue(maxsize=500)
        with self.app.sub_lock:
            self.app.subscribers.append(q)
        self.app.ui_changed()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            while True:
                try:
                    data = q.get(timeout=10)
                    self.wfile.write(("data: %s\n\n" % data).encode("utf-8"))
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (OSError, ConnectionError):
            pass
        finally:
            with self.app.sub_lock:
                if q in self.app.subscribers:
                    self.app.subscribers.remove(q)
            self.app.ui_changed()

    def _static(self, path):
        if path in ("", "/"):
            path = "/index.html"
        full = os.path.normpath(os.path.join(WEB_DIR, path.lstrip("/")))
        if not full.startswith(WEB_DIR) or not os.path.isfile(full):
            return self._send("not found", "text/plain", 404)
        with open(full, "rb") as f:
            body = f.read()
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        self._send(body, ctype)

    def _export(self, kind, q):
        fn = getattr(self, "export_" + kind, None)
        if not fn:
            raise ApiError("Невідомий експорт", 404)
        e = self.app.engine
        base = re.sub(r"[^\w\-]+", "_", (e.view.get("tl") or e.project_name or "timeline"))[:60]
        return fn(e, q, base)

    def _text(self, name, text, q, ctype="text/plain; charset=utf-8"):
        """``raw=1``: for copying from the page; otherwise a file to save."""
        if q.get("raw") == "1":
            return self._send(text, "text/plain; charset=utf-8")
        return self._send(text, ctype, headers={"Content-Disposition": _disp(name)})

    @staticmethod
    def _budget(q):
        return int(q.get("budget") or stories.TOKEN_BUDGET), q.get("only") == "1"

    def export_estimate(self, e, q, base):
        res = stories.export_for_claude(e, *self._budget(q))
        return self._json({"mode": res["mode"], "tokens": res["tokens"], "parts": len(res["files"]),
                           "beats": len(e.project_state.get("beats") or []), "budget": stories.TOKEN_BUDGET,
                           "brief": stories.get_brief(e), "topics_parts": len(stories.export_topics(e, *self._budget(q))),
                           "topics": len(e.project_state.get("topics") or []),
                           "durations": {k: list(v[:3]) for k, v in stories.DURATIONS.items()}})

    def export_claude(self, e, q, base):
        files = stories.export_for_claude(e, *self._budget(q))["files"]
        if q.get("raw") == "1":
            return self._send(files[int(q.get("part") or 1) - 1][1], "text/plain; charset=utf-8")
        if len(files) == 1:
            return self._text(files[0][0], files[0][1], q)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for name, text in files:
                z.writestr(name, text)
        return self._send(buf.getvalue(), "application/zip", headers={"Content-Disposition": _disp("%s.prompt-parts.zip" % base)})

    def export_topics(self, e, q, base):
        name, text = stories.export_topics(e, *self._budget(q))[int(q.get("part") or 1) - 1]
        return self._text(name, text, q)

    def export_stage2(self, e, q, base):
        return self._text(*stories.export_stage2(e), q)

    def export_schema(self, e, q, base):
        body = json.dumps({"rules": stories.RULES, "answer_format": stories.ANSWER_SCHEMA}, ensure_ascii=False, indent=2)
        return self._text("dd-stories-schema.json", body, q, "application/json; charset=utf-8")

    def export_srt(self, e, q, base):
        return self._text("%s.srt" % base, edit_text(e, srt=True), q, "application/x-subrip; charset=utf-8")

    def export_txt(self, e, q, base):
        return self._text("%s.txt" % base, edit_text(e), q)

    def export_story(self, e, q, base):
        body = json.dumps({"format": stories.STORIES_FORMAT, "stories": [stories.find_story(e, q.get("uid"))]},
                          ensure_ascii=False, indent=2)
        return self._text("%s.story.json" % base, body, q, "application/json; charset=utf-8")

    def do_POST(self):
        if not self._local_only():
            return
        name = urllib.parse.urlparse(self.path).path[len("/api/"):].replace("/", "_")
        fn = getattr(self, "post_" + name, None)
        if not fn:
            return self._json({"error": "not found"}, 404)
        try:
            res = fn(self._body())
            self._json(res if res is not None else {"ok": True})
        except (ApiError, EditError, LinkError, transcribe.TranscriptionError, ComponentError) as e:
            self._json({"error": str(e)}, getattr(e, "status", 400))
        except Exception as e:  # unexpected: report, keep serving
            traceback.print_exc()
            self._json({"error": "Внутрішня помилка: %s" % e}, 500)

    # ---- editing
    def post_edit(self, b):
        e = self.app.engine
        op, v = b.get("op"), b.get("v")
        if op == "delete":
            res = e.delete(b["sel"], v)
        elif op == "move":
            res = e.move(b["sel"], b["pos"], v)
        elif op == "copy":
            res = e.copy(b["sel"])
        elif op == "paste":
            res = e.paste(b["pos"], v)
        elif op == "duplicate":
            res = e.duplicate(b["sel"], v)
        elif op == "restore":
            res = e.restore(b["k"], b["i0"], b["i1"], v)
        elif op == "close_gaps":
            res = e.close_all_gaps()
        elif op == "undo":
            res = e.undo()
        elif op == "redo":
            res = e.redo()
        else:
            raise ApiError("Невідома дія")
        self.app.emit("view", view=e.view, undo=self.app.undo_state())
        return {"ok": True, "res": res, "undo": self.app.undo_state()}

    def post_goto(self, b):
        e = self.app.engine
        if b.get("frame") is not None:
            return e.goto_frame(int(b["frame"]))
        if b.get("fid"):
            f = stories.locate_fragment(e, b["fid"])
            if f is None:
                raise ApiError("Фрагмент %s зараз не на таймлайні." % b["fid"])
            return e.goto_frame(f)
        return e.goto_word(int(b["b"]), int(b.get("w") if b.get("w") is not None else -1))

    def post_preview(self, b):
        """Prepare playback of a selection in Resolve: In/Out marks + playhead at In.
        The window then brings Resolve forward and presses Space; we stop at Out."""
        e = self.app.engine
        f1, f2 = e.selection_frames(b["sel"])
        fps = e.view.get("fps") or 25
        start = e.view.get("start") or 0
        res = self.app.link.run([{"op": "marks", "inn": int(f1), "out": int(f2 - 1), "soft": True},
                                 {"op": "go", "tc": frame_to_tc(f1, fps)}], timeout=10)
        return {"f1": f1, "f2": f2, "start": start, "seconds": (f2 - f1) / fps, "marks": (res.get("res") or [None])[0]}

    def post_playback(self, b):
        return self.app.playback()

    def post_preview_start(self, b):
        from . import resolve_window
        if not resolve_window.find_resolve_window():
            raise ApiError("Вікно DaVinci Resolve не знайдено.")
        threading.Thread(target=resolve_window.play_for, args=(float(b.get("seconds") or 3),), daemon=True).start()

    def post_track(self, b):
        self.app.engine.set_track(b["track"])
        self.app.emit("link", link=self.app.link_state())

    def post_refresh(self, b):
        return self.app.link.run([{"op": "state"}], timeout=20)

    def post_timelines(self, b):
        return self.app.link.run([{"op": "timelines"}], timeout=20)

    def post_set_timeline(self, b):
        return self.app.link.run([{"op": "set_timeline", "id": b["id"]}], timeout=20)

    # ---- transcript
    def post_transcribe(self, b):
        return self.app.start_transcription(b)

    def post_diarize(self, b):
        return self.app.start_diarization(b)

    def post_components_install(self, b):
        from . import components
        name = b["name"]
        if name not in components.COMPONENTS:
            raise ApiError("Невідомий компонент")

        def run(job):
            components.ensure(name, lambda x, m: self.app.job_update(job, progress=x, message=m),
                              cancel=lambda: job.cancelled)
        return self.app.start_job("download", "Завантаження", run)

    def post_components_remove(self, b):
        from . import components
        res = components.remove(b["name"])
        return {"removed": res}

    def post_models_download(self, b):
        from . import components, models

        def run(job):
            progress = lambda x, m: self.app.job_update(job, progress=x, message=m)  # noqa: E731
            components.ensure("speakers", progress, cancel=lambda: job.cancelled)  # brings huggingface_hub
            models.ensure_asr(progress, cancel=lambda: job.cancelled)
        return self.app.start_job("download", "Завантаження моделі", run)

    def post_cancel(self, b):
        job = self.app.jobs.get(b.get("id"))
        if job:
            job.cancelled = True

    def post_word(self, b):
        self.app.engine.edit_word(b["m"], int(b["i"]), b["text"])

    def post_words(self, b):
        """Correct the text of a selected phrase (several words of one clip)."""
        self.app.engine.edit_words(b["m"], int(b["i0"]), int(b["i1"]), b["text"])

    def post_speaker_rename(self, b):
        self.app.engine.rename_speaker(b["m"], b["sid"], b["name"])

    def post_words_speaker(self, b):
        self.app.engine.set_words_speaker(b.get("ranges") or [], b["sid"])

    # ---- stories
    def post_brief(self, b):
        """What the user wants from the language model (duration, variants, wishes); saved per project."""
        return {"brief": stories.set_brief(self.app.engine, b)}

    def post_stories_import(self, b):
        res = stories.import_any(self.app.engine, b.get("text") or "")
        res["stories"] = self.app.engine.project_state.get("stories", [])
        res["topics"] = self.app.engine.project_state.get("topics", [])
        return res

    def post_topics_clear(self, b):
        stories.clear_topics(self.app.engine)
        return {"topics": []}

    def post_story_update(self, b):
        stories.update_story(self.app.engine, b["uid"], b.get("patch") or {})
        return {"stories": self.app.engine.project_state["stories"]}

    def post_story_apply(self, b):
        return stories.apply_story(self.app.engine, b["uid"], b.get("mode", "new"), b.get("markers", True))

    def post_scene_apply(self, b):
        return stories.apply_scene(self.app.engine, b["uid"], b["scene"], b.get("where", "playhead"))

    def post_window_closed(self, b):
        """The user closed the window: stop everything it started (the app restarts as an idle watcher)."""
        self.app.stop_all_jobs()
        self.app.window_closed = True

    def post_quit(self, b):
        self.app.quit_requested = True

    def post_settings(self, b):
        self.app.settings.update(b)
        return self.app.settings.public()


def _components_state(disk):
    from . import components, models, parakeet
    st = components.status(disk=disk)
    st["models"] = [{"name": models.ASR, "available": models.available(), "label": parakeet.LABEL,
                     "credit": parakeet.CREDIT, "license": parakeet.LICENSE_URL, "size": "2,5 ГБ"}]
    st["local_models"] = models.list_local()
    return st


def _disp(name):
    ascii_name = re.sub(r"[^\w.\-]+", "_", name.encode("ascii", "replace").decode())
    return "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (ascii_name, urllib.parse.quote(name))


def make_server(app, port, host="127.0.0.1"):
    handler = type("BoundHandler", (Handler,), {"app": app})
    for p in [port] + list(range(port + 1, port + 20)) + [0]:
        try:
            srv = ThreadingHTTPServer((host, p), handler)
            srv.daemon_threads = True
            return srv
        except OSError:
            continue
    raise OSError("no free port")
