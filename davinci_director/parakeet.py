"""Speech recognition: Parakeet TDT 0.6B v3 UK (https://huggingface.co/ihnatov-yuri/parakeet-tdt-0.6b-v3-uk).

Model: Yuri Ihnatov (https://ihnatov.nl), fine-tuned from NVIDIA Parakeet TDT 0.6B v3 (CC BY 4.0,
https://creativecommons.org/licenses/by/4.0/).  Used unchanged.

Inference the way the model card does it: Transformers ``AutoProcessor`` + ``AutoModelForTDT`` on
torch.  Word timestamps come from the TDT token durations (``processor.decode(..., durations=...)``).

Long recordings: the model was trained on 1–20 s clips, so the audio is cut into chunks of at most
~20 s.  Cuts are made in pauses found by the Silero VAD; a stretch of speech longer than that is cut at
its quietest 20 ms frame and both neighbours get 0.5 s of overlap.  Every chunk owns a "core" interval;
the cores partition the timeline without gaps, and a recognised word is kept only by the chunk whose core
contains the word's middle.  So a word at a boundary is neither lost (the overlap gives both chunks the
whole word) nor duplicated (only one core contains its middle).
"""

import gc
import os

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


class Cancelled(Exception):
    pass


# ------------------------------------------------------------------ chunking (numpy only)

def speech_regions(audio):
    """[(start, end)] in samples where someone speaks (Silero VAD, as shipped with faster-whisper)."""
    from faster_whisper.vad import VadOptions, get_speech_timestamps
    regs = get_speech_timestamps(audio, VadOptions(min_silence_duration_ms=500, speech_pad_ms=200))
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


class _NoRuntime(Exception):
    pass


def _load(path, device):
    try:
        import torch
        from transformers import AutoModelForTDT, AutoProcessor
    except (ImportError, OSError) as e:  # OSError: a torch DLL failed to load
        raise _NoRuntime(e)
    dtype = torch.float16 if device == "cuda" else torch.float32
    proc = AutoProcessor.from_pretrained(path)
    model = AutoModelForTDT.from_pretrained(path, dtype=dtype).to(device).eval()
    _state.update(path=path, device=device, dtype=dtype, processor=proc, model=model)


def load(path, use_gpu=True, log=None):
    """Load once and keep it for the next transcriptions (GPU if available and wanted)."""
    if _state.get("path") == path and (_state.get("device") == "cuda") == bool(use_gpu and _cuda()):
        return _state["device"]
    release()
    device = "cuda" if use_gpu and _cuda() else "cpu"
    try:
        _load(path, device)
    except _NoRuntime as e:
        raise AsrError("Не вдалося запустити рушій розпізнавання (torch/transformers): %s. "
                       "Видаліть компонент «нейромережі» у «Компоненти й моделі» — програма встановить його знову."
                       % e.args[0])
    except Exception as e:
        if device == "cuda" and _is_oom(e):
            release()
            if log:
                log("Не вистачило пам'яті відеокарти для моделі розпізнавання — працюю на процесорі (повільніше).")
            device = "cpu"
            try:
                _load(path, device)
            except Exception as e2:
                raise _load_error(e2)
        else:
            raise _load_error(e)
    return device


def _cuda():
    try:
        import torch
        return torch.cuda.is_available()
    except Exception:
        return False


def _is_oom(e):
    import torch
    return isinstance(e, (torch.cuda.OutOfMemoryError, MemoryError)) or "out of memory" in str(e).lower()


def _load_error(e):
    if isinstance(e, MemoryError) or "out of memory" in str(e).lower() or "not enough memory" in str(e).lower():
        return AsrError("Не вистачає оперативної пам'яті, щоб завантажити модель розпізнавання (потрібно ~3 ГБ вільної). "
                        "Закрийте інші програми й спробуйте ще раз.")
    return AsrError("Не вдалося завантажити модель %s: %s. Якщо файли пошкоджені, видаліть модель у "
                    "«Компоненти й моделі» — програма завантажить її знову." % (LABEL, e))


def release():
    """Free the model (and its GPU memory), e.g. before speaker detection."""
    _state.clear()
    gc.collect()
    try:
        import sys
        torch = sys.modules.get("torch")
        if torch is not None and torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def _recognize(x):
    import torch
    proc, model = _state["processor"], _state["model"]
    inp = proc(x, sampling_rate=SR, return_tensors="pt").to(_state["device"], _state["dtype"])
    with torch.inference_mode():
        out = model.generate(**inp, return_dict_in_generate=True)
    _, stamps = proc.decode(out.sequences, durations=out.durations)
    return [(d["token"], d["start"], d["end"]) for d in stamps[0]]


def transcribe(audio, path, use_gpu=True, progress=None, cancel=None, log=None):
    """16 kHz mono float32 audio -> [{"t0","t1","w","p"}] (seconds from the start of ``audio``).
    ``p`` is always 1.0: the model gives no per-word confidence through Transformers."""
    load(path, use_gpu, log)
    if not len(audio):
        return []
    chunks = plan_chunks(audio, speech_regions(audio))
    words = []
    for i, ch in enumerate(chunks):
        if cancel and cancel():
            raise Cancelled()
        x = audio[ch["a"]:ch["b"]]
        if len(x) < SR // 10:  # nothing a model could read
            continue
        try:
            toks = _recognize(x)
        except Exception as e:
            if _state.get("device") == "cuda" and _is_oom(e):
                if log:
                    log("Не вистачило пам'яті відеокарти — розпізнавання продовжується на процесорі (повільніше).")
                load_path = _state["path"]
                release()
                load(load_path, False, log)
                toks = _recognize(x)
            elif isinstance(e, MemoryError):
                raise AsrError("Не вистачає оперативної пам'яті для розпізнавання. Закрийте інші програми й спробуйте ще раз.")
            else:
                raise AsrError("Помилка розпізнавання на %d:%02d: %s" % (ch["a"] // SR // 60, ch["a"] // SR % 60, e))
        got = tokens_to_words(toks, ch["a"] / SR)
        words += keep_core(got, ch["c0"] / SR, ch["c1"] / SR)
        if progress:
            progress((i + 1) / len(chunks))
    return words


def model_dir_ok(path):
    return all(os.path.isfile(os.path.join(path, f)) for f in FILES)
