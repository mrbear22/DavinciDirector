"""Speech-to-text with word timestamps and speaker diarization.

* ASR: Parakeet TDT 0.6B v3 UK (see ``parakeet``) – word timestamps from the TDT token durations.
* Diarization: pyannote.audio (optional, needs a Hugging Face token with the
  model license accepted).  Without it every word gets speaker S1 and the
  user can assign speakers in the editor.
* Fallback import: SRT/VTT subtitles (e.g. exported from Resolve's own
  "Create Subtitles from Audio") or Whisper JSON.
"""

import json
import os
import time
import re


class TranscriptionError(Exception):
    pass


class Cancelled(Exception):
    pass


# ------------------------------------------------------------------ ASR


def _read_wav(path):
    """16 kHz mono PCM WAV (made by media.extract_audio) -> float32 array (no decoder library needed)."""
    import wave
    import numpy as np
    with wave.open(path, "rb") as w:
        if w.getsampwidth() != 2 or w.getframerate() != 16000:
            return path
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        if w.getnchannels() > 1:
            data = data.reshape(-1, w.getnchannels()).mean(axis=1)
    return data.astype(np.float32) / 32768.0


def release_asr():
    """Drop the loaded recognition model so its GPU memory is free for speaker detection."""
    from . import parakeet
    parakeet.release()


def release_diarization():
    """Drop the speaker-detection pipeline and give its GPU memory back."""
    _diar_pipeline.clear()
    import gc
    gc.collect()
    try:
        import sys
        torch = sys.modules.get("torch")
        if torch is not None and torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


_NO_TEXT = re.compile(r"^[\W_]*$")  # "_" counts as a word character in regex, «______» must still go


def asr_words(wav_path, model_path, use_gpu=True, progress=None, cancel=None, log=None):
    """Return ([{"t0","t1","w","p"}] in seconds relative to the WAV start, language)."""
    from . import parakeet
    audio = _read_wav(wav_path)
    if isinstance(audio, str):
        raise TranscriptionError("Непідтримуваний WAV для розпізнавання: %s" % wav_path)
    try:
        words = parakeet.transcribe(audio, model_path, use_gpu, progress=progress, cancel=cancel, log=log)
    except parakeet.Cancelled:
        raise Cancelled()
    except parakeet.AsrError as e:
        raise TranscriptionError(str(e))
    return [w for w in words if not _NO_TEXT.match(w["w"])], "uk"


# ------------------------------------------------------------------ diarization

DIAR_MODEL = "pyannote/speaker-diarization-community-1"

GATED_HELP = ("Hugging Face не дає доступу до моделі визначення мовців ({why}).\n"
              "1) Відкрийте https://hf.co/" + DIAR_MODEL + " і прийміть умови (кнопка Agree / Access repository).\n"
              "2) Токен: тип «Read» (найпростіше) або у fine-grained токені увімкніть "
              "«Read access to contents of all public gated repos you can access».")


def check_hf_access(token):
    """Fail fast (before a long transcription) when the diarization model is not
    downloadable with this token; explain exactly what to fix."""
    if not token:
        raise TranscriptionError("Для визначення мовців потрібен токен Hugging Face (поле в діалозі транскрибування).")
    try:
        from huggingface_hub import HfApi, hf_hub_download
    except ImportError:
        raise TranscriptionError("pyannote.audio не встановлено: python install.py --diarization")
    try:
        hf_hub_download(DIAR_MODEL, "config.yaml", token=token)
        return
    except Exception as e:
        text = "%s %s" % (type(e).__name__, e)
    if "401" in text or "Unauthorized" in text or "Invalid" in text:
        raise TranscriptionError("Токен Hugging Face недійсний — створіть новий: https://hf.co/settings/tokens")
    if "Gated" in text or "403" in text:
        why = "модель закрита умовами використання"
        try:
            tok = (HfApi(token=token).whoami().get("auth") or {}).get("accessToken") or {}
            if tok.get("role") == "fineGrained" and "repo.content.read" not in ((tok.get("fineGrained") or {}).get("global") or []):
                why = "у fine-grained токені не ввімкнено доступ до gated-репозиторіїв"
        except Exception:
            pass
        raise TranscriptionError(GATED_HELP.format(why=why))
    raise TranscriptionError("Не вдалося завантажити модель мовців (немає інтернету?): %s" % text[:200])


