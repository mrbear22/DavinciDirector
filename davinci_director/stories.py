"""Story search with a language model (Claude, ChatGPT, …) via a text prompt (no API calls).

1. ``export_for_claude`` → a plain-text prompt (``*.prompt.txt``): the user's brief (wanted
   duration, number of variants, free-form wishes), rules, the answer format and the material as
   readable lines ``A12-A15 [12:03 · 18 с] Олексій: … Марко: …``.  Fragment ids refer to source
   media, not to the timeline, so the answer stays valid whatever the user does on the timeline.
   Long recordings are split into parts: stage 1 asks for key moments (beats) per part,
   ``export_stage2`` sends all beats for the story search — each file fits one chat context.
2. The model answers with ``davinci-director.stories/1`` JSON (one or more variants).
3. ``import_stories`` validates it; the UI shows the variants; ``story_clips``
   turns scenes into source ranges that the engine puts on the Resolve timeline.
"""

import json
import re
import time
import uuid

from .engine import EditError

TRANSCRIPT_FORMAT = "davinci-director.transcript/1"
STORIES_FORMAT = "davinci-director.stories/1"

ROLES = ["hook", "setup", "context", "rising", "conflict", "turn", "climax", "resolution", "payoff",
         "outro", "cta", "bridge", "montage", "other"]
ROLE_COLORS = {"hook": "Red", "setup": "Blue", "context": "Cyan", "rising": "Green", "conflict": "Purple",
               "turn": "Fuchsia", "climax": "Rose", "resolution": "Sky", "payoff": "Mint", "outro": "Lavender",
               "cta": "Sand", "bridge": "Cream", "montage": "Lemon", "other": "Blue"}

# Only what the app reads back.  Kept small: every field here is something the model will fill in.
ANSWER_SCHEMA = {
    "format": STORIES_FORMAT,
    "stories": [{
        "title": "Назва варіанта",
        "logline": "Про що відео, одним реченням",
        "duration_sec": 600,
        "scenes": [{
            "title": "Назва сцени",
            "role": "hook | setup | rising | conflict | turn | climax | resolution | payoff | outro",
            "fragments": ["A12-A15", "A40"],
            "summary": "Що відбувається",
            "edit_notes": "Що прибрати, де різати (необов'язково)",
            "voiceover": "Закадровий текст, лише якщо без нього незрозуміло; інакше null"
        }]
    }]
}

BEATS_FORMAT = "davinci-director.beats/1"
BEATS_SCHEMA = {
    "format": BEATS_FORMAT,
    "beats": [{"fragments": "A120-A134", "title": "Коротка назва", "summary": "Що відбувається",
               "type": "funny | tense | drama | info | turn | epic | context", "strength": 4,
               "quote": "найкраща цитата або порожньо"}]
}

# Budget per file for a chat: leaves room for the answer and the conversation in
# 200k-token models (Sonnet/Opus) even with long system prompts.
TOKEN_BUDGET = 35000

# What the user wants (saved per project, edited in the export dialog)
DURATIONS = {
    "shorts": ("Shorts / Reels / TikTok", 30, 60),
    "short": ("коротке відео", 120, 240),
    "medium": ("відео середньої довжини", 360, 720),
    "long": ("довге відео", 900, 1800),
}
DEFAULT_BRIEF = {"duration": "medium", "min_sec": None, "max_sec": None, "variants": 3, "notes": ""}

RULES = [
    "Посилайся на матеріал лише через id на початку рядка (\"A12\" або діапазон \"A12-A15\"). Таймкодів не вигадуй.",
    "Можна брати частину діапазону: з рядка \"A12-A15\" — \"A13-A14\". Кожен id — шматок у кілька секунд.",
    "Порядок фрагментів у сцені і порядок сцен — це порядок у готовому відео. Хронологію можна змінювати.",
    "Тривалість варіанта — сума тривалостей вибраних фрагментів (у дужках біля кожного рядка). Тримайся заданої тривалості.",
    "Бери лише те, що справді є в матеріалі. Закадровий текст — лише там, де без нього глядач не зрозуміє.",
    "«(пауза N с)» — довгий відрізок без мови; на стрімі це зазвичай геймплей, його можна брати як дію.",
]


def get_brief(engine):
    b = dict(DEFAULT_BRIEF)
    b.update({k: v for k, v in (engine.project_state.get("brief") or {}).items() if k in DEFAULT_BRIEF})
    return b


