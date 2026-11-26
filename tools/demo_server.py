"""Demo/UI-test server: mock Resolve timeline + synthetic transcript + scripted Claude.

    python tools/demo_server.py VIDEO [--port 8765]

No Resolve, Whisper model or API key needed — used for UI development and
automated browser tests.  Not part of the add-on runtime.
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tests"))

import conftest  # noqa: E402  (synthetic script + fake Claude)
from davinci_director import transcribe  # noqa: E402
from davinci_director.bridge import DirectClient, MockResolveApp  # noqa: E402
from davinci_director.claude_ai import ClaudeService  # noqa: E402
from davinci_director.media import probe  # noqa: E402
from davinci_director.server import App, make_server  # noqa: E402
from davinci_director.storage import Settings  # noqa: E402


class LoopingClaude(conftest.FakeClaude):
    def _stream(self, **kw):
        self.requests.append(kw)
        if kw.get("output_config", {}).get("format"):
            return conftest.FakeStream(conftest.text_msg(json.dumps(conftest.ANALYSIS, ensure_ascii=False)))
        last = kw["messages"][-1]["content"][0]
        if last.get("type") == "tool_result":
            return conftest.FakeStream(conftest.text_msg("Готово — зберіг новий варіант. Він починається з крику о 00:00:40.3, далі сетап про зілля."))
        if "початок" in last.get("text", ""):
            v = dict(conftest.ANALYSIS["variants"][0], title="Старт з провалу")
            return conftest.FakeStream(conftest.tool_msg({"replace_variant_id": "", "variant": v}))
        return conftest.FakeStream(conftest.text_msg(
            "**Сильні моменти:**\n- 00:00:40.5 — крик «НІ НІ НІ!»\n- 00:00:46.0 — «я ж казав про зілля»\n\nРаджу почати з крику, а потім повернутися до сетапу."))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()

    def whisper_words(wav, **kw):
        ws = [dict(w) for w in conftest.fake_words()]
        for w in ws:
            w.pop("label")
        return ws, "uk"

    transcribe.whisper_words = whisper_words
    transcribe.diarize = lambda wav, tok, n=0, progress=None: [(s - 0.1, s + 0.33 * len(t.split()) + 0.5, sp) for s, sp, t in conftest.SCRIPT]
    settings = Settings()
    settings.update({"anthropic_api_key": "demo", "hf_token": "demo"})
    fake = LoopingClaude([])
    path = os.path.abspath(a.video)
    app = App(DirectClient(MockResolveApp([(path, probe(path)["duration"])])), settings,
              ClaudeService(settings, client_factory=lambda: fake))
    app.connect()
    server = make_server(app, port=a.port)
    print("demo on http://127.0.0.1:%d/" % a.port, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
