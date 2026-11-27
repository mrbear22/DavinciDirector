"""Speakers: diarization jitter smoothing and reassigning selected words."""

from davinci_director import transcribe


def W(spec):
    """'S1:Як S2:я це робив? ...' -> words 0.4 s apart (a '|' adds a 1 s pause before the next word)."""
    words, t = [], 0.0
    for tok in spec.split():
        if tok == "|":
            t += 1.0
            continue
        sp, w = tok.split(":", 1)
        words.append({"t0": t, "t1": t + 0.3, "w": w, "sp": sp})
        t += 0.4
    return words


def speakers(words):
    return " ".join("%s:%s" % (w["sp"], w["w"]) for w in words)


def test_short_flip_inside_speech_joins_the_neighbours():
    ws = W("S1:Це S1:лучше S2:вирубати S1:за S1:запання S1:клас.")
    transcribe.smooth_speakers(ws)
    assert {w["sp"] for w in ws} == {"S1"}


def test_short_reply_after_a_pause_stays():
    ws = W("S1:Ти S1:будеш S1:грати? | S2:Так. | S1:Тоді S1:заходь.")
    transcribe.smooth_speakers(ws)
    assert speakers(ws) == "S1:Ти S1:будеш S1:грати? S2:Так. S1:Тоді S1:заходь."


def test_change_moves_to_the_sentence_end():
    # diarization switched one word too early: «Як» belongs to the question «Як я це робив?»
    ws = W("S2:Ось S2:як S2:ти S2:били. S2:Як S1:я S1:це S1:робив? S1:Слеш S1:спін.")
    transcribe.smooth_speakers(ws)
    assert speakers(ws) == "S2:Ось S2:як S2:ти S2:били. S1:Як S1:я S1:це S1:робив? S1:Слеш S1:спін."
    # ... and one word too late
    ws = W("S1:Що S1:ти S1:кажеш S1:мені S2:зараз? S2:Нічого S2:не S2:кажу S2:тобі.")
    transcribe.smooth_speakers(ws)
    assert speakers(ws) == "S1:Що S1:ти S1:кажеш S1:мені S1:зараз? S2:Нічого S2:не S2:кажу S2:тобі."


def test_fast_on_a_long_stream():
    import random
    import time
    rnd = random.Random(1)
    ws, t = [], 0.0
    for i in range(40000):  # ~4 h of chatter with lots of jitter
        ws.append({"t0": t, "t1": t + 0.3, "w": "слово." if i % 9 == 8 else "слово", "sp": "S%d" % rnd.choice((1, 1, 1, 2, 3))})
        t += 0.4 if i % 9 != 8 else 1.2
    t0 = time.time()
    transcribe.smooth_speakers(ws)
    assert time.time() - t0 < 5


def test_reassign_exactly_the_selected_words(tmp_path, monkeypatch):
    from test_engine import make
    r, link, eng = make(tmp_path, monkeypatch)
    key, tr = next(iter(eng.transcripts.items()))
    frags_before = [list(f) for f in tr.frags]
    eng.set_words_speaker([{"m": key, "i0": 2, "i1": 5}], "new")
    tr = eng.transcripts[key]
    new_idx = len(tr.speakers) - 1
    assert [w[3] == new_idx for w in tr.words[:7]] == [False, False, True, True, True, False, False]
    assert tr.frags == frags_before  # fragment ids used by stories do not move
    eng.set_words_speaker([{"m": key, "i0": 3, "i1": 4}], tr.speakers[0]["id"])
    assert eng.transcripts[key].words[3][3] == 0 and eng.transcripts[key].words[2][3] == new_idx


def test_real_turns_are_kept():
    ws = W("S1:Я S1:не S1:знаю S1:що S1:робити. S2:Давай S2:просто S2:підемо S2:туди.")
    before = speakers(ws)
    transcribe.smooth_speakers(ws)
    assert speakers(ws) == before