def set_brief(engine, patch):
    b = get_brief(engine)
    if patch.get("duration") in list(DURATIONS) + ["custom"]:
        b["duration"] = patch["duration"]
    for k in ("min_sec", "max_sec"):
        if k in patch:
            v = patch[k]
            b[k] = max(5, int(float(v))) if v not in (None, "") else None
    if "variants" in patch:
        b["variants"] = max(1, min(5, int(patch["variants"] or 1)))
    if "notes" in patch:
        b["notes"] = str(patch["notes"] or "")[:4000]
    if b["min_sec"] and b["max_sec"] and b["max_sec"] < b["min_sec"]:
        b["min_sec"], b["max_sec"] = b["max_sec"], b["min_sec"]
    with engine._lock:
        engine.project_state["brief"] = b
        engine.save_project()
    return b


def brief_range(brief):
    """(min_sec, max_sec, label) of the wanted duration."""
    if brief.get("duration") == "custom" and (brief.get("min_sec") or brief.get("max_sec")):
        lo = brief.get("min_sec") or brief.get("max_sec")
        hi = brief.get("max_sec") or lo
        return lo, hi, "відео"
    label, lo, hi = DURATIONS.get(brief.get("duration"), DURATIONS["medium"])
    return lo, hi, label


def _len_text(sec):
    sec = int(sec)
    if sec < 120:
        return "%d с" % sec
    return "%g хв" % (round(sec / 30) / 2)


def _brief_lines(brief, what):
    lo, hi, label = brief_range(brief)
    n = brief.get("variants") or 1
    lines = ["ЗАВДАННЯ",
             "Ти — сценарист і монтажер. %s" % what,
             "- Варіантів: %d%s." % (n, " (різні історії або різні підходи до матеріалу)" if n > 1 else ""),
             "- Тривалість кожного варіанта: %s — %s–%s." % (label, _len_text(lo), _len_text(hi))]
    notes = (brief.get("notes") or "").strip()
    if notes:
        lines += ["- Побажання автора (найважливіше):"] + ["  " + l for l in notes.splitlines() if l.strip()]
    return lines


def estimate_tokens(text):
    """Rough token count: Cyrillic ≈ 2.3 chars/token, Latin/digits ≈ 3.8."""
    cyr = sum(1 for c in text if "\u0400" <= c <= "\u04ff")
    return int(cyr / 2.3 + (len(text) - cyr) / 3.8) + 1


def _clock(sec):
    sec = int(max(0, sec))
    h, m, s = sec // 3600, sec // 60 % 60, sec % 60
    return "%d:%02d:%02d" % (h, m, s) if h else "%d:%02d" % (m, s)


def short_id(engine, key, k):
    return "%s%d" % (engine.tags.get(key) or engine.tag_for(key), k + 1)


def _on_timeline(engine):
    on = set()
    for b in engine.view["blocks"]:
        if not b["m"] or b["i1"] <= b["i0"]:
            continue
        for k, (a, z) in enumerate(engine.transcripts[b["m"]].frags):
            if a < b["i1"] and z > b["i0"]:
                on.add((b["m"], k))
    return on


_SENT = re.compile(r"[.!?…]$")

ROW_PAUSE = 4.0     # a pause this long starts a new line of material
ROW_MAX_SEC = 35.0  # ... and so does a line getting this long (at the end of a sentence if possible)
ROW_MAX_HARD = 50.0