_diar_pipeline = {}


# share of the diarization time per pyannote step (embeddings dominate, especially on the CPU build of torch)
_DIAR_STEPS = {"segmentation": (0.0, 0.25), "speaker_counting": (0.25, 0.3), "embeddings": (0.3, 0.97),
               "discrete_diarization": (0.97, 1.0)}
_DIAR_NAMES = {"segmentation": "пошук мовлення", "speaker_counting": "підрахунок мовців",
               "embeddings": "голосові відбитки", "discrete_diarization": "розмітка"}


def _diar_hook(progress, cancel):
    """pyannote calls hook(step, artefact, file=..., total=..., completed=...) along the way."""
    def hook(step, artefact=None, file=None, total=None, completed=None, **kw):
        if cancel and cancel():
            raise Cancelled()
        if progress and step in _DIAR_STEPS:
            a, b = _DIAR_STEPS[step]
            frac = (completed or 0) / total if total else 1.0
            progress(a + (b - a) * min(1.0, frac), _DIAR_NAMES[step])
    return hook


def diarize(wav_path, hf_token, num_speakers=0, progress=None, cancel=None):
    """Return [(start, end, label)] or raise TranscriptionError.
    ``progress(x, step)`` gets 0..1 and the current step name while pyannote works."""
    try:
        from pyannote.audio import Pipeline
        import torch
    except (ImportError, OSError):  # OSError: a DLL of torch failed to load
        raise TranscriptionError("Визначення мовців недоступне: pyannote.audio не встановлено "
                                 "(у збірці — build.py --diarization, з вихідного коду — install.py --diarization).")
    check_hf_access(hf_token)
    pipeline = _diar_pipeline.get(hf_token)
    if pipeline is None:
        try:
            try:
                pipeline = Pipeline.from_pretrained(DIAR_MODEL, token=hf_token)
            except TypeError:  # pyannote 3.x
                pipeline = Pipeline.from_pretrained(DIAR_MODEL, use_auth_token=hf_token)
        except Exception as e:
            raise TranscriptionError("Не вдалося завантажити pyannote: %s" % e)
        if pipeline is None:
            raise TranscriptionError(GATED_HELP.format(why="модель не завантажилась"))
        try:
            if torch.cuda.is_available():
                pipeline.to(torch.device("cuda"))
        except Exception:
            pass
        _diar_pipeline.clear()
        _diar_pipeline[hf_token] = pipeline
    audio = _read_wav(wav_path)
    if isinstance(audio, str):
        raise TranscriptionError("Непідтримуваний WAV для визначення мовців")
    inp = {"waveform": torch.from_numpy(audio).unsqueeze(0), "sample_rate": 16000}
    kwargs = {"num_speakers": int(num_speakers)} if num_speakers else {}
    try:
        out = pipeline(inp, hook=_diar_hook(progress, cancel), **kwargs)
    except TypeError as e:  # a pipeline without hook support
        if "hook" not in str(e):
            raise
        out = pipeline(inp, **kwargs)
    ann = getattr(out, "exclusive_speaker_diarization", None) or getattr(out, "speaker_diarization", out)
    turns = [(float(t.start), float(t.end), str(label)) for t, _, label in ann.itertracks(yield_label=True)]
    if progress:
        progress(1.0, "готово")
    return turns


