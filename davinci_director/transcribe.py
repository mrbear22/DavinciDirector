"""Speech-to-text with word timestamps, and speaker detection.

* recognition: Parakeet TDT 0.6B v3 UK (see ``parakeet``);
* speakers: pyannote.audio (needs a Hugging Face token with the model's terms accepted).
  Without it every word gets speaker S1 and the editor assigns speakers by hand.
"""

import gc
import re
import sys
import time
import wave

from . import Cancelled, components, media, models, parakeet

SR = parakeet.SR
DIAR_MODEL = "pyannote/speaker-diarization-community-1"
GATED_HELP = ("Hugging Face не дає доступу до моделі визначення мовців ({why}).\n"
              "1) Відкрийте https://hf.co/" + DIAR_MODEL + " і прийміть умови (кнопка Agree / Access repository).\n"
              "2) Токен: тип «Read» (найпростіше) або у fine-grained токені увімкніть "
              "«Read access to contents of all public gated repos you can access».")
# share of the time per second of audio: extraction, recognition, speaker detection
SHARES_WITH_SPEAKERS, SHARES_TEXT_ONLY = (0.05, 0.55, 0.40), (0.1, 0.9, 0.0)
# share of the diarization time per pyannote step (embeddings dominate, especially on the processor)
DIAR_STEPS = {"segmentation": (0.0, 0.25, "пошук мовлення"), "speaker_counting": (0.25, 0.3, "підрахунок мовців"),
              "embeddings": (0.3, 0.97, "голосові відбитки"), "discrete_diarization": (0.97, 1.0, "розмітка")}
NO_TEXT = re.compile(r"^[\W_]*$")  # "_" is a word character for regex, «______» must still go
SENT_END = re.compile(r"[.!?…]+[\"»”')\]]*$")
JITTER_WORDS, JITTER_SEC, JOIN_GAP = 3, 1.0, 0.6  # see smooth_speakers


class TranscriptionError(Exception):
    pass


def read_wav(path):
    """16 kHz mono PCM WAV made by media.extract_audio -> float32 array."""
    import numpy as np
    with wave.open(path, "rb") as w:
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return data.astype(np.float32) / 32768.0


def _free_gpu():
    gc.collect()
    torch = sys.modules.get("torch")
    if torch is not None and torch.cuda.is_available():
        torch.cuda.empty_cache()


# ------------------------------------------------------------------ recognition


def asr_words(wav_path, model_path, progress=None, cancel=None, log=None):
    """[{"t0", "t1", "w", "p"}] in seconds from the start of the WAV."""
    words = parakeet.transcribe(read_wav(wav_path), model_path, progress=progress, cancel=cancel, log=log)
    return [w for w in words if not NO_TEXT.match(w["w"])]


# ------------------------------------------------------------------ speakers

_diar_pipeline = {}  # token -> loaded pipeline


def check_hf_access(token):
    """Fail fast (before a long transcription) when the speaker model cannot be downloaded with this
    token, and say exactly what to fix."""
    if not token:
        raise TranscriptionError("Для визначення мовців потрібен токен Hugging Face (поле в діалозі транскрибування).")
    from huggingface_hub import HfApi, hf_hub_download
    try:
        hf_hub_download(DIAR_MODEL, "config.yaml", token=token)
        return
    except Exception as e:  # the type differs between huggingface_hub versions: classify by the message
        text = "%s %s" % (type(e).__name__, e)
    if "401" in text or "Unauthorized" in text or "Invalid" in text:
        raise TranscriptionError("Токен Hugging Face недійсний — створіть новий: https://hf.co/settings/tokens")
    if "Gated" not in text and "403" not in text:
        raise TranscriptionError("Не вдалося завантажити модель мовців (немає інтернету?): %s" % text[:200])
    raise TranscriptionError(GATED_HELP.format(why=_gated_reason(HfApi(token=token))))