def material_rows(engine, only_timeline=False):
    """Lines of material: {"id", "t0", "t1", "text", "pause"}.  A line is a stretch of speech up to a long
    pause (or ~35 s); a change of speaker does not start a new line — it is written inline («Ім'я: …»),
    because every word already carries its speaker.  Ids are fragment ranges (A358-A361); the ids
    themselves never change."""
    if not engine.transcripts:
        raise EditError("Немає транскрипції — спочатку транскрибуйте таймлайн.")
    on = _on_timeline(engine)
    keys = sorted((k for k in engine.tags if k in engine.transcripts), key=lambda k: engine.tags[k])
    rows, files = [], {}
    for key in keys:
        tr = engine.transcripts[key]
        files[engine.tags[key]] = "%s (%s)" % (tr.path.replace("\\", "/").split("/")[-1],
                                               _clock(tr.words[-1][1] if tr.words else 0))
        names = [s["name"] for s in tr.speakers] or [""]
        multi = len({w[3] for w in tr.words}) > 1
        cur, prev_end = None, None

        def flush():
            if cur:
                rid = cur["ids"][0] if len(cur["ids"]) == 1 else "%s-%s" % (cur["ids"][0], cur["ids"][-1])
                rows.append({"id": rid, "t0": cur["t0"], "t1": cur["t1"], "text": " ".join(cur["text"]),
                             "pause": cur["pause"], "file": engine.tags[key]})

        for k, (a, z) in enumerate(tr.frags):
            ws = tr.words[a:z]
            if not ws or (only_timeline and (key, k) not in on):
                continue
            gap = ws[0][0] - prev_end if prev_end is not None else ws[0][0]
            long_ = cur is not None and ws[-1][1] - cur["t0"] > ROW_MAX_SEC
            new = (cur is None or gap >= ROW_PAUSE
                   or (long_ and _SENT.search(cur["text"][-1]))
                   or (cur is not None and ws[-1][1] - cur["t0"] > ROW_MAX_HARD))
            if new:
                flush()
                cur = {"ids": [], "t0": ws[0][0], "t1": ws[-1][1], "text": [], "sp": None,
                       "pause": gap if prev_end is not None else 0}
            # inline speaker label wherever the speaker changes (not when there is only one)
            parts, run = [], []
            for w in ws:
                sp = w[3] if w[3] < len(names) else 0
                if multi and sp != cur["sp"]:
                    if run:
                        parts.append(_join(run))
                        run = []
                    run.append("%s:" % names[sp])
                    cur["sp"] = sp
                run.append(w[2])
            parts.append(_join(run))
            cur["ids"].append(short_id(engine, key, k))
            cur["text"].append(" ".join(p for p in parts if p))
            cur["t1"] = ws[-1][1]
            prev_end = ws[-1][1]
        flush()
    return rows, files


def _row_line(r):
    pause = "(пауза %d с) " % round(r["pause"]) if r["pause"] >= 10 else ""
    return "%s %s[%s · %s] %s" % (r["id"], pause, _clock(r["t0"]), _len_text(max(1, r["t1"] - r["t0"])), r["text"])


def _material_head(files, only_timeline):
    src = "; ".join("%s — %s" % (tag, name) for tag, name in sorted(files.items()))
    return ["МАТЕРІАЛ%s" % (" (лише те, що зараз на таймлайні)" if only_timeline else ""),
            "Файли: %s." % src,
            "Рядок: id [початок у файлі · тривалість] текст. «Ім'я:» — хто говорить далі."]


def _prompt(sections):
    return "\n\n".join("\n".join(s) for s in sections if s) + "\n"


def _answer_lines(schema, extra=""):
    return ["ВІДПОВІДЬ",
            "Лише один JSON-об'єкт у такому форматі (українською)%s:" % extra,
            json.dumps(schema, ensure_ascii=False, indent=1)]


def export_for_claude(engine, budget=TOKEN_BUDGET, only_timeline=False):
    """Return {"mode": "single"|"beats", "tokens": N, "files": [(name, text)]}.
    Fits one chat: a single prompt asking for stories.  Longer: parts asking for key
    moments (beats); then ``export_stage2`` builds stories from all moments."""
    rows, files = material_rows(engine, only_timeline)
    brief = get_brief(engine)
    tl = engine.view.get("tl") or engine.project_name or "timeline"
    lines = [_row_line(r) for r in rows]
    what = "З матеріалу нижче (таймлайн «%s») змонтуй відео." % tl
    single = _prompt([_brief_lines(brief, what), ["ПРАВИЛА"] + ["- " + x for x in RULES],
                      _answer_lines(ANSWER_SCHEMA), _material_head(files, only_timeline) + [""] + lines])
    total = estimate_tokens(single)
    if total <= budget:
        return {"mode": "single", "tokens": total, "files": [("%s.prompt.txt" % _safe(tl), single)]}
    head = lambda i, n: [  # noqa: E731
        _brief_lines(brief, "Це частина %d з %d довгого запису (таймлайн «%s»). Зараз потрібні не сюжети, а ключові "
                            "моменти: усе помітне (смішне, напружене, драматичне, поворотне, епічне, важливий контекст), "
                            "приблизно 1 на 1–3 хв. З них на наступному етапі складуться варіанти нижче." % (i, n, tl)),
        ["ПРАВИЛА"] + ["- " + x for x in RULES[:2] + RULES[4:]],
        _answer_lines(dict(BEATS_SCHEMA, part=i))]
    parts = _split(lines, budget - estimate_tokens(_prompt(head(99, 99))) - 150, overlap=2)
    if len(parts) == 1:  # nothing to split (e.g. one very long line): a single prompt is still best
        return {"mode": "single", "tokens": total, "files": [("%s.prompt.txt" % _safe(tl), single)]}
    out = []
    for i, part in enumerate(parts, 1):
        text = _prompt(head(i, len(parts)) + [_material_head(files, only_timeline) + [""] + part])
        out.append(("%s.part%02d-of-%02d.prompt.txt" % (_safe(tl), i, len(parts)), text))
    return {"mode": "beats", "tokens": total, "files": out}