def assign_speakers(words, turns):
    """Give each word the speaker label with the largest time overlap (or the
    nearest turn).  Returns the ordered list of labels seen."""
    if not turns:
        for w in words:
            w["sp"] = "S1"
        return ["S1"]
    turns = sorted(turns)
    order = []
    j = 0
    for w in words:
        while j < len(turns) and turns[j][1] < w["t0"]:
            j += 1
        best, best_ov = None, 0.0
        for k in range(max(0, j - 2), min(len(turns), j + 4)):
            s, e, lab = turns[k]
            ov = min(e, w["t1"]) - max(s, w["t0"])
            if ov > best_ov:
                best, best_ov = lab, ov
        if best is None:
            near = min(turns[max(0, j - 1):j + 2] or turns, key=lambda t: min(abs(t[0] - w["t0"]), abs(t[1] - w["t1"])))
            best = near[2]
        w["sp_raw"] = best
        if best not in order:
            order.append(best)
    mapping = {lab: "S%d" % (n + 1) for n, lab in enumerate(order)}
    for w in words:
        w["sp"] = mapping[w.pop("sp_raw")]
    return [mapping[o] for o in order]


_SENT_END = re.compile(r"[.!?…]+[\"»”')\]]*$")
JITTER_WORDS, JITTER_SEC, JOIN_GAP = 3, 1.0, 0.6


def _runs(words):
    runs, start = [], 0
    for i in range(1, len(words) + 1):
        if i == len(words) or words[i].get("sp") != words[start].get("sp"):
            runs.append([start, i])
            start = i
    return runs


def smooth_speakers(words, min_run=2):
    """Fix diarization jitter on word level (pyannote turns rarely match word edges exactly):

    1. a speaker run of up to 3 words and under 1 s inside continuous speech joins its neighbour —
       the same speaker on both sides wins, otherwise the side it is closer to in time.  A short run
       that stands apart (pauses on both sides, e.g. a separate «Так.») is a real reply and stays;
    2. a change of speaker 1–2 words away from the end of a sentence is moved to that sentence end
       («… я це | зробив?» -> «… я це зробив? |»)."""
    if len(words) < 2:
        return
    # shortest runs first (1 word, then 2, then 3), each in a linear pass; a run only joins a longer
    # neighbour that it is not separated from by a pause
    def side_len(i, step, limit):
        """Length (capped) of the run that word i belongs to, counted from i in direction step."""
        sp, n = words[i]["sp"], 0
        while 0 <= i < len(words) and words[i]["sp"] == sp and n < limit:
            n += 1
            i += step
        return n

    for size in range(1, JITTER_WORDS + 1):
        for _ in range(2):
            changed = False
            for a, z in _runs(words):
                if z - a != size or words[z - 1]["t1"] - words[a]["t0"] >= JITTER_SEC:
                    continue
                if words[a]["sp"] != words[z - 1]["sp"] or (a and words[a - 1]["sp"] == words[a]["sp"]):
                    continue  # this run already changed in this pass
                near_l = a > 0 and words[a]["t0"] - words[a - 1]["t1"] < JOIN_GAP and side_len(a - 1, -1, size + 1) > size
                near_r = z < len(words) and words[z]["t0"] - words[z - 1]["t1"] < JOIN_GAP and side_len(z, 1, size + 1) > size
                if near_l and near_r and words[a - 1]["sp"] == words[z]["sp"]:
                    sp = words[a - 1]["sp"]
                elif near_l or near_r:
                    gl = words[a]["t0"] - words[a - 1]["t1"] if near_l else 1e9
                    gr = words[z]["t0"] - words[z - 1]["t1"] if near_r else 1e9
                    sp = words[a - 1]["sp"] if gl <= gr else words[z]["sp"]
                else:
                    continue
                for w in words[a:z]:
                    w["sp"] = sp
                changed = True
            if not changed:
                break
    # move changes of speaker onto sentence ends nearby
    ends = lambda w: bool(_SENT_END.search(w.get("w") or ""))  # noqa: E731
    runs = _runs(words)
    for k in range(1, len(runs)):
        a0, a1 = runs[k - 1]
        b0, b1 = runs[k]
        if ends(words[a1 - 1]):
            continue
        sa, sb = words[a1 - 1]["sp"], words[b0]["sp"]
        moved = False
        for j in (a1 - 2, a1 - 3):  # sentence ended 1–2 words before the change: those words belong to B
            if j >= a0 and ends(words[j]):
                for w in words[j + 1:a1]:
                    w["sp"] = sb
                runs[k - 1][1] = runs[k][0] = j + 1
                moved = True
                break
        if moved:
            continue
        for j in (b0, b0 + 1):  # sentence ends 1–2 words after the change: those words belong to A
            if j < b1 - 1 and ends(words[j]):
                for w in words[b0:j + 1]:
                    w["sp"] = sa
                runs[k - 1][1] = runs[k][0] = j + 1
                break


