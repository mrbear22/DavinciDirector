"""Parakeet ASR: chunking of long audio, word timestamps, boundaries, errors (no model needed)."""

import numpy as np
import pytest

from davinci_director import parakeet

SR = parakeet.SR


def _audio(seconds, quiet_at=()):
    rng = np.random.default_rng(1)
    a = (rng.standard_normal(int(seconds * SR)) * 0.3).astype(np.float32)
    for t in quiet_at:  # a short pause inside continuous speech
        a[int(t * SR):int((t + 0.1) * SR)] *= 0.001
    return a


def _check_partition(chunks, total):
    assert chunks[0]["c0"] == 0 and chunks[-1]["c1"] == total
    for x, y in zip(chunks, chunks[1:]):
        assert x["c1"] == y["c0"], "cores must touch: no gap, no overlap"
    for c in chunks:
        assert c["a"] <= c["c0"] or c["a"] < c["c1"]
        assert (c["b"] - c["a"]) / SR <= 20.0 + 1e-6, "model was trained on clips up to 20 s"


def test_pauses_become_cuts_and_cores_cover_everything():
    audio = _audio(60)
    regions = [(1 * SR, 6 * SR), (7 * SR, 12 * SR), (30 * SR, 40 * SR), (41 * SR, 55 * SR)]
    chunks = parakeet.plan_chunks(audio, regions)
    _check_partition(chunks, len(audio))
    # 1-6 and 7-12 share a chunk (short pause), the 18 s silence starts a new one, 30-40 + 41-55 > 19 s
    assert [(c["a"] / SR, c["b"] / SR) for c in chunks] == [(0.5, 12.5), (29.5, 40.5), (40.5, 55.5)]
    assert chunks[0]["c1"] == 21 * SR  # middle of the 12-30 s pause


def test_long_speech_is_cut_at_its_quietest_moment_with_overlap():
    audio = _audio(50, quiet_at=[16.0, 33.0])
    chunks = parakeet.plan_chunks(audio, [(0, 50 * SR)])
    _check_partition(chunks, len(audio))
    cuts = [c["c1"] / SR for c in chunks[:-1]]
    assert len(cuts) == 2 and 16.0 <= cuts[0] <= 16.1 and 33.0 <= cuts[1] <= 33.1  # inside the 0.1 s pauses
    for x, y in zip(chunks, chunks[1:]):  # both neighbours hear the cut point with 0.5 s to spare
        assert x["b"] - x["c1"] == y["c0"] - y["a"] == int(parakeet.OVERLAP * SR)


def test_boundary_word_is_kept_exactly_once():
    """The same spoken word recognised by both overlapping chunks: only the core with its middle keeps it."""
    w_a = {"t0": 15.8, "t1": 16.3, "w": "слово", "p": 1.0}   # as heard by chunk 1 (ends at 16.5)
    w_b = {"t0": 15.81, "t1": 16.29, "w": "слово", "p": 1.0}  # as heard by chunk 2 (starts at 15.5)
    kept = parakeet.keep_core([w_a], 0.0, 16.0) + parakeet.keep_core([w_b], 16.0, 33.0)
    assert len(kept) == 1 and kept[0] is w_b
    edge = {"t0": 15.9, "t1": 16.1, "w": "x", "p": 1.0}  # middle exactly on the cut: belongs to the right chunk
    assert parakeet.keep_core([edge], 0.0, 16.0) == [] and parakeet.keep_core([edge], 16.0, 33.0) == [edge]


def test_tokens_to_words():
    toks = [(" При", 0.10, 0.20), ("віт", 0.20, 0.36), (",", 0.36, 0.36), (" як", 0.50, 0.62),
            (" справи", 0.70, 0.98), ("?", 0.98, 0.98), (" Wi", 1.2, 1.3), ("-Fi", 1.3, 1.5)]
    ws = parakeet.tokens_to_words(toks, offset=10.0)
    assert [w["w"] for w in ws] == ["Привіт,", "як", "справи?", "Wi-Fi"]
    assert ws[0]["t0"] == pytest.approx(10.10) and ws[0]["t1"] == pytest.approx(10.36)
    assert all(w["t1"] > w["t0"] for w in ws)
    assert parakeet.tokens_to_words([("▁ну", 0, 0)], 0)[0] == {"t0": 0.0, "t1": 0.02, "w": "ну", "p": 1.0}