def _split(lines, room, overlap=0):
    """Lines -> parts of at most ``room`` tokens (the last ``overlap`` lines repeat for context)."""
    parts, cur, size = [], [], 0
    for ln in lines:
        n = estimate_tokens(ln) + 1
        if cur and size + n > room:
            parts.append(cur)
            cur = cur[-overlap:] if overlap else []
            size = sum(estimate_tokens(x) + 1 for x in cur)
        cur.append(ln)
        size += n
    if cur:
        parts.append(cur)
    return parts


# ------------------------------------------------------------------ topics (contents of the conversation)

TOPICS_FORMAT = "davinci-director.topics/1"
TOPICS_SCHEMA = {"format": TOPICS_FORMAT,
                 "topics": [{"from": "A1", "title": "Коротка конкретна назва", "summary": "Про що це, одним реченням"}]}
TOPICS_RULES = [
    "Нова тема — коли змінюється предмет розмови, подія або дія. Звичайна тема триває 1–10 хв; дрібних тем не роби.",
    "Назва — 2–6 слів, конкретна, щоб з неї було ясно, що відбувається (не «Розмова», не «Гра»).",
    "\"from\" — id рядка, з якого тема починається (перший id рядка; для діапазону \"A12-A15\" — \"A12\").",
    "Теми йдуть по порядку і покривають увесь матеріал: перша починається з першого рядка.",
]


def export_topics(engine, budget=TOKEN_BUDGET, only_timeline=False):
    """Prompt(s) asking the language model to split the material into topics (a table of contents)."""
    rows, files = material_rows(engine, only_timeline)
    tl = engine.view.get("tl") or engine.project_name or "timeline"
    lines = [_row_line(r) for r in rows]

    def head(i, n):
        part = "" if n == 1 else " Це частина %d з %d; теми — лише для цієї частини (якщо початок продовжує " \
                                 "тему з попередньої частини, назви її так само)." % (i, n)
        return [["ЗАВДАННЯ", "Поділи розмову з відео (таймлайн «%s») на теми — як зміст книжки, щоб монтажер одразу "
                             "бачив, що де відбувається.%s" % (tl, part)],
                ["ПРАВИЛА"] + ["- " + x for x in TOPICS_RULES],
                _answer_lines(TOPICS_SCHEMA)]

    parts = _split(lines, budget - estimate_tokens(_prompt(head(99, 99))) - 150)
    out = []
    for i, part in enumerate(parts, 1):
        name = "%s.topics.prompt.txt" % _safe(tl) if len(parts) == 1 else \
            "%s.topics.part%02d-of-%02d.prompt.txt" % (_safe(tl), i, len(parts))
        out.append((name, _prompt(head(i, len(parts)) + [_material_head(files, only_timeline) + [""] + part])))
    return out


def import_topics(engine, data):
    """Topics from the model -> project_state["topics"] = [{m, i, fid, title, summary}] (word index of the
    start).  Parts of a long recording merge; a topic at the same place is replaced."""
    got, warn = [], []
    for tp in data.get("topics") or []:
        ref = tp.get("from") or tp.get("start") or tp.get("fragments") or ""
        if isinstance(ref, list):
            ref = ref[0] if ref else ""
        ids, w = expand_refs(engine, re.split(r"\s*[-–—]\s*", str(ref).strip())[0])
        warn += w
        if not ids or not (tp.get("title") or "").strip():
            continue
        key, k = engine.parse_fid(ids[0])
        got.append({"m": key, "i": engine.transcripts[key].frags[k][0], "fid": short_id(engine, key, k),
                    "title": tp["title"].strip()[:120], "summary": (tp.get("summary") or "").strip()[:400]})
    if not got:
        raise EditError("У відповіді немає тем (формат %s)." % TOPICS_FORMAT)
    with engine._lock:
        have = {(t["m"], t["i"]): t for t in engine.project_state.get("topics") or []}
        for t in got:
            have[(t["m"], t["i"])] = t
        merged = sorted(have.values(), key=lambda t: (engine.tags.get(t["m"], ""), t["i"]))
        engine.project_state["topics"] = merged
        engine.save_project()
    return {"kind": "topics", "count": len(got), "total": len(merged), "warnings": warn[:10]}