# ------------------------------------------------------------------ import

_TS = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})")


def _ts(m):
    h, mi, s, ms = m.groups()
    return int(h) * 3600 + int(mi) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000.0


def parse_subtitles(text):
    """SRT/VTT -> words with timings spread proportionally to word length."""
    words = []
    blocks = re.split(r"\n\s*\n", text.replace("\r", "").strip())
    for block in blocks:
        lines = [l for l in block.split("\n") if l.strip()]
        for n, line in enumerate(lines):
            stamps = list(_TS.finditer(line))
            if "-->" in line and len(stamps) >= 2:
                t0, t1 = _ts(stamps[0]), _ts(stamps[1])
                body = " ".join(lines[n + 1:])
                body = re.sub(r"<[^>]+>", "", body)
                speaker = None
                m = re.match(r"^\s*\[?([^\]:]{1,30})\]?:\s+(.*)$", body)
                if m and len(m.group(1).split()) <= 3:
                    speaker, body = m.group(1).strip(), m.group(2)
                toks = body.split()
                if not toks:
                    break
                total = sum(len(t) + 1 for t in toks)
                cur = t0
                for t in toks:
                    d = (t1 - t0) * (len(t) + 1) / total
                    words.append({"t0": cur, "t1": cur + d, "w": t, "p": 1.0, "label": speaker})
                    cur += d
                break
    return words


def parse_whisper_json(text):
    data = json.loads(text)
    segs = data.get("segments", data if isinstance(data, list) else [])
    words = []
    for seg in segs:
        spk = seg.get("speaker")
        if seg.get("words"):
            for w in seg["words"]:
                words.append({"t0": float(w["start"]), "t1": float(w["end"]), "w": w["word"].strip(),
                              "p": float(w.get("probability", w.get("score", 1.0))), "label": w.get("speaker", spk)})
        else:
            toks = seg.get("text", "").split()
            if not toks:
                continue
            d = (seg["end"] - seg["start"]) / len(toks)
            for n, t in enumerate(toks):
                words.append({"t0": seg["start"] + n * d, "t1": seg["start"] + (n + 1) * d, "w": t, "p": 1.0, "label": spk})
    return [w for w in words if w["w"]]


def labels_to_speakers(words):
    order = []
    for w in words:
        lab = w.pop("label", None) or "Speaker"
        if lab not in order:
            order.append(lab)
        w["sp"] = "S%d" % (order.index(lab) + 1)
    return order


# ------------------------------------------------------------------ track transcription


def _merge_ranges(ranges, pad=1.0, join=3.0):
    out = []
    for a, b in sorted((max(0.0, a - pad), b + pad) for a, b in ranges):
        if out and a <= out[-1][1] + join:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _subtract(ranges, covered):
    out = []
    for a, b in ranges:
        parts = [[a, b]]
        for c0, c1 in covered:
            nxt = []
            for x, y in parts:
                if c1 <= x or c0 >= y:
                    nxt.append([x, y])
                    continue
                if c0 > x:
                    nxt.append([x, c0])
                if c1 < y:
                    nxt.append([c1, y])
            parts = nxt
        out += [p for p in parts if p[1] - p[0] > 0.5]
    return out


