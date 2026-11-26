"""Local application server: JSON API + Server-Sent Events + static UI."""

import json
import mimetypes
import os
import queue
import re
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import exporters, media, transcribe
from .bridge import ResolveUnavailable
from .claude_ai import ClaudeService
from .model import Project, apply_loudness, build_tokens, new_id, SPEAKER_PALETTE
from .resolve_ops import ResolveError
from .storage import DebouncedSaver, Settings, atomic_write, base_dir, project_path

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class Job:
    def __init__(self, kind, label):
        self.id = new_id("j")
        self.kind = kind
        self.label = label
        self.status = "running"
        self.progress = 0.0
        self.message = ""
        self.cancelled = False
        self.started = time.time()

    def public(self):
        return {"id": self.id, "kind": self.kind, "label": self.label, "status": self.status,
                "progress": round(self.progress, 4), "message": self.message}


class App:
    def __init__(self, resolve_client, settings=None, claude=None):
        self.resolve = resolve_client
        self.settings = settings or Settings()
        self.claude = claude or ClaudeService(self.settings)
        self.lock = threading.RLock()
        self.project = None
        self.project_file = None
        self.saver = DebouncedSaver(1.0)
        self.jobs = {}
        self.subscribers = []
        self.sub_lock = threading.Lock()
        self._sync_timer = None
        self.last_error = None

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
            except transcribe.Cancelled:
                self.job_update(job, status="cancelled", message="Скасовано")
            except Exception as e:  # report every failure to the UI
                traceback.print_exc()
                msg = str(e) if str(e) != "cancelled" else "Скасовано"
                self.job_update(job, status="cancelled" if msg == "Скасовано" else "error", message=msg)

        threading.Thread(target=run, daemon=True).start()
        return job.public()

    # ------------------------------------------------------------ project
    def save(self):
        if self.project and self.project_file:
            proj = self.project
            self.saver.schedule(self.project_file, proj.to_json)

    def changed(self, edit=True, full=False, **extra):
        """Persist and notify clients about project changes."""
        self.save()
        p = self.project
        payload = {"history": p.history_state(), "edit": p.data["edit"], "render": self.render_summary()}
        if full:
            payload["project"] = self.public_project()
        payload.update(extra)
        self.emit("project", **payload)
        if edit and self.settings.get("auto_sync"):
            self.schedule_sync()
        return payload

    def public_project(self):
        if not self.project:
            return None
        d = dict(self.project.data)
        d.pop("chat_api", None)
        return d

    def render_summary(self):
        if not self.project or not self.project.tokens:
            return {"segments": [], "duration": 0}
        segs = self.project.render()
        return {"segments": segs, "duration": round(sum(s["dur"] for s in segs), 3)}

    def require_project(self, transcript=False):
        if not self.project:
            raise ApiError("Проєкт не завантажено. Підключіться до таймлайну Resolve.")
        if transcript and not self.project.tokens:
            raise ApiError("Спочатку створіть транскрипцію.")
        return self.project

    def _index_path(self):
        return os.path.join(base_dir(), "index.json")

    def _index(self):
        try:
            with open(self._index_path(), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _index_set(self, timeline_id, path):
        idx = self._index()
        idx[timeline_id] = path
        atomic_write(self._index_path(), json.dumps(idx, ensure_ascii=False, indent=1))

    def connect(self, timeline_id=None):
        """Load (or create) the project for the current Resolve timeline."""
        info = self.resolve.call("read_timeline", timeline_id=timeline_id)
        tl_id = info["timeline_id"]
        # opening the generated edit timeline -> load its source project
        path = self._index().get(tl_id)
        if path and os.path.exists(path) and path != project_path(info["project"], tl_id):
            with open(path, encoding="utf-8") as f:
                proj = Project.from_json(f.read())
            with self.lock:
                self.saver.flush()
                self.project, self.project_file = proj, path
            return {"loaded": True, "from_edit_timeline": True}
        if not info["clips"]:
            raise ApiError("На таймлайні «%s» немає відеокліпів з медіафайлами." % info["timeline"])
        path = project_path(info["project"], tl_id)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                proj = Project.from_json(f.read())
            changed = _clips_signature(proj.data["clips"]) != _clips_signature(info["clips"])
            proj.data["resolve"].update({k: info[k] for k in ("project", "timeline", "timeline_id", "fps", "drop_frame", "start_frame")})
            proj.data["timeline_changed"] = changed
            if not proj.tokens:
                proj.data["clips"] = _clips_with_fps(info)
        else:
            proj = Project()
            proj.data["resolve"] = {k: info[k] for k in ("project", "timeline", "timeline_id", "fps", "drop_frame", "start_frame")}
            proj.data["clips"] = _clips_with_fps(info)
        with self.lock:
            self.saver.flush()
            self.project, self.project_file = proj, path
            self._index_set(tl_id, path)
        self.save()
        return {"loaded": True, "timeline_changed": proj.data.get("timeline_changed", False)}

    def refresh_clips(self):
        """Re-read clips from Resolve after the source timeline changed
        (requires a new transcription)."""
        p = self.require_project()
        info = self.resolve.call("read_timeline", timeline_id=p.data["resolve"]["timeline_id"])
        with self.lock:
            p.data["clips"] = _clips_with_fps(info)
            p.data["timeline_changed"] = False
            p.set_transcript([], {})
            p.data["transcription"] = {"status": "none"}
        self.changed(full=True)

    # ------------------------------------------------------------ transcription
    def start_transcription(self, opts):
        p = self.require_project()
        model = opts.get("model") or self.settings.get("whisper_model")
        language = opts.get("language", self.settings.get("language")) or None
        do_diar = opts.get("diarize", self.settings.get("diarize"))
        num_speakers = int(opts.get("num_speakers") or self.settings.get("num_speakers") or 0)

        def run(job):
            clips = p.data["clips"]
            total = sum((c["src_out"] - c["src_in"]) / c["media_fps"] for c in clips) or 1.0
            done = 0.0
            clip_words, loud, all_labels = {}, {}, []
            notes = []
            for ci, c in enumerate(clips):
                start, end = c["src_in"] / c["media_fps"], c["src_out"] / c["media_fps"]
                dur = end - start
                base = done / total
                share = dur / total

                def prog(x, phase, w0, w1):
                    self.job_update(job, progress=base + share * (w0 + (w1 - w0) * x),
                                    message="Кліп %d/%d: %s" % (ci + 1, len(clips), phase))

                prog(0, "витягування аудіо (FFmpeg)", 0, 0.05)
                wav = media.extract_audio(c["path"], start, end, cancel=lambda: job.cancelled,
                                          progress=lambda x: prog(x, "витягування аудіо (FFmpeg)", 0, 0.05))
                hop, values = media.loudness_profile(wav)
                loud[ci] = (hop, _shift_loudness(values, hop, start))
                prog(0, "розпізнавання мови (Whisper %s)" % model, 0.05, 0.85)
                words, lang = transcribe.whisper_words(
                    wav, model_name=model, language=language,
                    device=self.settings.get("whisper_device"), compute_type=self.settings.get("whisper_compute_type"),
                    progress=lambda x: prog(x, "розпізнавання мови (Whisper %s)" % model, 0.05, 0.85),
                    cancel=lambda: job.cancelled, duration=dur)
                turns = []
                if do_diar and words:
                    prog(0, "визначення мовців (pyannote)", 0.85, 1.0)
                    try:
                        turns = transcribe.diarize(wav, self.settings.get("hf_token"), num_speakers)
                    except Exception as e:  # diarization is optional
                        notes.append(str(e))
                labels = transcribe.assign_speakers(words, turns)
                transcribe.smooth_speakers(words)
                # speaker labels are per clip; keep them distinct per clip only if diarized
                for w in words:
                    w["t0"] += start
                    w["t1"] += start
                for lab in labels:
                    if lab not in all_labels:
                        all_labels.append(lab)
                clip_words[ci] = words
                done += dur
            self._install_transcript(p, clip_words, loud, all_labels or ["S1"],
                                     {"status": "done", "engine": "faster-whisper " + model, "language": language or "auto",
                                      "diarized": bool(do_diar and not notes), "notes": notes, "finished": time.time()})
            self.job_update(job, status="done", progress=1.0,
                            message=("Готово. " + " ".join(notes)) if notes else "Готово")

        return self.start_job("transcribe", "Транскрибування", run)

    def _install_transcript(self, p, clip_words, loud, labels, meta):
        tokens = build_tokens(p.data["clips"], clip_words)
        if loud:
            apply_loudness(tokens, loud)
        old = p.data.get("speakers") or {}
        speakers = {}
        for n, lab in enumerate(labels):
            speakers[lab] = old.get(lab) or {"name": "Мовець %d" % (n + 1), "color": SPEAKER_PALETTE[n % len(SPEAKER_PALETTE)]}
        with self.lock:
            p.set_transcript(tokens, speakers)
            p.data["transcription"] = meta
        self.changed(full=True)

    def import_transcript(self, text, fmt, offset_mode="timeline"):
        p = self.require_project()
        if fmt == "json":
            words = transcribe.parse_whisper_json(text)
        else:
            words = transcribe.parse_subtitles(text)
        if not words:
            raise ApiError("Не вдалося прочитати слова з файлу.")
        labels = transcribe.labels_to_speakers(words)
        clips = p.data["clips"]
        tfps = p.data["resolve"].get("fps") or 25.0
        start_frame = p.data["resolve"].get("start_frame", 0)
        clip_words = {}
        for w in words:
            t0, t1 = w["t0"], w["t1"]
            if offset_mode == "timeline":
                # subtitles exported from Resolve use timeline timecode (from TC start)
                if t0 * tfps >= start_frame and start_frame > 0:
                    t0 -= start_frame / tfps
                    t1 -= start_frame / tfps
                for ci, c in enumerate(clips):
                    r0, r1 = c["rec_start"] / tfps, c["rec_end"] / tfps
                    if r0 <= t0 < r1:
                        off = c["src_in"] / c["media_fps"] - r0
                        clip_words.setdefault(ci, []).append(dict(w, t0=t0 + off, t1=min(t1, r1) + off))
                        break
            else:
                clip_words.setdefault(0, []).append(w)
        self._install_transcript(p, clip_words, {}, labels, {"status": "done", "engine": "import:" + fmt,
                                                             "finished": time.time(), "notes": []})
        return {"words": len(words)}

    # ------------------------------------------------------------ sync
    def schedule_sync(self, delay=2.0):
        if self._sync_timer:
            self._sync_timer.cancel()
        self._sync_timer = threading.Timer(delay, self._auto_sync)
        self._sync_timer.daemon = True
        self._sync_timer.start()

    def _auto_sync(self):
        try:
            self.sync_timeline()
        except Exception as e:
            self.emit("toast", level="error", text="Автосинхронізація: %s" % e)

    def sync_timeline(self, _retry=True):
        p = self.require_project(transcript=True)
        with self.lock:
            frames = p.render_frames()
            markers = p.edit_markers(frames)
            name = (p.data["resolve"].get("timeline") or "Timeline") + self.settings.get("edit_timeline_suffix")
            inclusive = p.data["settings"].get("end_inclusive", True)
            replace_id = p.data["edit"].get("synced_timeline_id")
        if not frames:
            raise ApiError("Монтаж порожній — нічого переносити на таймлайн.")
        self.emit("sync", status="running")
        res = self.resolve.call("build_timeline", name=name, segments=[
            {"media_id": f["media_id"], "path": f["path"], "start": f["start"], "end": f["end"]} for f in frames],
            markers=markers, replace_id=replace_id, end_inclusive=inclusive)
        with self.lock:
            p.data["edit"].update({"synced_timeline_id": res["timeline_id"], "synced_timeline": res["timeline"],
                                   "last_sync": time.time(), "dirty": False,
                                   "synced_frames": [{"rec": f["rec"], "rec_dur": f["rec_dur"], "start": f["start"],
                                                      "parts": f["parts"]} for f in frames]})
            self._index_set(res["timeline_id"], self.project_file)
        # calibrate AppendToTimeline endFrame semantics once
        fd, fe = res.get("first_duration"), res.get("first_expected")
        if _retry and fd is not None and fe is not None and fd != fe and abs(fd - fe) == 1:
            p.data["settings"]["end_inclusive"] = not inclusive
            return self.sync_timeline(_retry=False)
        self.save()
        self.emit("sync", status="done", result=res)
        self.emit("project", edit=p.data["edit"], history=p.history_state(), render=self.render_summary())
        return res

    def jump(self, token=None, rec=None, target="auto", piece=None):
        """Move Resolve's playhead to a token (source or edited timeline)."""
        p = self.require_project(transcript=True)
        tfps = p.data["resolve"].get("fps") or 25.0
        src_id = p.data["resolve"]["timeline_id"]
        edit_id = p.data["edit"].get("synced_timeline_id")
        current = self.resolve.call("current_timeline_id")
        use_edit = target == "edit" or (target == "auto" and edit_id and current == edit_id)
        if use_edit and token is not None:
            frame = _edit_frame(p, int(token), piece)
            if frame is not None:
                tc = self.resolve.call("jump", timeline_id=edit_id, frame_offset=frame, open_timeline=True)
                return {"timeline": "edit", "timecode": tc}
        if token is not None:
            rec = p.tokens[int(token)]["r0"]
        frame = int(round(float(rec or 0) * tfps))
        tc = self.resolve.call("jump", timeline_id=src_id, frame_offset=frame, open_timeline=True)
        return {"timeline": "source", "timecode": tc}

    # ------------------------------------------------------------ claude
    def start_analysis(self, instructions, variants):
        p = self.require_project(transcript=True)

        def run(job):
            stats = {"thinking": 0, "text": 0}

            def emit(kind, delta):
                if kind in stats:
                    stats[kind] += len(delta)
                    if kind == "thinking":
                        self.emit("ai_thinking", job=job.id, delta=delta)
                    if job.cancelled:
                        raise transcribe.Cancelled()
                    est = min(0.95, 0.05 + stats["text"] / 18000.0)
                    if kind == "text" and int(stats["text"]) % 200 < len(delta):
                        self.job_update(job, progress=est, message="Claude пише структуру… %d симв." % stats["text"])
                    elif kind == "thinking" and int(stats["thinking"]) % 300 < len(delta):
                        self.job_update(job, progress=0.05, message="Claude аналізує запис…")

            self.job_update(job, progress=0.02, message="Надсилання транскрипції Claude…")
            an = self.claude.analyze(p, instructions=instructions, variants=variants, emit=emit)
            with self.lock:
                p.checkpoint("analysis")
                p.data["analyses"].insert(0, an)
            self.changed(edit=False, full=True)
            self.job_update(job, status="done", progress=1.0,
                            message="Знайдено %d варіант(и), %d моментів" % (len(an["variants"]), len(an["moments"])))
            self.emit("analysis", analysis=an["id"])

        return self.start_job("analyze", "Пошук сюжетів (Claude)", run)

    def start_chat(self, text):
        p = self.require_project(transcript=True)

        def on_variant(variant, replace_id):
            with self.lock:
                p.checkpoint("chat_variant")
                if replace_id:
                    for an in p.data["analyses"]:
                        for n, v in enumerate(an["variants"]):
                            if v["id"] == replace_id:
                                variant["id"] = replace_id
                                an["variants"][n] = variant
                                if (p.data["edit"].get("story_link") or {}).get("variant") == replace_id:
                                    p.reapply_linked()
                                return replace_id
                chat_an = next((a for a in p.data["analyses"] if a.get("source") == "chat"), None)
                if not chat_an:
                    chat_an = {"id": new_id("a"), "created": time.time(), "model": "", "source": "chat",
                               "overview": "Варіанти, створені в чаті з Claude", "moments": [], "variants": [], "warnings": []}
                    p.data["analyses"].insert(0, chat_an)
                chat_an["variants"].insert(0, variant)
                return variant["id"]

        def run(job):
            def emit(kind, delta):
                if job.cancelled:
                    raise transcribe.Cancelled()
                if kind in ("text", "thinking"):
                    self.emit("chat_delta", job=job.id, part=kind, delta=delta)
                elif kind == "variant":
                    self.changed(edit=bool(p.data["edit"].get("story_link")), full=True)

            self.job_update(job, message="Claude думає…")
            try:
                self.claude.chat(p, text, emit=emit, on_variant=on_variant)
            except Exception:
                p.data["chat"].append({"role": "error", "text": "Помилка запиту до Claude.", "ts": time.time()})
                self.changed(edit=False, full=True)
                raise
            self.changed(edit=False, full=True)
            self.job_update(job, status="done", progress=1.0, message="")

        return self.start_job("chat", "Чат з Claude", run)


# ---------------------------------------------------------------- helpers

def _clips_with_fps(info):
    clips = []
    for c in info["clips"]:
        c = dict(c)
        c["fps"] = info["fps"]
        c["media_fps"] = c.get("media_fps") or info["fps"]
        clips.append(c)
    return clips


def _clips_signature(clips):
    return [(c["path"], c["src_in"], c["src_out"], c["rec_start"]) for c in clips]


def _shift_loudness(values, hop, start):
    """Loudness list is relative to the WAV (clip) start; tokens use absolute
    media seconds -> pad the front so index = media_time / hop."""
    pad = int(round(start / hop))
    return [-120.0] * pad + values


def _edit_frame(p, token, piece=None):
    frames = p.data["edit"].get("synced_frames") or []
    tfps = p.data["resolve"].get("fps") or 25.0
    t = p.tokens[token]
    clip = p.data["clips"][t["c"]]
    mfps = clip.get("media_fps") or tfps
    for f in frames:
        for part in f["parts"]:
            if part["a"] <= token <= part["b"] and (piece is None or part["pid"] == piece):
                return f["rec"] + int(round((t["t0"] * mfps - f["start"]) * tfps / mfps))
    return None


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    app = None  # set by make_server
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if os.environ.get("DD_DEBUG"):
            super().log_message(fmt, *args)

    # -------------------------------------------------------------- utils
    def _json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _local_only(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            self._json({"error": "forbidden"}, 403)
            return False
        origin = self.headers.get("Origin")
        if origin and not re.match(r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$", origin):
            self._json({"error": "forbidden origin"}, 403)
            return False
        return True

    # -------------------------------------------------------------- GET
    def do_GET(self):
        if not self._local_only():
            return
        url = urllib.parse.urlparse(self.path)
        path = url.path
        try:
            if path == "/api/events":
                return self._events()
            if path == "/api/ping":
                return self._json({"ok": True, "app": "davinci-director"})
            if path == "/api/state":
                return self._json(self._state())
            if path == "/api/playhead":
                return self._json(self._playhead())
            if path.startswith("/media/"):
                return self._media(int(path.split("/")[2]))
            if path.startswith("/api/export/"):
                return self._export(path.rsplit("/", 1)[1], urllib.parse.parse_qs(url.query))
            return self._static(path)
        except ApiError as e:
            self._json({"error": str(e)}, e.status)
        except (ResolveError, ResolveUnavailable) as e:
            self._json({"error": str(e), "resolve": True}, 502)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            traceback.print_exc()
            try:
                self._json({"error": str(e)}, 500)
            except OSError:
                pass

    def _state(self):
        app = self.app
        info = None
        try:
            info = app.resolve.call("get_info")
            resolve_ok = True
        except Exception as e:
            resolve_ok = False
            info = {"error": str(e)}
        p = app.project
        return {
            "settings": app.settings.public(),
            "resolve": {"ok": resolve_ok, "info": info, "kind": app.resolve.kind},
            "project": app.public_project(),
            "history": p.history_state() if p else None,
            "render": app.render_summary(),
            "jobs": [j.public() for j in app.jobs.values() if j.status == "running"],
        }

    def _playhead(self):
        app = self.app
        p = app.project
        if not p or not p.tokens:
            return {}
        ph = app.resolve.call("playhead")
        if not ph:
            return {}
        tfps = p.data["resolve"].get("fps") or 25.0
        if ph["timeline_id"] == p.data["resolve"].get("timeline_id"):
            rec = ph["frame"] / tfps
            idx = p.tokens_in_range(rec - 0.5, rec + 0.5)
            best = min(idx, key=lambda i: abs(p.tokens[i]["r0"] - rec)) if idx else None
            return {"timeline": "source", "token": best, "timecode": ph["timecode"]}
        if ph["timeline_id"] == p.data["edit"].get("synced_timeline_id"):
            for f in p.data["edit"].get("synced_frames") or []:
                if f["rec"] <= ph["frame"] < f["rec"] + f["rec_dur"]:
                    clip_tok = p.tokens[f["parts"][0]["a"]]
                    mfps = p.data["clips"][clip_tok["c"]].get("media_fps") or tfps
                    media_t = (f["start"] + (ph["frame"] - f["rec"]) * mfps / tfps) / mfps
                    for part in f["parts"]:
                        for i in range(part["a"], part["b"] + 1):
                            if p.tokens[i]["t0"] <= media_t < p.tokens[i]["t1"]:
                                return {"timeline": "edit", "token": i, "piece": part["pid"], "timecode": ph["timecode"]}
            return {"timeline": "edit", "token": None, "timecode": ph["timecode"]}
        return {"timeline": "other", "timecode": ph["timecode"]}

    def _events(self):
        q = queue.Queue(maxsize=2000)
        with self.app.sub_lock:
            self.app.subscribers.append(q)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            self.wfile.write(b": hello\n\n")
            self.wfile.flush()
            while True:
                try:
                    data = q.get(timeout=15)
                    self.wfile.write(("data: %s\n\n" % data).encode("utf-8"))
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with self.app.sub_lock:
                if q in self.app.subscribers:
                    self.app.subscribers.remove(q)
            self.close_connection = True

    def _static(self, path):
        if path in ("", "/"):
            path = "/index.html"
        full = os.path.normpath(os.path.join(WEB_DIR, path.lstrip("/")))
        if not full.startswith(WEB_DIR) or not os.path.isfile(full):
            return self._json({"error": "not found"}, 404)
        with open(full, "rb") as f:
            data = f.read()
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _media(self, clip_index):
        p = self.app.require_project()
        clip = p.data["clips"][clip_index]
        path = media.existing_proxy(clip["path"]) or clip["path"]
        if not os.path.exists(path):
            return self._json({"error": "media missing"}, 404)
        size = os.path.getsize(path)
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        status = 200
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else size - 1
                else:
                    start = size - int(m.group(2))
                end = min(end, size - 1, start + 8 * 1024 * 1024 - 1)
                status = 206
        ctype = mimetypes.guess_type(path)[0] or "video/mp4"
        if path.lower().endswith((".mkv",)):
            ctype = "video/webm"
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = f.read(min(1024 * 256, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _export(self, kind, query):
        p = self.app.require_project(transcript=True)
        scope = (query.get("scope") or ["edit"])[0]
        if kind == "srt":
            data, ctype, ext = exporters.srt(p, scope), "application/x-subrip", "srt"
        elif kind == "txt":
            data, ctype, ext = exporters.text(p, scope), "text/plain", "txt"
        elif kind == "edl":
            data, ctype, ext = exporters.edl(p), "text/plain", "edl"
        elif kind == "json":
            data, ctype, ext = p.to_json(), "application/json", "json"
        elif kind == "md":
            data, ctype, ext = exporters.story_markdown(p, (query.get("variant") or [None])[0]), "text/markdown", "md"
        else:
            raise ApiError("unknown export")
        raw = data.encode("utf-8")
        name = re.sub(r"[^\w\-]+", "_", p.data["resolve"].get("timeline", "edit")) + "_" + scope + "." + ext
        self.send_response(200)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Disposition", 'attachment; filename="%s"' % name)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    # -------------------------------------------------------------- POST
    def do_POST(self):
        if not self._local_only():
            return
        path = urllib.parse.urlparse(self.path).path
        app = self.app
        try:
            body = self._body()
            route = path[len("/api/"):] if path.startswith("/api/") else None
            handler = getattr(self, "post_" + (route or "").replace("/", "_"), None)
            if not handler:
                raise ApiError("not found", 404)
            self._json(handler(body) or {"ok": True})
        except ApiError as e:
            self._json({"error": str(e)}, e.status)
        except (ResolveError, ResolveUnavailable) as e:
            self._json({"error": str(e), "resolve": True}, 502)
        except (KeyError, ValueError, IndexError) as e:
            traceback.print_exc()
            self._json({"error": "Некоректний запит: %s" % e}, 400)
        except Exception as e:
            traceback.print_exc()
            app.last_error = str(e)
            self._json({"error": str(e)}, 500)

    def post_connect(self, b):
        res = self.app.connect(b.get("timeline_id"))
        res["state"] = self._state()
        return res

    def post_bridge(self, b):
        from .bridge import BridgeClient
        self.app.resolve = BridgeClient(b["url"], b["token"])
        self.app.emit("toast", level="info", text="Підключено до DaVinci Resolve")
        return {"ok": True}

    def post_refresh_clips(self, b):
        self.app.refresh_clips()

    def post_transcribe(self, b):
        return self.app.start_transcription(b)

    def post_import_transcript(self, b):
        return self.app.import_transcript(b["text"], b.get("format", "srt"), b.get("offset_mode", "timeline"))

    def post_cancel(self, b):
        job = self.app.jobs.get(b["job"])
        if job:
            job.cancelled = True
        return {"ok": True}

    def post_edit(self, b):
        app = self.app
        p = app.require_project(transcript=True)
        with app.lock:
            result = p.op(b["op"], **(b.get("args") or {}))
        out = app.changed()
        out["result"] = result
        return out

    def post_undo(self, b):
        app = self.app
        p = app.require_project(transcript=True)
        with app.lock:
            label = p.undo()
        return dict(app.changed(full=True), label=label)

    def post_redo(self, b):
        app = self.app
        p = app.require_project(transcript=True)
        with app.lock:
            label = p.redo()
        return dict(app.changed(full=True), label=label)

    def post_words(self, b):
        app = self.app
        p = app.require_project(transcript=True)
        with app.lock:
            n = p.edit_words(b["changes"])
        app.changed(edit=False)
        return {"changed": n, "history": p.history_state()}

    def post_speakers(self, b):
        app = self.app
        p = app.require_project(transcript=True)
        action = b["action"]
        with app.lock:
            if action == "add":
                sid = p.add_speaker(b.get("name"))
                res = {"id": sid}
            elif action == "rename":
                p.rename_speaker(b["id"], b.get("name"), b.get("color"))
                res = {}
            elif action == "assign":
                sp = b.get("id") or p.add_speaker(b.get("name"))
                res = {"changed": p.assign_speaker(b["sel"], sp), "id": sp}
            else:
                raise ApiError("unknown action")
        app.changed(edit=False, full=True)
        return res

    def post_sync(self, b):
        return self.app.sync_timeline()

    def post_jump(self, b):
        return self.app.jump(b.get("token"), b.get("rec"), b.get("target", "auto"), b.get("piece"))

    def post_ai_analyze(self, b):
        return self.app.start_analysis(b.get("instructions", ""), int(b.get("variants") or 3))

    def post_ai_chat(self, b):
        text = (b.get("text") or "").strip()
        if not text:
            raise ApiError("Порожнє повідомлення")
        return self.app.start_chat(text)

    def post_ai_chat_clear(self, b):
        app = self.app
        p = app.require_project()
        with app.lock:
            p.data["chat"] = []
            p.data["chat_api"] = []
        app.changed(edit=False, full=True)

    def post_story(self, b):
        app = self.app
        p = app.require_project(transcript=True)
        args = {k: v for k, v in b.items() if k not in ("analysis", "variant", "action")}
        with app.lock:
            res = p.story_op(b["analysis"], b["variant"], b["action"], **args)
        app.changed(edit=res.get("relinked", False), full=True)
        return res

    def post_story_apply(self, b):
        app = self.app
        p = app.require_project(transcript=True)
        with app.lock:
            n = p.apply_story(b["analysis"], b["variant"])
        app.changed(full=True)
        return {"pieces": n}

    def post_story_unlink(self, b):
        app = self.app
        p = app.require_project()
        with app.lock:
            p.checkpoint("unlink")
            p.data["edit"]["story_link"] = None
        app.changed(edit=False, full=True)

    def post_analysis_delete(self, b):
        app = self.app
        p = app.require_project()
        with app.lock:
            p.checkpoint("delete_analysis")
            p.data["analyses"] = [a for a in p.data["analyses"] if a["id"] != b["analysis"]]
            link = p.data["edit"].get("story_link")
            if link and link["analysis"] == b["analysis"]:
                p.data["edit"]["story_link"] = None
        app.changed(edit=False, full=True)

    def post_settings(self, b):
        self.app.settings.update(b)
        return {"settings": self.app.settings.public()}

    def post_proxy(self, b):
        app = self.app
        p = app.require_project()
        clip = p.data["clips"][int(b.get("clip", 0))]

        def run(job):
            media.make_proxy(clip["path"], cancel=lambda: job.cancelled,
                             progress=lambda x: app.job_update(job, progress=x, message="Створення проксі для перегляду"))
            app.emit("proxy", clip=int(b.get("clip", 0)))

        return app.start_job("proxy", "Проксі для в'ювера", run)

    def post_probe(self, b):
        p = self.app.require_project()
        out = []
        for c in p.data["clips"]:
            try:
                info = media.probe(c["path"])
                out.append({"playable": media.browser_playable(info) or bool(media.existing_proxy(c["path"])),
                            "info": info, "proxy": bool(media.existing_proxy(c["path"]))})
            except Exception as e:
                out.append({"playable": False, "error": str(e)})
        return {"clips": out}


def make_server(app, host="127.0.0.1", port=8765):
    handler = type("BoundHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server