def clear_topics(engine):
    with engine._lock:
        engine.project_state["topics"] = []
        engine.save_project()


def _span_seconds(engine, ids):
    """Duration of the material behind fragment ids (sum over fragments)."""
    total = 0.0
    for fid in ids:
        p = engine.parse_fid(fid)
        if not p:
            continue
        tr = engine.transcripts[p[0]]
        a, z = tr.frags[p[1]]
        if z > a:
            total += tr.words[z - 1][1] - tr.words[a][0]
    return total


def export_stage2(engine):
    beats = engine.project_state.get("beats") or []
    if not beats:
        raise EditError("Ще немає моментів: імпортуйте відповіді мовної моделі на всі частини.")
    brief = get_brief(engine)
    tl = engine.view.get("tl") or engine.project_name or "timeline"
    lines = []
    for bt in beats:
        ids, _ = expand_refs(engine, bt["fragments"])
        kind = ", ".join(str(x) for x in (bt.get("type"), bt.get("strength")) if x not in (None, ""))
        quote = " «%s»" % bt["quote"] if bt.get("quote") else ""
        lines.append("%s [%s · %s]%s %s — %s%s" % (bt["fragments"], bt.get("time", ""), _len_text(max(1, _span_seconds(engine, ids))),
                                                  " (%s)" % kind if kind else "", bt.get("title", ""), bt.get("summary", ""), quote))
    text = _prompt([_brief_lines(brief, "Нижче — ключові моменти довгого запису (таймлайн «%s»). Склади з них відео." % tl),
                    ["ПРАВИЛА"] + ["- " + x for x in RULES[:5]],
                    _answer_lines(ANSWER_SCHEMA),
                    ["МОМЕНТИ", "Рядок: id [початок · тривалість] (тип, сила 1–5) назва — опис «цитата».", ""] + lines])
    return ("%s.stage2.prompt.txt" % _safe(tl), text)


def _safe(name):
    return re.sub(r"[^\w\-]+", "_", name)[:60] or "timeline"


def export_transcript(engine, max_chars=None, only_timeline=False):
    """List of {"name", "text", "mode", "tokens"} (kept for scripts/tests)."""
    budget = int(max_chars / 2.5) if max_chars else TOKEN_BUDGET
    res = export_for_claude(engine, budget, only_timeline)
    return [{"name": n, "text": t, "mode": res["mode"], "tokens": res["tokens"]} for n, t in res["files"]]


def _join(tokens):
    s = ""
    for t in tokens:
        if s and not re.match(r"^[,.!?…:;)\]»”]", t):
            s += " "
        s += t
    return s


# ------------------------------------------------------------------ import


def _extract_json(text):
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if m:
        text = m.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise EditError("У файлі немає JSON-об'єкта.")
    try:
        return json.loads(text[start:end + 1])
    except ValueError as e:
        raise EditError("JSON не розпізнано: %s" % e)


def expand_refs(engine, refs):
    """["A0012", "A0013-A0015", {"from": .., "to": ..}] -> ids + warnings."""
    out, warn = [], []
    if isinstance(refs, str):
        refs = [x for x in re.split(r"[,\s]+", refs) if x]
    for r in refs or []:
        if isinstance(r, dict):
            r = "%s-%s" % (r.get("from") or r.get("id"), r.get("to") or r.get("from") or r.get("id"))
        r = str(r).strip()
        m = re.match(r"^([A-Za-z]+)0*(\d+)\s*[-–—]\s*([A-Za-z]+)?0*(\d+)$", r)
        if m:
            tag1, n1, tag2, n2 = m.group(1).upper(), int(m.group(2)), (m.group(3) or m.group(1)).upper(), int(m.group(4))
            if tag1 != tag2 or n2 < n1:
                warn.append("Діапазон %s некоректний" % r)
                continue
            for n in range(n1, n2 + 1):
                fid = "%s%04d" % (tag1, n)
                if engine.parse_fid(fid):
                    out.append(fid)
                else:
                    warn.append("Фрагмент %s не знайдено" % fid)
            continue
        parsed = engine.parse_fid(r)
        if parsed:
            out.append("%s%04d" % (engine.tags[parsed[0]], parsed[1] + 1))
        else:
            warn.append("Фрагмент %s не знайдено" % r)
    return out, warn


