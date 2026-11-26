import copy
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def dd_home(tmp_path, monkeypatch):
    monkeypatch.setenv("DD_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return tmp_path / "home"


@pytest.fixture(scope="session")
def video(tmp_path_factory):
    """60 s test video: colour bars + a tone that is loud between 40-42 s."""
    out = tmp_path_factory.mktemp("media") / "stream.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30:duration=60",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=60",
        "-filter_complex", "[1:a]volume='if(between(t,40,42),1.0,0.05)':eval=frame[a]",
        "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(out)], check=True)
    return str(out)


SCRIPT = [
    # (start, speaker, text) — synthetic "stream"
    (1.0, "A", "Привіт чат, сьогодні ми нарешті йдемо на боса."),
    (6.0, "A", "Ну що, перевіряємо зброю, все готово."),
    (12.0, "B", "Слухай, а ти взяв зілля? Без зілля нам кінець."),
    (17.0, "A", "Звичайно взяв. Ну, майже."),
    (24.0, "A", "Окей, заходимо. Тихо тихо тихо."),
    (31.0, "B", "Він нас бачить! Біжи!"),
    (40.5, "A", "НІ НІ НІ! Як він це зробив?!"),
    (46.0, "B", "Ха-ха-ха, я ж казав про зілля!"),
    (52.0, "A", "Добре, чат, це був провал. Спробуємо ще раз."),
]


def fake_words(offset=0.0, end=None):
    words = []
    for start, sp, text in SCRIPT:
        t = start
        for w in text.split():
            d = 0.25 + 0.04 * len(w)
            if end is None or t + d <= end:
                words.append({"t0": t - offset, "t1": t + d - offset, "w": w, "p": 0.9, "label": sp})
            t += d + 0.08
    return words


@pytest.fixture
def fake_asr(monkeypatch):
    from davinci_director import transcribe

    def whisper_words(wav, model_name=None, language=None, device=None, compute_type=None,
                      progress=None, cancel=None, duration=None):
        ws = [dict(w) for w in fake_words()]
        for w in ws:
            w.pop("label")
        if progress:
            progress(1.0)
        return ws, "uk"

    def diarize(wav, token, n=0, progress=None):
        turns = []
        for start, sp, text in SCRIPT:
            turns.append((start - 0.1, start + 0.33 * len(text.split()) + 0.5, sp))
        return turns

    monkeypatch.setattr(transcribe, "whisper_words", whisper_words)
    monkeypatch.setattr(transcribe, "diarize", diarize)


# ---------------------------------------------------------------- fake Claude

class _Block(SimpleNamespace):
    def model_dump(self, exclude_none=False):
        return {k: v for k, v in self.__dict__.items() if not (exclude_none and v is None)}


class FakeStream:
    def __init__(self, msg):
        self.msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        for b in self.msg.content:
            if b.type == "text":
                yield SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="text_delta", text=b.text))

    def get_final_message(self):
        return self.msg


class FakeClaude:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kw):
        self.requests.append(copy.deepcopy(kw))
        r = self.responses.pop(0)
        return FakeStream(r)


def text_msg(text, stop="end_turn"):
    return SimpleNamespace(content=[_Block(type="text", text=text)], stop_reason=stop, model="claude-opus-5-5",
                           usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0,
                                                 cache_creation_input_tokens=0), stop_details=None)


def tool_msg(tool_input):
    return SimpleNamespace(content=[_Block(type="text", text="Перебудовую сюжет."),
                                    _Block(type="tool_use", id="tu_1", name="save_story_variant", input=tool_input)],
                           stop_reason="tool_use", model="claude-opus-5-5",
                           usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0,
                                                 cache_creation_input_tokens=0), stop_details=None)


ANALYSIS = {
    "overview": "Стрімер іде на боса без зілля і програє.",
    "moments": [{"start": "00:00:40.0", "end": "00:00:44.0", "type": "fail", "description": "Бос вбиває", "strength": 5}],
    "variants": [{
        "title": "Без зілля нам кінець", "logline": "Як забуте зілля зруйнувало план",
        "why_it_works": "Чіткий сетап і пейоф", "tone": "комедія", "estimated_duration": "~40 с",
        "hook_options": [{"start": "00:00:40.0", "end": "00:00:43.5", "why": "крик"}],
        "scenes": [
            {"role": "hook", "title": "НІ НІ НІ", "description": "Крик", "ranges": [{"start": "00:00:40.3", "end": "00:00:43.6"}],
             "edit_notes": "різкий старт", "techniques": ["zoom"], "voiceover": []},
            {"role": "setup", "title": "Зілля", "description": "Друг питає про зілля",
             "ranges": [{"start": "00:00:11.8", "end": "00:00:19.5"}], "edit_notes": "", "techniques": [],
             "voiceover": [{"at": "00:00:11.8", "text": "За п'ять хвилин до катастрофи…"}]},
            {"role": "resolution", "title": "Я ж казав", "description": "Пейоф",
             "ranges": [{"start": "00:00:45.8", "end": "00:00:49.0"}], "edit_notes": "", "techniques": [], "voiceover": []},
        ],
        "weak_parts": [{"start": "00:00:06.0", "end": "00:00:10.0", "reason": "нудно"}],
        "missing_context": [{"at": "00:00:24.0", "problem": "хто бос?", "fix": "титр"}],
        "ending": "фінальна репліка"}],
}


@pytest.fixture
def fake_claude():
    return FakeClaude([text_msg(json.dumps(ANALYSIS, ensure_ascii=False))])