def transcribe_track(engine, settings, opts, progress=None, cancel=None, log=None):
    """Transcribe the media used on the edit track.  Words are stored against
    the source file (seconds from its start), so the transcript survives any
    later timeline edit."""
    from . import components, media
    from .engine import Transcript

    def comp_progress(x, msg):  # downloads show their own 0..100 %
        if progress:
            progress(x, msg)

    components.ensure("whisper", comp_progress, cancel)   # audio (PyAV/FFmpeg), voice activity detection
    components.ensure("speakers", comp_progress, cancel)  # torch + transformers (Parakeet) + pyannote
    use_gpu = True  # the graphics card whenever it can (CUDA); otherwise, or when it runs out of memory, the processor
    from . import models
    model_name = models.ASR
    try:
        model_path = models.ensure_asr(comp_progress, cancel)
    except models.ModelError as e:
        raise TranscriptionError(str(e))

    medias = engine.media_in_track()
    if not medias:
        raise TranscriptionError("На доріжці %s немає кліпів з медіафайлами." % engine.track)
    plan = []
    for m in medias:
        if not os.path.exists(m["path"]):
            raise TranscriptionError("Файл не знайдено: %s" % m["path"])
        existing = engine.transcripts.get(m["key"])
        if opts.get("whole"):
            ranges = [[0.0, media.probe(m["path"])["duration"]]]
        else:
            ranges = _merge_ranges(m["ranges"])
        if existing and not opts.get("force"):
            ranges = _subtract(ranges, existing.covered)
        if ranges:
            plan.append((m, ranges, existing))
    if not plan:
        raise TranscriptionError("Усе, що є на доріжці, вже транскрибовано. Позначте «заново», щоб перерахувати.")
    total = sum(b - a for _, rs, _ in plan for a, b in rs) or 1.0
    done = 0.0
    want_diar = opts.get("diarize", settings.get("diarize"))
    if want_diar:
        if progress:
            progress(0.0, "Перевірка доступу до моделі мовців")
        check_hf_access(settings.get("hf_token"))  # before the long transcription, not after
    # time shares of one second of audio: extraction, speech recognition, speaker detection
    share = (0.05, 0.55, 0.40) if want_diar else (0.1, 0.9, 0.0)
    for m, ranges, existing in plan:
        words, labels_turns, lang, pieces = [], [], None, []
        base = done
        # pass 1: speech recognition of every range of this file
        for a, b in ranges:
            span = b - a

            def prog_extract(x, a=a, span=span):
                if progress:
                    progress((done + share[0] * x * span) / total, "Аудіо: %s" % m["name"])

            wav = media.extract_audio(m["path"], a, b, cancel=cancel, progress=prog_extract)

            def prog_asr(x, span=span):
                if progress:
                    progress((done + (share[0] + share[1] * x) * span) / total, "Розпізнавання: %s" % m["name"])

            ws, lang = asr_words(wav, model_path, use_gpu, progress=prog_asr, cancel=cancel, log=log)
            for w in ws:
                w["t0"] += a
                w["t1"] += a
            words += ws
            pieces.append((a, span, wav, bool(ws)))
            done += (share[0] + share[1]) * span
        # pass 2: speaker detection.  The recognition model leaves the graphics card first: next to
        # DaVinci Resolve the card has little free memory for two models at once.
        if want_diar and any(p[3] for p in pieces):
            release_asr()
        for a, span, wav, has_words in pieces:
            if want_diar and has_words:
                try:
                    def prog_diar(x, step="", span=span):
                        if progress:
                            progress((done + share[2] * x * span) / total,
                                     "Мовці (%s): %s" % (step, m["name"]) if step else "Мовці: %s" % m["name"])

                    prog_diar(0.0)
                    turns = diarize(wav, settings.get("hf_token"), opts.get("speakers") or settings.get("num_speakers"),
                                    progress=prog_diar, cancel=cancel)
                    labels_turns += [(s + a, e + a, lab) for s, e, lab in turns]
                except TranscriptionError as e:
                    if log:
                        log("Мовців не визначено: " + str(e))
                    want_diar = False
            done += share[2] * span
        if want_diar:
            release_diarization()  # the next file starts with Whisper again
        done = base + sum(p[1] for p in pieces)
        labels = assign_speakers(words, labels_turns) if labels_turns else assign_speakers(words, [])
        smooth_speakers(words)
        speakers = [{"id": lab, "name": "Мовець %s" % lab[1:]} for lab in labels]
        index = {s["id"]: i for i, s in enumerate(speakers)}
        new = [[round(w["t0"], 3), round(max(w["t1"], w["t0"] + 0.02), 3), w["w"], index.get(w.get("sp"), 0),
                round(w.get("p", 1.0), 3)] for w in words]
        covered = [list(r) for r in ranges]
        if existing and not opts.get("force"):
            keep = [w for w in existing.words if not any(a <= (w[0] + w[1]) / 2 < b for a, b in ranges)]
            # keep speaker ids of the existing transcript, append new ones
            sp = list(existing.speakers)
            remap = {}
            for i, s in enumerate(speakers):
                j = next((k for k, x in enumerate(sp) if x["id"] == s["id"]), None)
                if j is None:
                    sp.append(s)
                    j = len(sp) - 1
                remap[i] = j
            for w in new:
                w[3] = remap.get(w[3], 0)
            new = sorted(keep + new, key=lambda w: w[0])
            speakers = sp
            covered = _merge_ranges(existing.covered + covered, pad=0, join=0)
        tr = Transcript(m["path"], new, speakers, lang=lang or (existing.lang if existing else None), covered=covered,
                        meta={"model": model_name, "fps": m["mfps"],
                              "created": time.strftime("%Y-%m-%d %H:%M")})
        engine.set_transcript(tr)
    if progress:
        progress(1.0, "Готово")