def _gated_reason(api):
    """Why a valid token gets 403: a fine-grained token without access to gated repositories is the usual cause."""
    try:
        tok = (api.whoami().get("auth") or {}).get("accessToken") or {}
    except Exception as e:  # only a more precise wording is lost; the instructions stay the same
        print("Hugging Face whoami: %r" % e)
        tok = {}
    if tok.get("role") == "fineGrained" and "repo.content.read" not in ((tok.get("fineGrained") or {}).get("global") or []):
        return "у fine-grained токені не ввімкнено доступ до gated-репозиторіїв"
    return "модель закрита умовами використання"


def release_diarization():
    """Give the speaker pipeline's GPU memory back (the recognition model loads next)."""
    _diar_pipeline.clear()
    _free_gpu()


def _pipeline(token):
    import torch
    from pyannote.audio import Pipeline
    if token not in _diar_pipeline:
        _diar_pipeline.clear()
        pipeline = Pipeline.from_pretrained(DIAR_MODEL, token=token)
        if torch.cuda.is_available():
            pipeline.to(torch.device("cuda"))
        _diar_pipeline[token] = pipeline
    return _diar_pipeline[token]


def _diar_hook(progress, cancel):
    """pyannote calls hook(step, artefact, file=..., total=..., completed=...) along the way."""
    def hook(step, artefact=None, file=None, total=None, completed=None, **kw):
        if cancel and cancel():
            raise Cancelled()
        if progress and step in DIAR_STEPS:
            a, b, name = DIAR_STEPS[step]
            frac = (completed or 0) / total if total else 1.0
            progress(a + (b - a) * min(1.0, frac), name)
    return hook


def diarize(wav_path, hf_token, num_speakers=0, progress=None, cancel=None):
    """[(start, end, label)]; ``progress(x, step)`` gets 0..1 and the current step name."""
    import torch
    check_hf_access(hf_token)
    pipeline = _pipeline(hf_token)
    inp = {"waveform": torch.from_numpy(read_wav(wav_path)).unsqueeze(0), "sample_rate": SR}
    kwargs = {"num_speakers": int(num_speakers)} if num_speakers else {}
    out = pipeline(inp, hook=_diar_hook(progress, cancel), **kwargs)
    ann = getattr(out, "exclusive_speaker_diarization", None) or out.speaker_diarization
    if progress:
        progress(1.0, "готово")
    return [(float(t.start), float(t.end), str(label)) for t, _, label in ann.itertracks(yield_label=True)]


def _best_turn(w, turns, j):
    """Label of the turn overlapping word ``w`` the most (searched around index ``j``), else the nearest."""
    best, best_ov = None, 0.0
    for s, e, lab in turns[max(0, j - 2):j + 4]:
        ov = min(e, w["t1"]) - max(s, w["t0"])
        if ov > best_ov:
            best, best_ov = lab, ov
    if best is not None:
        return best
    near = turns[max(0, j - 1):j + 2] or turns
    return min(near, key=lambda t: min(abs(t[0] - w["t0"]), abs(t[1] - w["t1"])))[2]


def assign_speakers(words, turns):
    """Sets w["sp"] = "S1", "S2", ... (in order of appearance); returns those ids."""
    if not turns:
        for w in words:
            w["sp"] = "S1"
        return ["S1"]
    turns = sorted(turns)
    labels, j = [], 0
    for w in words:
        while j < len(turns) and turns[j][1] < w["t0"]:
            j += 1
        w["sp"] = _best_turn(w, turns, j)
        if w["sp"] not in labels:
            labels.append(w["sp"])
    ids = {lab: "S%d" % (n + 1) for n, lab in enumerate(labels)}
    for w in words:
        w["sp"] = ids[w["sp"]]
    return [ids[lab] for lab in labels]


def _runs(words):
    """[[start, end)] of consecutive words with the same speaker."""
    runs, start = [], 0
    for i in range(1, len(words) + 1):
        if i == len(words) or words[i]["sp"] != words[start]["sp"]:
            runs.append([start, i])
            start = i
    return runs


def _run_len(words, i, step, limit):
    """Length (capped at ``limit``) of the speaker run of word i, counted from i in direction ``step``."""
    sp, n = words[i]["sp"], 0
    while 0 <= i < len(words) and words[i]["sp"] == sp and n < limit:
        n += 1
        i += step
    return n


