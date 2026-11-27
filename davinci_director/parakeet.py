"""Speech recognition: Parakeet TDT 0.6B v3 UK (https://huggingface.co/ihnatov-yuri/parakeet-tdt-0.6b-v3-uk).

Model: Yuri Ihnatov (https://ihnatov.nl), fine-tuned from NVIDIA Parakeet TDT 0.6B v3 (CC BY 4.0,
https://creativecommons.org/licenses/by/4.0/).  Used unchanged.

Inference the way the model card does it: Transformers ``AutoProcessor`` + ``AutoModelForTDT`` on
torch.  Word timestamps come from the TDT token durations (``processor.decode(..., durations=...)``).

Long recordings: the model was trained on 1–20 s clips, so the audio is cut into chunks of at most
~20 s.  Cuts are made in pauses found by the Silero VAD (ONNX, from the "audio" component); a stretch of speech longer than that is cut at
its quietest 20 ms frame and both neighbours get 0.5 s of overlap.  Every chunk owns a "core" interval;
the cores partition the timeline without gaps, and a recognised word is kept only by the chunk whose core
contains the word's middle.  So a word at a boundary is neither lost (the overlap gives both chunks the
whole word) nor duplicated (only one core contains its middle).
"""

import os

from . import Cancelled

REPO = "ihnatov-yuri/parakeet-tdt-0.6b-v3-uk"
NAME = "parakeet-tdt-0.6b-v3-uk"
LABEL = "Parakeet TDT 0.6B v3 UK"
CREDIT = ("Yuri Ihnatov (https://ihnatov.nl), fine-tuned from NVIDIA Parakeet TDT 0.6B v3 (CC BY 4.0)")
LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"
FILES = ["config.json", "generation_config.json", "model.safetensors", "processor_config.json",
         "tokenizer.json", "tokenizer_config.json"]

SR = 16000
MAX_SPEECH = 19.0     # seconds of speech per chunk (+ overlap stays within the 20 s the model was trained on)
MIN_SPLIT = 12.0      # a forced cut inside long speech is searched in [start + 12 s, start + 19 s]
OVERLAP = 0.5         # seconds added on both sides of a chunk
JOIN_GAP = 2.0        # speech pieces closer than this go into the same chunk (if it stays short enough)


class AsrError(Exception):
    pass


# ------------------------------------------------------------------ chunking (numpy only)

_vad = []


def speech_regions(audio):
    """[(start, end)] in samples where someone speaks (Silero VAD)."""
    import torch
    from silero_vad import get_speech_timestamps, load_silero_vad
    if not _vad:
        _vad.append(load_silero_vad(onnx=True))  # ONNX: ~2x faster than its torch version on the processor
    regs = get_speech_timestamps(torch.from_numpy(audio), _vad[0], sampling_rate=SR,
                                 min_silence_duration_ms=500, speech_pad_ms=200)
    return [(int(r["start"]), int(r["end"])) for r in regs]


def _quietest(audio, a, b, frame=320):
    """Sample index of the quietest 20 ms frame in [a, b)."""
    import numpy as np  # numpy comes with the downloaded components, not with the app itself
    n = (b - a) // frame
    if n <= 1:
        return (a + b) // 2
    seg = audio[a:a + n * frame].astype(np.float32).reshape(n, frame)
    i = int(np.argmin((seg * seg).mean(axis=1)))
    return a + i * frame + frame // 2


def plan_chunks(audio, regions):
    """Chunks as dicts {"a", "b"} (audio window, samples) and {"c0", "c1"} (core, samples)."""
    total = len(audio)
    max_s, min_s = int(MAX_SPEECH * SR), int(MIN_SPLIT * SR)
    # 1) speech spans of at most MAX_SPEECH, cut at the quietest point inside long speech
    pieces = []  # (start, end, hard_cut_before)
    for s, e in regions:
        hard = False
        while e - s > max_s:
            cut = _quietest(audio, s + min_s, s + max_s)
            pieces.append((s, cut, hard))
            s, hard = cut, True
        pieces.append((s, e, hard))
    # 2) pack neighbouring pieces into one chunk while it stays short and the pause between is short
    spans = []  # [start, end, hard_cut_before]
    for s, e, hard in pieces:
        if spans and not hard and e - spans[-1][0] <= max_s and s - spans[-1][1] <= JOIN_GAP * SR:
            spans[-1][1] = e
        else:
            spans.append([s, e, hard])
    # 3) cores partition [0, total): a cut in a pause sits in its middle, a forced cut exactly at the cut
    chunks = []
    ov = int(OVERLAP * SR)
    for i, (s, e, hard) in enumerate(spans):
        c0 = 0 if i == 0 else chunks[-1]["c1"]
        if i + 1 < len(spans):
            ns, _, nhard = spans[i + 1]
            c1 = ns if nhard else (e + ns) // 2
        else:
            c1 = total
        chunks.append({"a": max(0, s - ov), "b": min(total, e + ov), "c0": c0, "c1": c1})
    return chunks


