"""Claude JSON export/import and applying stories to the (mock) Resolve timeline."""

import json
import re

import pytest

from davinci_director import stories
from davinci_director.engine import EditError

from test_engine import make, text


def frag_text(eng, fid):
    key, n = eng.parse_fid(fid)
    tr = eng.transcripts[key]
    a, z = tr.frags[n]
    return [w[2] for w in tr.words[a:z]]


ROW = re.compile(r"^([A-Z]+\d+(?:-[A-Z]+\d+)?) (?:\(пауза \d+ с\) )?\[([\d:]+) · ([\d,.]+ (?:с|хв))\] (.*)$")


def material(text):
    """Material lines of an exported prompt -> [(id, start, length, text)]."""
    return [m.groups() for m in map(ROW.match, text.splitlines()) if m]


def test_export_is_a_readable_prompt_with_the_brief(tmp_path, monkeypatch):
    r, link, eng = make(tmp_path, monkeypatch)
    stories.set_brief(eng, {"duration": "shorts", "variants": 2, "notes": "Лише смішні моменти\nбез матів"})
    res = stories.export_for_claude(eng)
    assert res["mode"] == "single" and len(res["files"]) == 1
    name, text = res["files"][0]
    assert name.endswith(".prompt.txt")
    # the user's brief leads the prompt
    assert text.index("ЗАВДАННЯ") < text.index("ПРАВИЛА") < text.index("ВІДПОВІДЬ") < text.index("МАТЕРІАЛ")
    assert "Варіантів: 2" in text and "30 с–60 с" in text and "Лише смішні моменти" in text and "без матів" in text
    assert stories.STORIES_FORMAT in text  # answer format inside
    rows = material(text)
    assert rows and rows[0][0].startswith("A1") and rows[0][3].startswith("A: w0")  # speakers inline
    ids, _ = stories.expand_refs(eng, [row[0] for row in rows])
    assert len(ids) == sum(len(tr.frags) for tr in eng.transcripts.values())  # every fragment covered once
    # no machine noise: no JSON arrays per line, no "not_on_timeline" lists
    assert "not_on_timeline" not in text and '["A1' not in text


def test_brief_is_saved_and_validated(tmp_path, monkeypatch):
    r, link, eng = make(tmp_path, monkeypatch)
    assert stories.get_brief(eng)["variants"] == 3
    b = stories.set_brief(eng, {"duration": "custom", "min_sec": "300", "max_sec": "120", "variants": 9})
    assert (b["min_sec"], b["max_sec"], b["variants"]) == (120, 300, 5)
    assert stories.brief_range(b)[:2] == (120, 300)
    assert eng.project_state["brief"]["duration"] == "custom"