def _jitter_target(words, a, z, size):
    """Speaker a short run [a, z) should join, or None when it stands apart (a real short reply)."""
    near_l = a > 0 and words[a]["t0"] - words[a - 1]["t1"] < JOIN_GAP and _run_len(words, a - 1, -1, size + 1) > size
    near_r = z < len(words) and words[z]["t0"] - words[z - 1]["t1"] < JOIN_GAP and _run_len(words, z, 1, size + 1) > size
    if near_l and near_r and words[a - 1]["sp"] == words[z]["sp"]:
        return words[a - 1]["sp"]
    if not (near_l or near_r):
        return None
    gap_l = words[a]["t0"] - words[a - 1]["t1"] if near_l else float("inf")
    gap_r = words[z]["t0"] - words[z - 1]["t1"] if near_r else float("inf")
    return words[a - 1]["sp"] if gap_l <= gap_r else words[z]["sp"]


def _absorb_jitter(words):
    """A run of up to 3 words and under 1 s inside continuous speech joins its neighbour.  Shortest runs
    first (1 word, then 2, then 3); a run only joins a longer neighbour it is not separated from by a pause."""
    for size in range(1, JITTER_WORDS + 1):
        for _ in range(2):
            changed = False
            for a, z in _runs(words):
                if z - a != size or words[z - 1]["t1"] - words[a]["t0"] >= JITTER_SEC:
                    continue
                if a and words[a - 1]["sp"] == words[a]["sp"]:
                    continue  # merged with its left neighbour earlier in this pass
                sp = _jitter_target(words, a, z, size)
                if sp is not None:
                    for w in words[a:z]:
                        w["sp"] = sp
                    changed = True
            if not changed:
                break


def _ends_sentence(w):
    return bool(SENT_END.search(w.get("w") or ""))


def _snap_to_sentence_ends(words):
    """A change of speaker 1–2 words away from a sentence end moves onto it
    («… я це | зробив?» -> «… я це зробив? |»)."""
    runs = _runs(words)
    for k in range(1, len(runs)):
        a0, a1 = runs[k - 1]
        b0, b1 = runs[k]
        if _ends_sentence(words[a1 - 1]):
            continue
        cut = next((j + 1 for j in (a1 - 2, a1 - 3) if j >= a0 and _ends_sentence(words[j])), None)
        if cut is not None:  # the sentence ended before the change: its tail belongs to the next speaker
            for w in words[cut:a1]:
                w["sp"] = words[b0]["sp"]
        else:  # the sentence ends just after the change: those words belong to the previous speaker
            cut = next((j + 1 for j in (b0, b0 + 1) if j < b1 - 1 and _ends_sentence(words[j])), None)
            if cut is None:
                continue
            for w in words[b0:cut]:
                w["sp"] = words[a1 - 1]["sp"]
        runs[k - 1][1] = runs[k][0] = cut


def smooth_speakers(words):
    """Fix diarization jitter on word level: pyannote turns rarely match word edges exactly."""
    if len(words) >= 2:
        _absorb_jitter(words)
        _snap_to_sentence_ends(words)


# ------------------------------------------------------------------ ranges (seconds of the source file)


def _merge_ranges(ranges, pad=1.0, join=3.0):
    out = []
    for a, b in sorted((max(0.0, a - pad), b + pad) for a, b in ranges):
        if out and a <= out[-1][1] + join:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _cut(part, hole):
    """What is left of interval ``part`` after removing ``hole``."""
    (x, y), (c0, c1) = part, hole
    if c1 <= x or c0 >= y:
        return [[x, y]]
    return [q for q in ([x, c0], [c1, y]) if q[1] > q[0]]


def _subtract(ranges, covered, min_len=0.5):
    """Parts of ``ranges`` not yet ``covered`` (pieces shorter than ``min_len`` s are not worth it)."""
    out = []
    for r in ranges:
        parts = [r]
        for hole in covered:
            parts = [q for p in parts for q in _cut(p, hole)]
        out += [p for p in parts if p[1] - p[0] > min_len]
    return out