def diarize_track(engine, settings, opts, progress=None, cancel=None):
    """Detect speakers for transcripts that already exist (no new speech recognition)."""
    from . import components, media
    from .engine import Transcript, segment
    components.ensure("whisper", lambda x, m: progress and progress(0.0, m), cancel)  # PyAV for audio
    components.ensure("speakers", lambda x, m: progress and progress(0.0, m), cancel)
    check_hf_access(settings.get("hf_token"))
    release_asr()  # a recognition model left from an earlier transcription would only take GPU memory
    medias = [m for m in engine.media_in_track() if m["key"] in engine.transcripts]
    if not medias:
        raise TranscriptionError("Спочатку транскрибуйте таймлайн.")
    jobs = [(m, engine.transcripts[m["key"]]) for m in medias]
    total = sum(b - a for _, tr in jobs for a, b in tr.covered) or 1.0
    done = 0.0
    for m, tr in jobs:
        turns = []
        for a, b in tr.covered:
            if cancel and cancel():
                raise Cancelled()
            span = b - a
            if progress:
                progress(done / total, "Аудіо: %s" % m["name"])
            wav = media.extract_audio(m["path"], a, b, cancel=cancel)
            def prog_diar(x, step="", span=span):
                if progress:
                    progress((done + (0.1 + 0.9 * x) * span) / total,
                             "Мовці (%s): %s" % (step, m["name"]) if step else "Мовці: %s" % m["name"])

            prog_diar(0.0)
            found = diarize(wav, settings.get("hf_token"), opts.get("speakers") or settings.get("num_speakers"),
                            progress=prog_diar, cancel=cancel)
            turns += [(t0 + a, t1 + a, lab) for t0, t1, lab in found]
            done += span
        words = [{"t0": w[0], "t1": w[1], "w": w[2]} for w in tr.words]
        labels = assign_speakers(words, turns)
        smooth_speakers(words)
        old = {sp["id"]: sp["name"] for sp in tr.speakers}
        speakers = []
        for lab in labels:
            name = old.get(lab)
            if not name or name.startswith("Мовець"):
                name = "Мовець %s" % lab[1:]
            speakers.append({"id": lab, "name": name})
        index = {sp["id"]: i for i, sp in enumerate(speakers)}
        new_words = [[w[0], w[1], w[2], index.get(nw.get("sp"), 0)] + list(w[4:]) for w, nw in zip(tr.words, words)]
        meta = dict(tr.meta, diarized=time.strftime("%Y-%m-%d %H:%M"), speakers_found=len(labels))
        engine.set_transcript(Transcript(tr.path, new_words, speakers, frags=segment(new_words), lang=tr.lang,
                                         covered=tr.covered, meta=meta))
    if progress:
        progress(1.0, "Готово")