class FakeModel:
    """Recognises one word in the middle of every chunk; can run out of GPU memory once."""

    def __init__(self, oom_once=False, fail=None):
        self.oom_once, self.fail, self.devices = oom_once, fail, []

    def recognize(self, x):
        self.devices.append(parakeet._state["device"])
        if self.fail:
            raise self.fail
        if self.oom_once and parakeet._state["device"] == "cuda":
            self.oom_once = False
            raise RuntimeError("CUDA out of memory. Tried to allocate 2.00 GiB")
        mid = len(x) / SR / 2
        return [(" слово", mid - 0.2, mid + 0.2)]


def _fake(monkeypatch, model, regions):
    monkeypatch.setattr(parakeet, "speech_regions", lambda audio: regions)
    monkeypatch.setattr(parakeet, "_cuda", lambda: True)
    monkeypatch.setattr(parakeet, "_is_oom", lambda e: "out of memory" in str(e))

    def fake_load(path, device):
        parakeet._state.update(path=path, device=device, dtype=None, processor=None, model=None)

    monkeypatch.setattr(parakeet, "_load", fake_load)
    monkeypatch.setattr(parakeet, "_recognize", model.recognize)
    parakeet._state.clear()


def test_transcribe_long_audio(monkeypatch):
    audio = _audio(120)
    model = FakeModel()
    _fake(monkeypatch, model, [(int(s * SR), int((s + 8) * SR)) for s in range(0, 112, 10)])
    seen = []
    words = parakeet.transcribe(audio, "dir", progress=seen.append)
    assert len(words) == len(parakeet.plan_chunks(audio, parakeet.speech_regions(audio)))
    assert [w["t0"] for w in words] == sorted(w["t0"] for w in words)
    assert seen[-1] == 1.0 and model.devices == ["cuda"] * len(words)


def test_gpu_out_of_memory_continues_on_cpu(monkeypatch):
    model = FakeModel(oom_once=True)
    _fake(monkeypatch, model, [(SR, 5 * SR), (30 * SR, 35 * SR)])
    log = []
    words = parakeet.transcribe(_audio(40), "dir", log=log.append)
    assert len(words) == 2 and model.devices == ["cuda", "cpu", "cpu"]
    assert "процесорі" in log[0]


def test_errors_are_explained(monkeypatch):
    _fake(monkeypatch, FakeModel(fail=ValueError("bad shape")), [(SR, 5 * SR)])
    with pytest.raises(parakeet.AsrError) as e:
        parakeet.transcribe(_audio(10), "dir")
    assert "Помилка розпізнавання на 0:00" in str(e.value) and "bad shape" in str(e.value)
    _fake(monkeypatch, FakeModel(fail=MemoryError()), [(SR, 5 * SR)])
    with pytest.raises(parakeet.AsrError) as e:
        parakeet.transcribe(_audio(10), "dir")
    assert "оперативної пам'яті" in str(e.value)
    assert "оперативної пам'яті" in str(parakeet._load_error(MemoryError()))
    assert "видаліть модель" in str(parakeet._load_error(OSError("model.safetensors: invalid header")))


def test_cancel(monkeypatch):
    _fake(monkeypatch, FakeModel(), [(SR, 5 * SR)])
    with pytest.raises(parakeet.Cancelled):
        parakeet.transcribe(_audio(10), "dir", cancel=lambda: True)


def test_model_is_kept_between_runs(monkeypatch):
    loads = []
    _fake(monkeypatch, FakeModel(), [(SR, 5 * SR)])
    real = parakeet._load
    monkeypatch.setattr(parakeet, "_load", lambda p, d: loads.append(d) or real(p, d))
    parakeet.transcribe(_audio(10), "dir")
    parakeet.transcribe(_audio(10), "dir")
    assert loads == ["cuda"]
    parakeet.release()
    assert parakeet._state == {}
