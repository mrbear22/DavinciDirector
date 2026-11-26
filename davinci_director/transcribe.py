"""Speech-to-text with word timestamps and speaker diarization.

* ASR: faster-whisper (CTranslate2 Whisper) – ``word_timestamps=True``.
* Diarization: pyannote.audio (optional, needs a Hugging Face token with the
  model license accepted).  Without it every word gets speaker S1 and the
  user can assign speakers in the editor.
* Fallback import: SRT/VTT subtitles (e.g. exported from Resolve's own
  "Create Subtitles from Audio") or Whisper JSON.
"""

import json
import re


class TranscriptionError(Exception):
    pass


class Cancelled(Exception):
    pass


# ------------------------------------------------------------------ ASR

_model_cache = {}


def whisper_words(wav_path, model_name="large-v3", language=None, device="auto", compute_type="auto",
                  progress=None, cancel=None, duration=None):
    """Return [{"t0","t1","w","p"}] in seconds relative to the WAV start."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise TranscriptionError("faster-whisper не встановлено: pip install faster-whisper")
    key = (model_name, device, compute_type)
    if key not in _model_cache:
        try:
            _model_cache.clear()
            _model_cache[key] = WhisperModel(model_name, device=device, compute_type=compute_type)
        except Exception as e:
            raise TranscriptionError("Не вдалося завантажити модель Whisper «%s»: %s" % (model_name, e))
    model = _model_cache[key]
    segments, info = model.transcribe(
        wav_path, language=language or None, word_timestamps=True, vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500}, beam_size=5,
        condition_on_previous_text=False,  # avoids repetition loops on hours-long audio
    )
    total = duration or getattr(info, "duration", None) or 1.0
    words = []
    for seg in segments:
        if cancel and cancel():
            raise Cancelled()
        for w in seg.words or []:
            text = w.word.strip()
            if text:
                words.append({"t0": float(w.start), "t1": float(w.end), "w": text, "p": float(w.probability)})
        if progress:
            progress(min(1.0, seg.end / total))
    return words, getattr(info, "language", language)


# ------------------------------------------------------------------ diarization

def diarize(wav_path, hf_token, num_speakers=0, progress=None):
    """Return [(start, end, label)] or raise TranscriptionError."""
    try:
        from pyannote.audio import Pipeline
    except ImportError:
        raise TranscriptionError("pyannote.audio не встановлено (pip install pyannote.audio) — визначення мовців пропущено.")
    if not hf_token:
        raise TranscriptionError("Для визначення мовців потрібен Hugging Face токен (Налаштування).")
    pipeline = None
    errors = []
    for name in ("pyannote/speaker-diarization-community-1", "pyannote/speaker-diarization-3.1"):
        for kw in ({"token": hf_token}, {"use_auth_token": hf_token}):
            try:
                pipeline = Pipeline.from_pretrained(name, **kw)
                if pipeline is not None:
                    break
            except TypeError as e:
                errors.append(str(e))
            except Exception as e:  # license not accepted, offline, ...
                errors.append("%s: %s" % (name, e))
                break
        if pipeline is not None:
            break
    if pipeline is None:
        raise TranscriptionError("Не вдалося завантажити pyannote: " + "; ".join(errors[-2:]))
    try:
        import torch
        if torch.cuda.is_available():
            pipeline.to(torch.device("cuda"))
    except Exception:
        pass
    kwargs = {"num_speakers": int(num_speakers)} if num_speakers else {}
    out = pipeline(wav_path, **kwargs)
    ann = getattr(out, "speaker_diarization", out)
    turns = [(float(t.start), float(t.end), str(label)) for t, _, label in ann.itertracks(yield_label=True)]
    if progress:
        progress(1.0)
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


def smooth_speakers(words, min_run=2):
    """Remove single-word speaker flips (diarization jitter)."""
    for i in range(1, len(words) - 1):
        a, b, c = words[i - 1].get("sp"), words[i].get("sp"), words[i + 1].get("sp")
        if b != a and a == c and min_run > 1:
            words[i]["sp"] = a


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
