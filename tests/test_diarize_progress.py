"""Speaker detection reports its own progress (it can take minutes) and can be cancelled."""

import pytest

from davinci_director import transcribe


def test_hook_maps_pyannote_steps_to_progress():
    seen = []
    hook = transcribe._diar_hook(lambda x, step: seen.append((round(x, 3), step)), None)
    hook("segmentation", None, file={}, total=10, completed=5)
    hook("segmentation", object())             # step finished (no counters)
    hook("speaker_counting", object())
    hook("embeddings", None, total=4, completed=0)
    hook("embeddings", None, total=4, completed=2)
    hook("embeddings", object())
    hook("unknown_step", object())             # ignored
    hook("discrete_diarization", object())
    xs = [x for x, _ in seen]
    assert xs == sorted(xs) and xs[-1] == 1.0
    assert seen[0] == (0.125, "пошук мовлення") and ("голосові відбитки" in s for _, s in seen)
    assert len(seen) == 7


def test_whisper_leaves_the_gpu_before_speaker_detection(tmp_path, monkeypatch):
    """Recognition of all ranges first, then Whisper is unloaded, then pyannote; progress only goes up."""
    from davinci_director import components, media, models
    calls, prog = [], []
    monkeypatch.setattr(components, "ensure", lambda *a, **k: False)
    monkeypatch.setattr(components, "nvidia_gpu", lambda: False)
    monkeypatch.setattr(models, "ensure_asr", lambda *a, **k: "model-dir")
    monkeypatch.setattr(transcribe, "check_hf_access", lambda token: None)
    monkeypatch.setattr(media, "extract_audio", lambda p, a, b, **k: "%s-%s.wav" % (a, b))

    def fake_asr(wav, model_path, use_gpu=True, **k):
        assert model_path == "model-dir"
        calls.append(("asr", wav))
        k["progress"](1.0)
        return [{"t0": 1.0, "t1": 1.5, "w": "слово", "p": 1.0}], "uk"

    def fake_diarize(wav, token, n, progress=None, cancel=None):
        calls.append(("diar", wav))
        progress(0.5, "голосові відбитки")
        return [(0.0, 5.0, "SPEAKER_00")]

    monkeypatch.setattr(transcribe, "asr_words", fake_asr)
    monkeypatch.setattr(transcribe, "diarize", fake_diarize)
    monkeypatch.setattr(transcribe, "release_asr", lambda: calls.append(("release_asr",)))
    monkeypatch.setattr(transcribe, "release_diarization", lambda: calls.append(("release_diar",)))
    media_path = tmp_path / "clip.mp4"
    media_path.write_bytes(b"x")
    saved = []

    class Engine:
        track = "V1"
        transcripts = {}

        def media_in_track(self):
            return [{"path": str(media_path), "key": "k", "name": "clip", "mfps": 25, "ranges": [[0, 10], [20, 30]]}]

        def set_transcript(self, tr):
            saved.append(tr)

    from davinci_director.storage import Settings
    monkeypatch.setenv("DD_HOME", str(tmp_path / "home"))
    settings = Settings()  # the real class (its get() takes only a key)
    settings.data.update({"diarize": True, "hf_token": "t"})
    gpu = []
    real_fake = fake_asr
    monkeypatch.setattr(transcribe, "asr_words", lambda *a, **k: gpu.append(a[2]) or real_fake(*a, **k))
    transcribe.transcribe_track(Engine(), settings, {}, progress=lambda x, m: prog.append(x))
    assert gpu == [True, True]  # GPU by default
    assert [c[0] for c in calls] == ["asr", "asr", "release_asr", "diar", "diar", "release_diar"]
    assert prog == sorted(prog) and prog[-1] == 1.0
    assert len(saved) == 1 and len(saved[0].words) == 2
    assert saved[0].meta["model"] == "parakeet-tdt-0.6b-v3-uk"
    # words are stored against the source file: the second range (20-30 s, padded by 1 s) starts at 19 s
    assert [w[0] for w in saved[0].words] == [1.0, 20.0]


def test_placeholder_words_are_dropped():
    for junk in ("______________.", "—", "___", "...", "_"):
        assert transcribe._NO_TEXT.match(junk), junk
    for word in ("Дякую.", "Wi-Fi", "100%", "м'ясо", "a"):
        assert not transcribe._NO_TEXT.match(word), word


def test_hook_cancels():
    hook = transcribe._diar_hook(None, lambda: True)
    with pytest.raises(transcribe.Cancelled):
        hook("embeddings", None, total=4, completed=1)