# ------------------------------------------------------------------ whole track


class _Meter:
    """One progress bar over many files: positions are seconds of audio processed."""

    def __init__(self, total, progress):
        self.total, self.progress, self.done = total or 1.0, progress, 0.0

    def report(self, extra, msg):
        if self.progress:
            self.progress((self.done + extra) / self.total, msg)

    def stage(self, offset, share, span, msg):
        """progress(x[, step]) for a stage covering ``share`` of ``span`` seconds, after ``offset`` of it."""
        return lambda x, step="": self.report((offset + share * x) * span,
                                              "%s (%s)" % (msg, step) if step else msg)


def _plan(engine, opts):
    """[(media, ranges to transcribe, existing transcript)] of the edit track."""
    medias = engine.media_in_track()
    if not medias:
        raise TranscriptionError("На доріжці %s немає кліпів з медіафайлами." % engine.track)
    plan = []
    for m in medias:
        existing = engine.transcripts.get(m["key"])
        ranges = [[0.0, media.duration(m["path"])]] if opts.get("whole") else _merge_ranges(m["ranges"])
        if existing and not opts.get("force"):
            ranges = _subtract(ranges, existing.covered)
        if ranges:
            plan.append((m, ranges, existing))
    if not plan:
        raise TranscriptionError("Усе, що є на доріжці, вже транскрибовано. Позначте «заново», щоб перерахувати.")
    return plan


def _recognize_media(m, ranges, model_path, meter, shares, cancel, log):
    """Pass 1 for one file: words of every range, and its audio for pass 2."""
    words, pieces = [], []
    for a, b in ranges:
        span = b - a
        wav = media.extract_audio(m["path"], a, b, cancel=cancel,
                                  progress=meter.stage(0, shares[0], span, "Аудіо: %s" % m["name"]))
        ws = asr_words(wav, model_path, progress=meter.stage(shares[0], shares[1], span, "Розпізнавання: %s" % m["name"]),
                       cancel=cancel, log=log)
        for w in ws:
            w["t0"] += a
            w["t1"] += a
        words += ws
        pieces.append((a, span, wav, bool(ws)))
        meter.done += (shares[0] + shares[1]) * span
    return words, pieces


def _speaker_turns(m, pieces, settings, opts, meter, share, cancel, log):
    """Pass 2 for one file.  Returns turns, or None when speakers could not be detected."""
    turns = []
    for a, span, wav, has_words in pieces:
        if has_words:
            try:
                found = diarize(wav, settings.get("hf_token"), opts.get("speakers") or settings.get("num_speakers"),
                                progress=meter.stage(0, share, span, "Мовці: %s" % m["name"]), cancel=cancel)
            except TranscriptionError as e:
                if log:
                    log("Мовців не визначено: " + str(e))
                return None
            turns += [(s + a, e + a, lab) for s, e, lab in found]
        meter.done += share * span
    return turns


def _merged(new, speakers, ranges, existing):
    """New words of ``ranges`` put into the existing transcript (its speaker ids are kept)."""
    keep = [w for w in existing.words if not any(a <= (w[0] + w[1]) / 2 < b for a, b in ranges)]
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
    covered = _merge_ranges(existing.covered + [list(r) for r in ranges], pad=0, join=0)
    return sorted(keep + new, key=lambda w: w[0]), sp, covered


def _transcript(m, words, turns, ranges, existing, force):
    from .transcript import Transcript
    labels = assign_speakers(words, turns or [])
    smooth_speakers(words)
    speakers = [{"id": lab, "name": "Мовець %s" % lab[1:]} for lab in labels]
    index = {s["id"]: i for i, s in enumerate(speakers)}
    new = [[round(w["t0"], 3), round(max(w["t1"], w["t0"] + 0.02), 3), w["w"], index.get(w["sp"], 0),
            round(w.get("p", 1.0), 3)] for w in words]
    covered = [list(r) for r in ranges]
    if existing and not force:
        new, speakers, covered = _merged(new, speakers, ranges, existing)
    return Transcript(m["path"], new, speakers, lang="uk", covered=covered,
                      meta={"model": models.ASR, "fps": m["mfps"], "created": time.strftime("%Y-%m-%d %H:%M")})