def keep_core(words, c0, c1):
    """Words whose middle lies in [c0, c1) seconds."""
    return [w for w in words if c0 <= (w["t0"] + w["t1"]) / 2 < c1]


def tokens_to_words(tokens, offset):
    """TDT tokens [(text, start, end)] (seconds in the chunk) -> words; a token starting with a space
    (or SentencePiece's '▁') starts a new word, punctuation sticks to the word before it."""
    words, cur = [], None
    for text, start, end in tokens:
        piece = text.replace("▁", " ")
        if cur is None or piece.startswith(" "):
            if cur and cur["w"].strip():
                words.append(cur)
            cur = {"t0": offset + float(start), "t1": offset + float(end), "w": piece.strip(), "p": 1.0}
        else:
            cur["w"] += piece
            cur["t1"] = max(cur["t1"], offset + float(end))
    if cur and cur["w"].strip():
        words.append(cur)
    for w in words:
        w["t1"] = max(w["t1"], w["t0"] + 0.02)
    return words


# ------------------------------------------------------------------ model

_state = {}  # path, device, dtype, processor, model
OOM_TEXTS = ("out of memory", "not enough memory")


def _is_oom(e):
    return isinstance(e, MemoryError) or type(e).__name__ == "OutOfMemoryError" or \
        any(t in str(e).lower() for t in OOM_TEXTS)


def _load(path, device):
    import torch
    from transformers import AutoModelForTDT, AutoProcessor
    dtype = torch.float16 if device == "cuda" else torch.float32
    proc = AutoProcessor.from_pretrained(path)
    model = AutoModelForTDT.from_pretrained(path, dtype=dtype).to(device).eval()
    _state.update(path=path, device=device, dtype=dtype, processor=proc, model=model)


def load(path, device=None, log=None):
    """Load once and keep it for the next transcriptions: on the graphics card when there is one
    (and it has the memory), otherwise on the processor."""
    import torch
    device = device or ("cuda" if torch.cuda.is_available() else "cpu")
    if _state.get("path") == path and _state.get("device") == device:
        return
    release()
    try:
        _load(path, device)
    except Exception as e:
        if device == "cuda" and _is_oom(e):
            if log:
                log("Не вистачило пам'яті відеокарти для моделі розпізнавання — працюю на процесорі (повільніше).")
            release()
            return load(path, "cpu", log)
        if _is_oom(e):
            raise AsrError("Не вистачає оперативної пам'яті, щоб завантажити модель розпізнавання (потрібно ~3 ГБ "
                           "вільної). Закрийте інші програми й спробуйте ще раз.")
        raise AsrError("Не вдалося завантажити модель %s: %s. Якщо файли пошкоджені, видаліть модель у "
                       "«Компоненти й моделі» — програма завантажить її знову." % (LABEL, e))


def release():
    """Free the model (and its GPU memory), e.g. before speaker detection."""
    import gc
    import sys
    _state.clear()
    gc.collect()
    torch = sys.modules.get("torch")
    if torch is not None and torch.cuda.is_available():
        torch.cuda.empty_cache()


def _recognize(x):
    import torch
    proc, model = _state["processor"], _state["model"]
    inp = proc(x, sampling_rate=SR, return_tensors="pt").to(_state["device"], _state["dtype"])
    with torch.inference_mode():
        out = model.generate(**inp, return_dict_in_generate=True)
    _, stamps = proc.decode(out.sequences, durations=out.durations)
    return [(d["token"], d["start"], d["end"]) for d in stamps[0]]


def _recognize_chunk(x, at, log):
    """Tokens of one chunk; when the graphics card runs out of memory, the rest goes on the processor."""
    try:
        return _recognize(x)
    except Exception as e:
        if not _is_oom(e):
            raise AsrError("Помилка розпізнавання на %d:%02d: %s" % (at // 60, at % 60, e))
        if _state.get("device") != "cuda":
            raise AsrError("Не вистачає оперативної пам'яті для розпізнавання. Закрийте інші програми й спробуйте ще раз.")
    if log:
        log("Не вистачило пам'яті відеокарти — розпізнавання продовжується на процесорі (повільніше).")
    load(_state["path"], "cpu", log)
    return _recognize(x)


def transcribe(audio, path, progress=None, cancel=None, log=None):
    """16 kHz mono float32 audio -> [{"t0","t1","w","p"}] (seconds from the start of ``audio``).
    ``p`` is always 1.0: the model gives no per-word confidence through Transformers."""
    load(path, log=log)
    if not len(audio):
        return []
    chunks = plan_chunks(audio, speech_regions(audio))
    words = []
    for i, ch in enumerate(chunks):
        if cancel and cancel():
            raise Cancelled()
        x = audio[ch["a"]:ch["b"]]
        if len(x) >= SR // 10:  # shorter: nothing a model could read
            toks = _recognize_chunk(x, ch["a"] // SR, log)
            words += keep_core(tokens_to_words(toks, ch["a"] / SR), ch["c0"] / SR, ch["c1"] / SR)
        if progress:
            progress((i + 1) / len(chunks))
    return words


def model_dir_ok(path):
    return all(os.path.isfile(os.path.join(path, f)) for f in FILES)