def import_beats(engine, data):
    out, warn = [], []
    for bt in data.get("beats") or []:
        ids, w = expand_refs(engine, bt.get("fragments") or [])
        warn += w
        if not ids:
            continue
        first, last = engine.parse_fid(ids[0]), engine.parse_fid(ids[-1])
        tr = engine.transcripts[first[0]]
        t0 = tr.words[tr.frags[first[1]][0]][0]
        out.append({"fragments": "%s-%s" % (short_id(engine, first[0], first[1]), short_id(engine, last[0], last[1]))
                    if len(ids) > 1 else short_id(engine, first[0], first[1]),
                    "order": (engine.tags[first[0]], first[1]), "time": _clock(t0),
                    "title": bt.get("title") or "", "summary": bt.get("summary") or "", "type": bt.get("type") or "",
                    "strength": bt.get("strength"), "people": bt.get("people") or [], "quote": bt.get("quote") or ""})
    with engine._lock:
        have = {b["fragments"]: b for b in engine.project_state.get("beats") or []}
        for b in out:
            have[b["fragments"]] = b
        merged = sorted(have.values(), key=lambda b: tuple(b["order"]))
        engine.project_state["beats"] = merged
        engine.save_project()
    return {"kind": "beats", "count": len(out), "total": len(merged), "warnings": warn[:10]}


def import_any(engine, text):
    data = _extract_json(text) if isinstance(text, str) else text
    if isinstance(data, dict) and (data.get("format") == TOPICS_FORMAT or ("topics" in data and "stories" not in data)):
        return import_topics(engine, data)
    if isinstance(data, dict) and (data.get("format") == BEATS_FORMAT or ("beats" in data and "stories" not in data)):
        return import_beats(engine, data)
    res = import_stories(engine, data)
    return {"kind": "stories", "imported": [x["uid"] for x in res]}


def import_stories(engine, text):
    data = _extract_json(text) if isinstance(text, str) else text
    stories = data.get("stories") if isinstance(data, dict) else None
    if stories is None and isinstance(data, dict) and data.get("scenes"):
        stories = [data]
    if not isinstance(stories, list) or not stories:
        raise EditError("Очікується JSON з масивом \"stories\" (формат %s)." % STORIES_FORMAT)
    imported = []
    for n, s in enumerate(stories, 1):
        warnings = []
        scenes = []
        for m, sc in enumerate(s.get("scenes") or [], 1):
            ids, w = expand_refs(engine, sc.get("fragments") or sc.get("fragment_ids") or [])
            warnings += ["Сцена %d: %s" % (m, x) for x in w]
            vo = sc.get("voiceover")
            if isinstance(vo, str):
                vo = {"text": vo, "placement": "over"}
            if vo and not (vo.get("text") or "").strip():
                vo = None
            role = str(sc.get("role") or "other").lower().strip()
            scenes.append({"uid": uuid.uuid4().hex[:8], "id": str(sc.get("id") or "s%d" % m),
                           "title": sc.get("title") or "Сцена %d" % m, "summary": sc.get("summary") or "",
                           "role": role if role in ROLES else "other", "role_label": role,
                           "role_note": sc.get("role_note") or sc.get("purpose") or "",
                           "fragments": ids, "edit_notes": sc.get("edit_notes") or sc.get("editing") or "",
                           "voiceover": vo, "transition": sc.get("transition") or "",
                           "context": sc.get("context") or "", "enabled": True})
        if not scenes:
            warnings.append("Варіант без сцен")
        # the model often misjudges length: compare what it picked with what the user asked for
        actual = _span_seconds(engine, [f for sc in scenes for f in sc["fragments"]])
        lo, hi, _ = brief_range(get_brief(engine))
        if scenes and actual and (actual < lo * 0.8 or actual > hi * 1.25):
            warnings.insert(0, "Тривалість ≈ %s, а замовлено %s–%s" % (_len_text(actual), _len_text(lo), _len_text(hi)))
        imported.append({"uid": uuid.uuid4().hex[:10], "id": str(s.get("id") or "v%d" % n),
                         "title": s.get("title") or "Варіант %d" % n, "logline": s.get("logline") or "",
                         "concept": s.get("concept") or s.get("description") or "",
                         "target_duration_sec": s.get("duration_sec") or s.get("target_duration_sec"),
                         "duration_sec": round(actual),
                         "editing_notes": s.get("editing_notes") or [], "music_and_sound": s.get("music_and_sound") or "",
                         "ending": s.get("ending") or "", "scenes": scenes, "warnings": warnings,
                         "imported_at": time.strftime("%Y-%m-%d %H:%M"), "source": data.get("source")})
    with engine._lock:
        engine.project_state.setdefault("stories", [])
        engine.project_state["stories"] = imported + engine.project_state["stories"]
        engine.save_project()
    return imported