def _components(progress, cancel):
    components.ensure("audio", progress, cancel)
    components.ensure("speakers", progress, cancel)


def transcribe_track(engine, settings, opts, progress=None, cancel=None, log=None):
    """Transcribe the media used on the edit track.  Words are stored against the source file (seconds
    from its start), so the transcript survives any later timeline edit."""
    _components(progress, cancel)
    model_path = models.ensure_asr(progress, cancel)
    plan = _plan(engine, opts)
    want_speakers = opts.get("diarize", settings.get("diarize"))
    if want_speakers:
        if progress:
            progress(0.0, "Перевірка доступу до моделі мовців")
        check_hf_access(settings.get("hf_token"))  # before the long recognition, not after
    shares = SHARES_WITH_SPEAKERS if want_speakers else SHARES_TEXT_ONLY
    meter = _Meter(sum(b - a for _, rs, _ in plan for a, b in rs), progress)
    for m, ranges, existing in plan:
        start = meter.done
        words, pieces = _recognize_media(m, ranges, model_path, meter, shares, cancel, log)
        turns = None
        if want_speakers and words:
            parakeet.release()  # next to Resolve the graphics card has little memory for two models
            turns = _speaker_turns(m, pieces, settings, opts, meter, shares[2], cancel, log)
            release_diarization()
            want_speakers = turns is not None  # a failure (token, network) is not retried for every file
        meter.done = start + sum(b - a for a, b in ranges)
        engine.set_transcript(_transcript(m, words, turns, ranges, existing, opts.get("force")))
    if progress:
        progress(1.0, "Готово")


def diarize_track(engine, settings, opts, progress=None, cancel=None):
    """Detect speakers for transcripts that already exist (no new recognition)."""
    from .transcript import Transcript, segment
    _components(progress, cancel)
    check_hf_access(settings.get("hf_token"))
    parakeet.release()  # a recognition model left from an earlier transcription would only take GPU memory
    jobs = [(m, engine.transcripts[m["key"]]) for m in engine.media_in_track() if m["key"] in engine.transcripts]
    if not jobs:
        raise TranscriptionError("Спочатку транскрибуйте таймлайн.")
    meter = _Meter(sum(b - a for _, tr in jobs for a, b in tr.covered), progress)
    for m, tr in jobs:
        turns = []
        for a, b in tr.covered:
            span = b - a
            wav = media.extract_audio(m["path"], a, b, cancel=cancel,
                                      progress=meter.stage(0, 0.1, span, "Аудіо: %s" % m["name"]))
            found = diarize(wav, settings.get("hf_token"), opts.get("speakers") or settings.get("num_speakers"),
                            progress=meter.stage(0.1, 0.9, span, "Мовці: %s" % m["name"]), cancel=cancel)
            turns += [(t0 + a, t1 + a, lab) for t0, t1, lab in found]
            meter.done += span
        words = [{"t0": w[0], "t1": w[1], "w": w[2]} for w in tr.words]
        labels = assign_speakers(words, turns)
        smooth_speakers(words)
        renamed = {sp["id"]: sp["name"] for sp in tr.speakers if not sp["name"].startswith("Мовець")}
        speakers = [{"id": lab, "name": renamed.get(lab) or "Мовець %s" % lab[1:]} for lab in labels]
        index = {sp["id"]: i for i, sp in enumerate(speakers)}
        new_words = [[w[0], w[1], w[2], index.get(nw["sp"], 0)] + list(w[4:]) for w, nw in zip(tr.words, words)]
        meta = dict(tr.meta, diarized=time.strftime("%Y-%m-%d %H:%M"), speakers_found=len(labels))
        engine.set_transcript(Transcript(tr.path, new_words, speakers, frags=segment(new_words), lang=tr.lang,
                                         covered=tr.covered, meta=meta))
    release_diarization()
    if progress:
        progress(1.0, "Готово")
