"""Text <-> timeline sync on the mock Resolve (behaviour measured on Resolve 21.2)."""

import pytest

from davinci_director.engine import Engine, EditError, Transcript
from davinci_director.link import DirectLink
from davinci_director.mock_resolve import MockResolve

FPS = 25.0


def make(tmp_path, monkeypatch, fps=FPS, tl_fps=None, n_words=40):
    monkeypatch.setenv("DD_HOME", str(tmp_path / "home"))
    r = MockResolve(fps=tl_fps or fps)
    path = str(tmp_path / "stream.mp4")
    mpi = r.add_media(path, fps=fps, frames=int(n_words * 0.5 * fps) + 50)
    r.timeline_with(mpi, ranges=[(0, mpi.frames)])
    link = DirectLink(r).start()
    eng = Engine(link, state_dir=str(tmp_path / "projects"))
    eng.load_project("Stream Project")
    # word i spans [0.5 i, 0.5 i + 0.4) ; speaker changes every 10 words
    words = [[0.5 * i, 0.5 * i + 0.4, "w%d" % i, (i // 10) % 2, 1.0] for i in range(n_words)]
    tr = Transcript(path, words, [{"id": "S1", "name": "A"}, {"id": "S2", "name": "B"}])
    eng.set_transcript(tr)
    return r, link, eng


def text(eng):
    """Visible (on-timeline) words in timeline order."""
    out = []
    for b in eng.view["blocks"]:
        tr = eng.transcripts.get(b["m"])
        out += [tr.words[i][2] for i in range(b["i0"], b["i1"])] if tr else ["<%s>" % b["name"]]
    return out


def video(r):
    tl = r.project.current
    return [(it.start, it.end, int(round(it.sf0))) for it in tl.GetItemListInTrack("video", 1)]


def audio_matches_video(r):
    tl = r.project.current
    v = [(it.start, it.end) for it in tl.GetItemListInTrack("video", 1)]
    a = [(it.start, it.end) for it in tl.GetItemListInTrack("audio", 1)]
    return v == a


def sel(eng, w_from, w_to):
    """Selection by word text (first occurrence)."""
    def find(name):
        for k, b in enumerate(eng.view["blocks"]):
            tr = eng.transcripts[b["m"]]
            for i in range(b["i0"], b["i1"]):
                if tr.words[i][2] == name:
                    return {"b": k, "w": i}
        raise AssertionError(name)
    return {"a": find(w_from), "z": find(w_to)}


def test_view_mirrors_timeline(tmp_path, monkeypatch):
    r, link, eng = make(tmp_path, monkeypatch)
    assert text(eng) == ["w%d" % i for i in range(40)]
    assert len(eng.view["blocks"]) == 1


def test_delete_words_ripples_real_timeline(tmp_path, monkeypatch):
    r, link, eng = make(tmp_path, monkeypatch)
    eng.delete(sel(eng, "w5", "w9"))
    assert text(eng) == ["w%d" % i for i in range(40) if not 5 <= i <= 9]
    v = video(r)
    assert len(v) == 2 and v[0][1] == v[1][0], "gap must be closed (ripple)"
    assert audio_matches_video(r)
    # cut words are listed as restorable after the first clip
    assert eng.view["blocks"][0]["cut"] == [{"side": "after", "i0": 5, "i1": 10}]


def test_restore_extends_clip(tmp_path, monkeypatch):
    r, link, eng = make(tmp_path, monkeypatch)
    before = video(r)
    eng.delete(sel(eng, "w5", "w9"))
    eng.restore(0, 5, 10)
    assert text(eng) == ["w%d" % i for i in range(40)]
    assert audio_matches_video(r)
    assert sum(e - s for s, e, _ in video(r)) == sum(e - s for s, e, _ in before)


def test_partial_restore(tmp_path, monkeypatch):
    r, link, eng = make(tmp_path, monkeypatch)
    eng.delete(sel(eng, "w5", "w9"))
    eng.restore(0, 7, 9)
    assert text(eng) == [w for w in ("w%d" % i for i in range(40)) if w not in ("w5", "w6", "w9")]


def test_move_fragment_forward_and_back(tmp_path, monkeypatch):
    r, link, eng = make(tmp_path, monkeypatch)
    # move w0..w4 before w20
    target = sel(eng, "w20", "w20")["a"]
    eng.move(sel(eng, "w0", "w4"), target)
    expect = ["w%d" % i for i in range(5, 20)] + ["w%d" % i for i in range(5)] + ["w%d" % i for i in range(20, 40)]
    assert text(eng) == expect
    assert audio_matches_video(r)
    v = video(r)
    assert all(v[i][1] == v[i + 1][0] for i in range(len(v) - 1)), "no gaps"
    # move w30..w35 to the very beginning
    eng.move(sel(eng, "w30", "w35"), {"b": 0, "w": None})
    assert text(eng)[:6] == ["w%d" % i for i in range(30, 36)]
    eng.undo()
    assert text(eng) == expect
    eng.undo()
    assert text(eng) == ["w%d" % i for i in range(40)]


def test_copy_paste_and_duplicate(tmp_path, monkeypatch):
    r, link, eng = make(tmp_path, monkeypatch)
    eng.copy(sel(eng, "w2", "w3"))
    eng.paste({"b": None, "end": True})
    assert text(eng)[-2:] == ["w2", "w3"]
    eng.duplicate(sel(eng, "w10", "w11"))
    t = text(eng)
    i = t.index("w11")
    assert t[i + 1:i + 3] == ["w10", "w11"]
    assert audio_matches_video(r)


def test_resolve_side_edits_update_text(tmp_path, monkeypatch):
    r, link, eng = make(tmp_path, monkeypatch)
    eng.delete(sel(eng, "w10", "w12"))  # -> two clips
    tl = r.project.current
    v = tl.GetItemListInTrack("video", 1)
    # editor deletes the second clip directly in Resolve
    tl.DeleteClips([v[1]] + v[1].GetLinkedItems())
    link.refresh()
    assert text(eng) == ["w%d" % i for i in range(10)]
    # editor trims the head of the first clip by 1 s (25 frames)
    v = tl.GetItemListInTrack("video", 1)
    v[0].SetLeftOffset(v[0].GetLeftOffset() + 25)
    link.refresh()
    assert text(eng)[0] == "w2"
    # editor puts a new piece of the source at the end
    r.project.pool.AppendToTimeline([{"mediaPoolItem": v[0].mpi, "startFrame": 375, "endFrame": 425}])
    link.refresh()
    assert text(eng)[-4:] == ["w30", "w31", "w32", "w33"]
    # an edit made in Resolve invalidates our undo history safely
    with pytest.raises(EditError):
        eng.undo()


def test_reordered_in_resolve(tmp_path, monkeypatch):
    r, link, eng = make(tmp_path, monkeypatch)
    eng.delete(sel(eng, "w19", "w20"))
    tl = r.project.current
    a, b = tl.GetItemListInTrack("video", 1)
    # swap clips in Resolve: park b, move a after it, put b first
    da, db = a.end - a.start, b.end - b.start
    s0 = a.start
    tl.MoveItems([b], {"startFrame": 200000})
    tl.MoveItems([a], {"startFrame": s0 + db})
    tl.MoveItems([b], {"startFrame": s0})
    link.refresh()
    assert text(eng)[:3] == ["w21", "w22", "w23"]
    assert text(eng)[-1] == "w18"


def test_mixed_frame_rates(tmp_path, monkeypatch):
    r, link, eng = make(tmp_path, monkeypatch, fps=25.0, tl_fps=24.0)
    eng.delete(sel(eng, "w5", "w9"))
    assert text(eng) == ["w%d" % i for i in range(40) if not 5 <= i <= 9]
    eng.move(sel(eng, "w0", "w2"), sel(eng, "w30", "w30")["a"])
    t = text(eng)
    assert t.index("w0") == t.index("w30") - 3


def test_fragment_ids_survive_project_switch(tmp_path, monkeypatch):
    r, link, eng = make(tmp_path, monkeypatch)
    assert eng.view["blocks"][0]["tag"] == "A"
    eng.load_project("Інший проєкт")
    assert eng.view["blocks"][0]["tag"] == "A"
    assert eng.parse_fid("A0001") is not None


def test_cut_a_pause_between_words(tmp_path, monkeypatch):
    """Silence (gameplay without speech) is a token: cutting it removes only the gap."""
    r, link, eng = make(tmp_path, monkeypatch)
    tr = next(iter(eng.transcripts.values()))
    # make a 4 s pause after w9: shift later words
    for w in tr.words[10:]:
        w[0] += 4.0
        w[1] += 4.0
    eng.set_transcript(tr)
    tl = r.project.current
    before = sum(it.end - it.start for it in tl.GetItemListInTrack("video", 1))
    eng.delete({"a": {"b": 0, "w": 9, "p": True}, "z": {"b": 0, "w": 9, "p": True}})
    after = sum(it.end - it.start for it in tl.GetItemListInTrack("video", 1))
    assert text(eng) == ["w%d" % i for i in range(36)], "no word lost (w36+ were pushed past the 22 s media)"
    assert 4.0 * FPS - 5 <= before - after <= 4.2 * FPS, "about 4 s of silence removed"


def test_correct_a_whole_phrase(tmp_path, monkeypatch):
    r, link, eng = make(tmp_path, monkeypatch)
    tr = eng.transcripts[eng.view["blocks"][0]["m"]]
    key, frags_before, before = tr.key, len(tr.frags), video(r)
    eng.project_state["topics"] = [{"m": key, "i": 30, "title": "T"}]
    # w3 w4 w5 w6 -> "w3 добрий w6": two misheard words were one; the kept words keep their timing
    eng.edit_words(key, 3, 7, "w3  добрий   w6")
    assert text(eng)[:6] == ["w0", "w1", "w2", "w3", "добрий", "w6"]
    assert tr.words[3][:2] == [1.5, 1.9] and tr.words[5][:2] == [3.0, 3.4]
    assert tr.words[4][:2] == [2.0, 2.9]
    assert len(tr.words) == 39 and len(tr.frags) == frags_before   # fragment numbering is stable
    assert tr.frags[-1][1] == len(tr.words) and all(a <= z for a, z in tr.frags)
    assert eng.project_state["topics"][0]["i"] == 29
    assert video(r) == before  # only the text changes, never the video
    # more new words than old: they share the time; speaker comes from the words they replace
    eng.edit_words(key, 9, 11, "раз два три чотири")
    assert [w[2] for w in tr.words[9:13]] == ["раз", "два", "три", "чотири"]
    assert tr.words[9][0] == 5.0 and tr.words[12][1] == 5.9 and {w[3] for w in tr.words[9:13]} == {1}
    with pytest.raises(EditError):
        eng.edit_words(key, 0, 2, "   ")


def test_text_corrections_and_speakers_undo(tmp_path, monkeypatch):
    r, link, eng = make(tmp_path, monkeypatch)
    tr = eng.transcripts[eng.view["blocks"][0]["m"]]
    key, words0 = tr.key, [list(w) for w in tr.words]
    eng.edit_words(key, 2, 5, "одне слово")
    eng.set_words_speaker([{"m": key, "i0": 0, "i1": 2}], "S2")
    assert tr.words[0][3] == 1 and len(tr.words) == 39
    eng.undo()  # the speaker change
    assert tr.words[0][3] == 0 and len(tr.words) == 39
    eng.undo()  # the correction
    assert eng.transcripts[key].words == words0
    eng.redo()
    assert [w[2] for w in eng.transcripts[key].words[2:4]] == ["одне", "слово"]