def find_story(engine, uid):
    for s in engine.project_state.get("stories", []):
        if s["uid"] == uid:
            return s
    raise EditError("Варіант не знайдено.")


def update_story(engine, uid, patch):
    with engine._lock:
        s = find_story(engine, uid)
        if "scene_order" in patch:
            by = {sc["uid"]: sc for sc in s["scenes"]}
            s["scenes"] = [by[u] for u in patch["scene_order"] if u in by] + [sc for sc in s["scenes"] if sc["uid"] not in patch["scene_order"]]
        for sc_patch in patch.get("scenes", []):
            sc = next((x for x in s["scenes"] if x["uid"] == sc_patch.get("uid")), None)
            if sc:
                for k in ("enabled", "title", "summary", "edit_notes", "role_note", "context", "transition"):
                    if k in sc_patch:
                        sc[k] = sc_patch[k]
                if "voiceover" in sc_patch:
                    t = (sc_patch["voiceover"] or "").strip()
                    sc["voiceover"] = {"text": t, "placement": (sc.get("voiceover") or {}).get("placement", "over")} if t else None
        if patch.get("delete"):
            engine.project_state["stories"] = [x for x in engine.project_state["stories"] if x["uid"] != uid]
        engine.save_project()
        return s


# ------------------------------------------------------------------ apply


def _media_fps(engine, key):
    for it in (engine.link.snapshot or {}).get("tracks", {}).get(engine.track, []):
        if it.get("path") and engine.transcripts.get(key) and it["path"].lower() == engine.transcripts[key].path.lower():
            return it["mfps"]
    tr = engine.transcripts[key]
    return float(tr.meta.get("fps") or (engine.link.snapshot or {}).get("fps") or 25)


def scene_ranges(engine, scene):
    """Merge consecutive fragments of a scene into source ranges."""
    ranges = []
    for fid in scene["fragments"]:
        parsed = engine.parse_fid(fid)
        if not parsed:
            continue
        key, n = parsed
        tr = engine.transcripts[key]
        a, z = tr.frags[n]
        if z <= a:
            continue
        if ranges and ranges[-1]["key"] == key and ranges[-1]["n"] == n - 1:
            ranges[-1]["n"] = n
            ranges[-1]["t1"] = tr.gap_after(z - 1)
            continue
        ranges.append({"key": key, "n": n, "t0": tr.gap_before(a), "t1": tr.gap_after(z - 1)})
    clips = []
    for r in ranges:
        tr = engine.transcripts[r["key"]]
        mfps = _media_fps(engine, r["key"])
        s, e = int(round(r["t0"] * mfps)), int(round(r["t1"] * mfps))
        if e > s:
            clips.append({"path": tr.path, "s": s, "e": e, "mfps": mfps})
    return clips


def story_plan(engine, story):
    scenes = [sc for sc in story["scenes"] if sc.get("enabled", True)]
    out = []
    for sc in scenes:
        clips = scene_ranges(engine, sc)
        if clips:
            out.append((sc, clips))
    if not out:
        raise EditError("У варіанті немає сцен із фрагментами, що є в транскрипції.")
    return out