def test_speaker_change_does_not_split_lines(tmp_path, monkeypatch):
    from davinci_director.engine import Transcript
    r, link, eng = make(tmp_path, monkeypatch)
    tr = next(iter(eng.transcripts.values()))
    words, t = [], 0.0
    for i in range(12):  # two people answering each other without pauses
        words.append([t, t + 0.3, "w%d%s" % (i, "." if i % 3 == 2 else ""), (i // 3) % 2, 1.0])
        t += 0.5
    eng.set_transcript(Transcript(tr.path, words, [{"id": "S1", "name": "Олексій"}, {"id": "S2", "name": "Марко"}]))
    rows = material(stories.export_for_claude(eng)["files"][0][1])
    assert len(rows) == 1
    assert rows[0][3].startswith("Олексій: w0") and "Марко: w3" in rows[0][3] and rows[0][3].count("Олексій:") == 2


def test_topics_prompt_and_import(tmp_path, monkeypatch):
    r, link, eng = make(tmp_path, monkeypatch)
    files = stories.export_topics(eng)
    assert len(files) == 1 and files[0][0].endswith(".topics.prompt.txt")
    text = files[0][1]
    assert stories.TOPICS_FORMAT in text and material(text) and "ЗАВДАННЯ" in text
    key, tr = next(iter(eng.transcripts.items()))
    res = stories.import_any(eng, "```json\n" + json.dumps({"format": stories.TOPICS_FORMAT, "topics": [
        {"from": "A1", "title": "Початок", "summary": "Вітаються"},
        {"from": "A3-A4", "title": "Пастка", "summary": "Помітили пастку"},
        {"from": "A999", "title": "Нема"}]}) + "\n```")
    assert res["kind"] == "topics" and res["count"] == 2 and any("A999" in w for w in res["warnings"])
    tps = eng.project_state["topics"]
    assert [(t["title"], t["i"]) for t in tps] == [("Початок", 0), ("Пастка", tr.frags[2][0])]
    # a later answer for the same place replaces the title; others stay
    stories.import_any(eng, {"topics": [{"from": "A3", "title": "Пастка на мосту"}]})
    assert [t["title"] for t in eng.project_state["topics"]] == ["Початок", "Пастка на мосту"]
    stories.clear_topics(eng)
    assert eng.project_state["topics"] == []


def test_import_warns_about_wrong_duration(tmp_path, monkeypatch):
    r, link, eng = make(tmp_path, monkeypatch)
    stories.set_brief(eng, {"duration": "long"})
    s = stories.import_stories(eng, {"stories": [{"title": "Коротко", "scenes": [{"title": "x", "fragments": ["A1"]}]}]})[0]
    assert s["warnings"][0].startswith("Тривалість ≈") and s["duration_sec"] >= 0


def test_long_material_goes_through_beats(tmp_path, monkeypatch):
    from davinci_director.engine import Transcript
    r, link, eng = make(tmp_path, monkeypatch)
    tr = next(iter(eng.transcripts.values()))
    words, t = [], 0.0
    for i in range(400):  # speech with a 5 s pause after every sentence of 8 words
        words.append([t, t + 0.3, "слово%d%s" % (i, "." if i % 8 == 7 else ""), 0, 1.0])
        t += 5.5 if i % 8 == 7 else 0.4
    eng.set_transcript(Transcript(tr.path, words, tr.speakers))
    res = stories.export_for_claude(eng, budget=900)
    assert res["mode"] == "beats" and len(res["files"]) > 1
    for name, text in res["files"]:
        assert stories.estimate_tokens(text) <= 900 + 250
        assert stories.BEATS_FORMAT in text and material(text)
    covered, _ = stories.expand_refs(eng, [r[0] for _, text in res["files"] for r in material(text)])
    tr2 = next(iter(eng.transcripts.values()))
    assert set(covered) == {"A%04d" % (n + 1) for n in range(len(tr2.frags))}  # every fragment in some part
    with pytest.raises(EditError):
        stories.export_stage2(eng)
    r1 = stories.import_any(eng, {"format": stories.BEATS_FORMAT, "part": 1, "beats": [
        {"fragments": "A3-A4", "title": "Пастка", "summary": "Помітили пастку", "type": "tense", "strength": 4}]})
    r2 = stories.import_any(eng, '```json\n{"format": "davinci-director.beats/1", "part": 2, "beats": ['
                                 '{"fragments": "A1", "title": "Вступ", "summary": "Привітання", "type": "context"}]}\n```')
    assert r1["kind"] == "beats" and r2["total"] == 2
    name, stage2 = stories.export_stage2(eng)
    moments = [l.split(" ")[0] for l in stage2.split("МОМЕНТИ", 1)[1].splitlines()[2:] if l.strip()]
    assert moments == ["A1", "A3-A4"]  # sorted by material order
    assert stories.STORIES_FORMAT in stage2 and "ЗАВДАННЯ" in stage2
    res = stories.import_any(eng, {"stories": [{"title": "З моментів", "scenes": [{"title": "s", "fragments": ["A3-A4"]}]}]})
    assert res["kind"] == "stories"


def test_import_validates_and_expands_ranges(tmp_path, monkeypatch):
    r, link, eng = make(tmp_path, monkeypatch)
    answer = "Ось варіанти:\n```json\n" + json.dumps({
        "format": stories.STORIES_FORMAT,
        "stories": [{"id": "v1", "title": "Тест", "scenes": [
            {"id": "s1", "title": "Кінець", "role": "hook", "fragments": ["A0004"], "voiceover": "Текст VO"},
            {"id": "s2", "title": "Початок", "role": "setup", "fragments": "A0001-A0002, A0099"}]}]}) + "\n```"
    imported = stories.import_stories(eng, answer)
    s = imported[0]
    assert [sc["fragments"] for sc in s["scenes"]] == [["A0004"], ["A0001", "A0002"]]
    assert s["scenes"][0]["voiceover"] == {"text": "Текст VO", "placement": "over"}
    assert any("A0099" in w for w in s["warnings"])
    with pytest.raises(EditError):
        stories.import_stories(eng, "не json")


def _story(eng):
    return stories.import_stories(eng, {"stories": [{"title": "Реванш", "scenes": [
        {"title": "Гачок", "role": "hook", "fragments": ["A0004"]},
        {"title": "Початок", "role": "setup", "fragments": ["A0001", "A0002"]}]}]})[0]


def test_apply_story_new_timeline(tmp_path, monkeypatch):
    r, link, eng = make(tmp_path, monkeypatch)
    s = _story(eng)
    original = r.project.current
    stories.apply_story(eng, s["uid"], mode="new")
    assert r.project.current is not original
    assert r.project.current.name.endswith("Реванш")
    assert text(eng) == frag_text(eng, "A0004") + frag_text(eng, "A0001") + frag_text(eng, "A0002")
    markers = r.project.current.markers
    assert [m["name"] for f, m in sorted(markers.items())] == ["Гачок", "Початок"]
    assert len(original.items) == 2  # the source timeline is untouched
    eng.undo()  # back to the original timeline
    assert r.project.current is original


def test_apply_story_replace_and_scene_insert(tmp_path, monkeypatch):
    r, link, eng = make(tmp_path, monkeypatch)
    s = _story(eng)
    stories.apply_story(eng, s["uid"], mode="replace", markers=False)
    expect = frag_text(eng, "A0004") + frag_text(eng, "A0001") + frag_text(eng, "A0002")
    assert text(eng) == expect
    # insert a single scene at the end
    sc = s["scenes"][0]
    stories.apply_scene(eng, s["uid"], sc["uid"], where="end")
    assert text(eng) == expect + frag_text(eng, "A0004")
    assert stories.locate_fragment(eng, "A0004") == eng.view["blocks"][0]["s"] + round(
        (eng.transcripts[eng.view["blocks"][0]["m"]].words[eng.transcripts[eng.view["blocks"][0]["m"]].frags[3][0]][0]
         - eng.view["blocks"][0]["src0"]) * 25)


def test_scene_order_update(tmp_path, monkeypatch):
    r, link, eng = make(tmp_path, monkeypatch)
    s = _story(eng)
    order = [sc["uid"] for sc in s["scenes"]][::-1]
    stories.update_story(eng, s["uid"], {"scene_order": order, "scenes": [{"uid": order[0], "enabled": False}]})
    s2 = stories.find_story(eng, s["uid"])
    assert [sc["uid"] for sc in s2["scenes"]] == order and s2["scenes"][0]["enabled"] is False


def test_tiny_fragments_are_merged(tmp_path, monkeypatch):
    from davinci_director.engine import Transcript
    r, link, eng = make(tmp_path, monkeypatch)
    tr = next(iter(eng.transcripts.values()))
    # Whisper-like: 2 words, then a 1.2 s pause -> a new fragment every 2 words, one speaker
    words, t = [], 0.0
    for i in range(30):
        words.append([t, t + 0.3, "w%d" % i, 0, 1.0])
        t += 0.4 if i % 2 == 0 else 1.5
    eng.set_transcript(Transcript(tr.path, words, [{"id": "S1", "name": "Олексій"}]))
    tr = next(iter(eng.transcripts.values()))
    assert len(tr.frags) == 15
    rows = material(stories.export_for_claude(eng)["files"][0][1])
    assert len(rows) < 6 and rows[0][0].startswith("A1-") and "Олексій:" not in rows[0][3]  # one speaker: no labels
    ids, _ = stories.expand_refs(eng, [row[0] for row in rows])
    assert len(ids) == 15
