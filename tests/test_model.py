from davinci_director.model import Project, build_tokens, fmt_ts, parse_ts
from davinci_director import resolve_ops

from conftest import fake_words

FPS = 30.0


def clips(n=1, dur=60.0):
    out = []
    for i in range(n):
        out.append({"path": "/m/%d.mp4" % i, "media_id": "m%d" % i, "media_name": "%d.mp4" % i,
                    "media_fps": FPS, "fps": FPS, "rec_start": int(i * dur * FPS), "rec_end": int((i + 1) * dur * FPS),
                    "src_in": 0, "src_out": int(dur * FPS)})
    return out


def make_project(n=1):
    p = Project()
    p.data["resolve"] = {"fps": FPS, "timeline": "T", "timeline_id": "tl", "start_frame": 86400}
    p.data["clips"] = clips(n)
    words = {i: fake_words() for i in range(n)}
    for ws in words.values():
        for w in ws:
            w["sp"] = "S1" if w.pop("label") == "A" else "S2"
    p.set_transcript(build_tokens(p.data["clips"], words), {"S1": {"name": "A"}, "S2": {"name": "B"}})
    return p


def test_tokens_tile_clip_exactly():
    p = make_project(2)
    toks = p.tokens
    for c in range(2):
        ct = [t for t in toks if t["c"] == c]
        assert abs(ct[0]["t0"] - 0.0) < 1e-6
        assert abs(ct[-1]["t1"] - 60.0) < 1e-6
        for a, b in zip(ct, ct[1:]):
            assert abs(a["t1"] - b["t0"]) < 1e-6
    # full edit renders back to the original timeline
    segs = p.render()
    assert len(segs) == 2
    assert abs(p.edit_duration() - 120.0) < 1e-6
    assert any(t["k"] == "g" for t in toks)


def test_cut_restore_and_render():
    p = make_project()
    piece = p.pieces[0]
    word_idx = [i for i in range(piece["a"], piece["b"] + 1) if p.tokens[i]["k"] == "w"]
    target = word_idx[2]
    p.op("cut", sel=[{"pid": piece["id"], "a": target, "b": target}])
    t = p.tokens[target]
    assert abs(p.edit_duration() - (60.0 - (t["t1"] - t["t0"]))) < 1e-6
    assert len(p.render()) == 2
    p.op("restore", sel=[{"pid": piece["id"], "a": target, "b": target}])
    assert abs(p.edit_duration() - 60.0) < 1e-6
    assert p.undo() == "restore"
    assert len(p.render()) == 2
    assert p.redo() == "restore"


def test_move_duplicate_split_merge_paste():
    p = make_project()
    ids = [x["id"] for x in p.pieces]
    assert len(ids) >= 4
    p.op("move", pids=[ids[2]], before=ids[0])
    assert p.pieces[0]["id"] == ids[2]
    segs = p.render()
    assert segs[0]["s0"] == p.tokens[p.piece(ids[2])["a"]]["t0"]
    p.op("move_step", pids=[ids[2]], delta=1)
    assert p.pieces[1]["id"] == ids[2]
    res = p.op("duplicate", pids=[ids[1]])
    assert len(p.pieces) == len(ids) + 1
    assert abs(p.edit_duration() - (60 + p.tokens[p.piece(ids[1])["b"]]["t1"] - p.tokens[p.piece(ids[1])["a"]]["t0"])) < 1e-6
    p.op("delete_pieces", pids=res["result"]["new"] if isinstance(res, dict) and "result" in res else res["new"])
    assert abs(p.edit_duration() - 60) < 1e-6
    big = max(p.pieces, key=lambda x: x["b"] - x["a"])
    mid = (big["a"] + big["b"]) // 2
    out = p.op("split", pid=big["id"], at=mid)
    assert p.piece(out["right"])["a"] == mid
    p.op("merge", pids=[big["id"], out["right"]])
    assert p.piece(big["id"])["b"] >= mid
    n = len(p.pieces)
    p.op("paste", items=[{"a": 0, "b": 3, "cut": []}], after=p.pieces[0]["id"])
    assert len(p.pieces) == n + 1
    p.op("reset")
    assert abs(p.edit_duration() - 60) < 1e-6


def test_remove_pauses_keeps_edges():
    p = make_project()
    before = p.edit_duration()
    p.op("remove_pauses", min_len=2.0)
    after = p.edit_duration()
    assert after < before - 5
    # words untouched
    kept_words = sum(1 for s in p.render() for part in s["parts"]
                     for i in range(part["a"], part["b"] + 1) if p.tokens[i]["k"] == "w")
    assert kept_words == sum(1 for t in p.tokens if t["k"] == "w")


def test_render_frames_and_markers_from_story():
    p = make_project()
    an = {"id": "a1", "variants": [{"id": "v1", "title": "x", "scenes": [
        {"id": "s1", "role": "hook", "title": "Hook", "description": "d", "ranges": [{"start": 40.3, "end": 43.6}],
         "voiceover": [{"at": 40.3, "text": "VO"}], "techniques": [], "edit_notes": ""},
        {"id": "s2", "role": "setup", "title": "Setup", "description": "", "ranges": [{"start": 11.8, "end": 19.5}],
         "voiceover": [], "techniques": [], "edit_notes": ""}]}]}
    p.data["analyses"].append(an)
    p.apply_story("a1", "v1")
    frames = p.render_frames()
    assert frames[0]["start"] < frames[-1]["start"] or len(frames) >= 2
    assert frames[0]["rec"] == 0
    assert frames[1]["rec"] == frames[0]["rec_dur"]
    markers = p.edit_markers(frames)
    names = [m["name"] for m in markers]
    assert any("HOOK" in n for n in names) and "VO" in names
    # restructure -> edit follows
    p.story_op("a1", "v1", "reorder", order=["s2", "s1"])
    assert p.pieces[0]["scene"] == "s2"
    # user cut inside a scene survives re-application
    pc = p.pieces[0]
    p.op("cut", sel=[{"pid": pc["id"], "a": pc["a"], "b": pc["a"]}])
    p.story_op("a1", "v1", "update_scene", scene="s1", fields={"title": "Новий хук"})
    assert p.pieces[0]["cut"] == [pc["a"]]


def test_word_edits_undo():
    p = make_project()
    i = next(t["i"] for t in p.tokens if t["k"] == "w")
    old = p.tokens[i]["w"]
    p.edit_words([{"i": i, "w": "Хелоу"}])
    assert p.tokens[i]["w"] == "Хелоу"
    p.undo()
    assert p.tokens[i]["w"] == old
    p.redo()
    assert p.tokens[i]["w"] == "Хелоу"


def test_timestamps_and_timecode():
    assert parse_ts("00:01:02.5") == 62.5
    assert parse_ts("01:02") == 62
    assert fmt_ts(3725.25) == "01:02:05.2" or fmt_ts(3725.25) == "01:02:05.3"
    for fps, df in ((25, False), (30, False), (29.97, True), (59.94, True), (24, False)):
        for f in (0, 1, 1799, 1800, 17982, 107892, 86400 * 3):
            tc = resolve_ops.frames_to_timecode(f, fps, df)
            assert resolve_ops.timecode_to_frames(tc, fps, df) == f, (fps, f, tc)


def test_project_roundtrip():
    p = make_project()
    q = Project.from_json(p.to_json())
    assert q.render() == p.render()
