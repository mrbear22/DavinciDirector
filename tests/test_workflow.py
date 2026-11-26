"""End-to-end workflow against the mock Resolve over real HTTP."""

import json
import threading
import time
import urllib.request

import pytest

from davinci_director.bridge import BridgeClient, DirectClient, MockResolveApp, serve_bridge
from davinci_director.claude_ai import ClaudeService
from davinci_director.server import App, make_server
from davinci_director.storage import Settings

from conftest import ANALYSIS, FakeClaude, text_msg, tool_msg


class Client:
    def __init__(self, port):
        self.base = "http://127.0.0.1:%d" % port

    def get(self, path, raw=False, headers=None):
        req = urllib.request.Request(self.base + path, headers=headers or {})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
            return (data, r) if raw else json.loads(data)

    def post(self, path, body=None, expect=200):
        req = urllib.request.Request(self.base + path, data=json.dumps(body or {}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == expect:
                return json.loads(e.read())
            raise AssertionError("%s -> %s %s" % (path, e.code, e.read()))


def wait_job(app, kind, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        jobs = [j for j in app.jobs.values() if j.kind == kind]
        if jobs and jobs[-1].status != "running":
            return jobs[-1]
        time.sleep(0.05)
    raise AssertionError("job %s timed out" % kind)


@pytest.fixture
def env(video, fake_asr):
    mock = MockResolveApp([(video, 60.0)])
    # run through the real bridge protocol (as inside Resolve)
    ready = threading.Event()
    port_box = {}
    stop = threading.Event()

    def on_ready(port):
        port_box["port"] = port
        ready.set()

    t = threading.Thread(target=serve_bridge, args=(mock, 0, "tok"),
                         kwargs={"should_stop": stop.is_set, "on_ready": on_ready}, daemon=True)
    t.start()
    ready.wait(5)
    resolve = BridgeClient("http://127.0.0.1:%d" % port_box["port"], "tok")
    settings = Settings()
    settings.update({"diarize": True, "hf_token": "x"})
    fake = FakeClaude([])
    app = App(resolve, settings, ClaudeService(settings, client_factory=lambda: fake))
    server = make_server(app, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield {"app": app, "c": Client(server.server_address[1]), "mock": mock, "fake": fake}
    server.shutdown()
    stop.set()


def test_full_workflow(env):
    app, c, mock, fake = env["app"], env["c"], env["mock"], env["fake"]
    st = c.post("/api/connect")
    assert st["state"]["project"]["clips"][0]["media_fps"] == 30.0
    assert st["state"]["resolve"]["ok"]

    # 1. transcription (real FFmpeg extraction + loudness, fake Whisper)
    c.post("/api/transcribe", {})
    job = wait_job(app, "transcribe")
    assert job.status == "done", job.message
    state = c.get("/api/state")
    proj = state["project"]
    assert len(proj["speakers"]) == 2
    words = [t for t in proj["tokens"] if t["k"] == "w"]
    assert words[0]["w"] == "Привіт"
    assert any(t["hl"] >= 2 for t in proj["tokens"] if 40 <= t["r0"] <= 42), "loud moment detected"
    assert abs(state["render"]["duration"] - 60.0) < 0.01

    # 2. text-based editing
    pieces = proj["edit"]["pieces"]
    first = pieces[0]
    r = c.post("/api/edit", {"op": "cut", "args": {"sel": [{"pid": first["id"], "a": first["a"], "b": first["a"] + 2}]}})
    assert r["render"]["duration"] < 60
    r = c.post("/api/edit", {"op": "move", "args": {"pids": [pieces[3]["id"]], "before": pieces[0]["id"]}})
    assert r["edit"]["pieces"][0]["id"] == pieces[3]["id"]
    r = c.post("/api/undo")
    assert r["edit"]["pieces"][0]["id"] == pieces[0]["id"]
    c.post("/api/speakers", {"action": "rename", "id": "S1", "name": "Стрімер"})
    c.post("/api/words", {"changes": [{"i": words[0]["i"], "w": "Хай"}]})
    assert app.project.tokens[words[0]["i"]]["w"] == "Хай"

    # 3. sync to Resolve timeline (also exercises endFrame calibration)
    mock.project.inclusive = 0  # pretend this Resolve treats endFrame exclusively
    res = c.post("/api/sync")
    tl = mock.project.current
    assert tl.name.endswith("DD Edit")
    total = sum(i.GetDuration() for i in tl.items)
    expected = sum(f["rec_dur"] for f in app.project.render_frames())
    assert total == expected, (total, expected)
    assert app.project.data["settings"]["end_inclusive"] is False
    n_timelines = len(mock.project.timelines)
    c.post("/api/sync")
    assert len(mock.project.timelines) == n_timelines, "old edit timeline replaced"

    # 4. jump to text in Resolve
    tok = words[10]["i"]
    j = c.post("/api/jump", {"token": tok})
    assert j["timeline"] == "edit"
    ph = c.get("/api/playhead")
    assert ph["timeline"] == "edit" and abs(ph["token"] - tok) <= 1
    j = c.post("/api/jump", {"token": tok, "target": "source"})
    assert j["timeline"] == "source"

    # 5. Claude analysis -> structure -> apply -> timeline
    fake.responses.append(text_msg(json.dumps(ANALYSIS, ensure_ascii=False)))
    c.post("/api/ai/analyze", {"instructions": "смішний ролик", "variants": 1})
    job = wait_job(app, "analyze")
    assert job.status == "done", job.message
    req = fake.requests[-1]
    assert req["model"] == "claude-opus-5-5"
    assert req["output_config"]["format"]["type"] == "json_schema"
    assert req["messages"][0]["content"][0]["cache_control"]["type"] == "ephemeral"
    assert "[00:00:" in req["messages"][0]["content"][0]["text"]
    an = app.project.data["analyses"][0]
    v = an["variants"][0]
    assert [s["role"] for s in v["scenes"]] == ["hook", "setup", "resolution"]
    c.post("/api/story/apply", {"analysis": an["id"], "variant": v["id"]})
    assert app.project.pieces[0]["scene"] == v["scenes"][0]["id"]
    dur = app.project.edit_duration()
    assert 10 < dur < 20
    c.post("/api/story", {"analysis": an["id"], "variant": v["id"], "action": "delete_scene", "scene": v["scenes"][2]["id"]})
    assert app.project.edit_duration() < dur
    c.post("/api/sync")
    marks = mock.project.current.GetMarkers()
    assert any("HOOK" in m["name"] for m in marks.values())
    assert any(m["name"] == "VO" for m in marks.values())

    # 6. chat with tool call that rebuilds the story
    new_variant = dict(ANALYSIS["variants"][0], title="Інший початок")
    fake.responses += [tool_msg({"replace_variant_id": "", "variant": new_variant}), text_msg("Готово: новий варіант.")]
    c.post("/api/ai/chat", {"text": "Знайди кращий початок"})
    job = wait_job(app, "chat")
    assert job.status == "done", job.message
    chat = app.project.data["chat"]
    assert chat[-1]["role"] == "assistant" and chat[-1]["variants"]
    assert any(v2["title"] == "Інший початок" for a in app.project.data["analyses"] for v2 in a["variants"])
    last_req = fake.requests[-1]
    assert last_req["tools"][0]["name"] == "save_story_variant"
    assert last_req["messages"][-1]["content"][0]["type"] == "tool_result"
    # api history is append-only and ends with an assistant turn
    assert app.project.data["chat_api"][-1]["role"] == "assistant"

    # 7. exports + media streaming
    srt, _ = c.get("/api/export/srt", raw=True)
    assert b"-->" in srt
    edl, _ = c.get("/api/export/edl", raw=True)
    assert b"FROM CLIP NAME" in edl
    md, _ = c.get("/api/export/md", raw=True)
    assert "HOOK".encode() in md
    data, resp = c.get("/media/0", raw=True, headers={"Range": "bytes=0-99"})
    assert resp.status == 206 and len(data) == 100

    # 8. persistence: reconnect loads the saved project
    app.saver.flush()
    app.project = None
    c.post("/api/connect")
    assert app.project.data["analyses"] and app.project.data["speakers"]["S1"]["name"] == "Стрімер"


def test_import_srt(env):
    app, c = env["app"], env["c"]
    c.post("/api/connect")
    srt = "1\n01:00:01,000 --> 01:00:03,000\nПривіт усім\n\n2\n01:00:10,000 --> 01:00:12,500\nДругий: ну поїхали\n"
    r = c.post("/api/import_transcript", {"text": srt, "format": "srt"})
    assert r["words"] == 4
    assert len(app.project.data["speakers"]) == 2
    toks = [t for t in app.project.tokens if t["k"] == "w"]
    assert abs(toks[0]["r0"] - 1.0) < 0.01
    assert abs(app.project.edit_duration() - 60) < 0.01


def test_rejects_foreign_origin(env):
    c = env["c"]
    req = urllib.request.Request(c.base + "/api/state", headers={"Origin": "http://evil.example"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 403


def test_direct_client_mock(video):
    client = DirectClient(MockResolveApp([(video, 60.0)]))
    info = client.call("read_timeline")
    assert info["clips"][0]["src_out"] == 1800