def _markers(engine, plan, fps, offset0):
    ops, pos = [], offset0
    for sc, clips in plan:
        dur = sum(int(round((c["e"] - c["s"]) * fps / c["mfps"])) for c in clips)
        note = (sc.get("summary") or "")
        if sc.get("edit_notes"):
            note += "\nМонтаж: " + sc["edit_notes"]
        if sc.get("context"):
            note += "\nКонтекст: " + sc["context"]
        ops.append({"op": "marker", "f": pos, "color": ROLE_COLORS.get(sc["role"], "Blue"), "name": sc["title"][:60],
                    "note": note[:1500], "dur": max(1, dur), "data": "dd:" + sc["uid"], "soft": True})
        vo = sc.get("voiceover")
        if vo and vo.get("text"):
            ops.append({"op": "marker", "f": pos + 1, "color": "Yellow", "name": "VO: " + vo["text"][:50],
                        "note": vo["text"][:1500], "dur": 1, "data": "dd-vo:" + sc["uid"], "soft": True})
        pos += dur
    return ops


def apply_story(engine, uid, mode="new", markers=True):
    """mode: "new" — a new timeline next to the current one; "replace" — rebuild the edit track."""
    story = find_story(engine, uid)
    plan = story_plan(engine, story)
    clips = [c for _, cs in plan for c in cs]
    snap = engine.link.snapshot
    if not snap:
        raise EditError("Немає активного таймлайна в Resolve.")
    fps = snap["fps"]
    with engine._lock:
        if mode == "new":
            name = "%s — %s" % (snap.get("tl") or "Timeline", story["title"])
            name = re.sub(r"[\\/:*?\"<>|]", " ", name)[:120]
            existing = {t.get("name") for t in (engine.link.info.get("timelines") or [])}
            base, k = name, 2
            while name in existing:
                name = "%s (%d)" % (base, k)
                k += 1
            ops = [{"op": "new_timeline", "name": name},
                   {"op": "append", "clips": [{"path": c["path"], "s": c["s"], "e": c["e"], "track": 1} for c in clips]}]
            if markers:
                ops += _markers(engine, plan, fps, 0)
            try:
                res = engine.link.run(ops, timeout=120)
            except Exception as e:
                raise EditError(str(e))
            engine.rebuild()
            if not res.get("ok"):
                raise EditError("Resolve: %s" % res.get("err"))
            engine.undo_stack.append({"label": "Сюжет", "undo": {"op": "timeline", "id": snap.get("tlid")},
                                      "fp": _sig(engine)})
            return res
        p = engine._planner()
        start = p.items[0]["s"] if p.items else snap["start"]
        if p.items:
            p.delete(start, p.items[-1]["e"])
        # sequential append (no record frames): Resolve lays the clips end to end itself,
        # so frame-rate rounding cannot create overlaps
        p.ops.append({"op": "append", "clips": [{"path": c["path"], "s": c["s"], "e": c["e"], "track": p.tindex}
                                                for c in clips]})
        if markers:
            p.ops += _markers(engine, plan, fps, start - snap["start"])
        return engine._exec(p, None, "Сюжет")


def _sig(engine):
    from .engine import content_sig
    return content_sig(engine.link.snapshot, engine.track)


def apply_scene(engine, uid, scene_uid, where="playhead"):
    story = find_story(engine, uid)
    sc = next((x for x in story["scenes"] if x["uid"] == scene_uid), None)
    if not sc:
        raise EditError("Сцену не знайдено.")
    clips = scene_ranges(engine, sc)
    if not clips:
        raise EditError("Фрагменти сцени не знайдено в транскрипції.")
    blocks = engine.view["blocks"]
    if where == "end" or not blocks or engine.link.playhead is None:
        f = blocks[-1]["e"] if blocks else engine.view["start"]
    else:
        ph = engine.link.playhead
        bounds = sorted({b["s"] for b in blocks} | {b["e"] for b in blocks})
        f = min(bounds, key=lambda x: abs(x - ph))
    return engine.insert_clips(f, clips, label="Сцена")


def locate_fragment(engine, fid):
    """Timeline frame of a fragment if it is on the timeline."""
    parsed = engine.parse_fid(fid)
    if not parsed:
        raise EditError("Фрагмент %s не знайдено." % fid)
    key, n = parsed
    a, z = engine.transcripts[key].frags[n]
    for b in engine.view["blocks"]:
        if b["m"] == key and b["i0"] < z and b["i1"] > a:
            w = max(a, b["i0"])
            return engine._frame(b, engine.transcripts[key].words[w][0])
    return None
