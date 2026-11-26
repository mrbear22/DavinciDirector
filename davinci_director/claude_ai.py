"""Claude integration: story analysis and the context-aware chat.

Uses the official ``anthropic`` Python SDK (Messages API, streaming,
adaptive thinking, structured outputs, prompt caching on the transcript and
server-side refusal fallbacks).
"""

import json
import time

from .model import fmt_ts, initial_pieces, new_id, parse_ts

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"

ROLES = ["hook", "setup", "development", "escalation", "climax", "resolution", "outro",
         "context", "callback", "montage", "other"]
MOMENT_TYPES = ["funny", "unexpected", "tense", "emotional", "epic", "fail", "wholesome", "skill", "drama", "other"]

SYSTEM_PROMPT = """Ти — досвідчений YouTube-сценарист і відеомонтажер, що спеціалізується на перетворенні довгих записів ігрових стрімів на захопливі ролики. Ти працюєш всередині аддону DaVinci Director для DaVinci Resolve і бачиш повну транскрипцію запису з часовими мітками.

Як читати транскрипцію:
- Кожен абзац починається з часового діапазону [ГГ:ХХ:СС.с–ГГ:ХХ:СС.с] на ОРИГІНАЛЬНОМУ таймлайні та імені мовця.
- {ГГ:ХХ:СС} всередині тексту — проміжні мітки для точнішого позиціонування.
- [пауза Nс] — відрізок без мови: найчастіше чистий геймплей, катсцена чи напружена тиша. Такі відрізки можуть бути важливими (кульмінація бою без слів).
- ⚡ перед словом або паузою означає різкий сплеск гучності (крик, вибух, сміх, удар) — часто ознака сильного моменту.

Жорсткі правила:
1. Спирайся ВИКЛЮЧНО на реальні події з транскрипції. Не вигадуй ситуацій, реплік, реакцій чи подій, яких немає в записі. Якщо щось лише ймовірно (наприклад, що відбувається на екрані під час паузи), прямо познач це як припущення.
2. Кожна рекомендація має містити часові мітки оригінального відео у форматі ГГ:ХХ:СС.с, що точно відповідають транскрипції. Початок діапазону став трохи до першого потрібного слова, кінець — після останнього.
3. Не розтягуй ролик штучно. Якщо матеріалу мало — запропонуй коротший, але цілісний сюжет.
4. Структуру (hook / setup / development / escalation / climax / resolution …) обирай під матеріал, а не за шаблоном. Ролі сцен: hook, setup, development, escalation, climax, resolution, outro, context, callback, montage, other.
5. Hook — найсильніший короткий момент (зазвичай 3–20 с), який інтригує, але не спойлерить розв'язку повністю. Hook може бути взятий з кінця запису.
6. Закадровий текст (voice-over) пиши коротко, розмовно, лише там, де глядачу справді бракує контексту, і без вигадок про події.
7. Відповідай українською, якщо користувач не пише іншою мовою. Цитати з запису наводь мовою оригіналу.
"""

ANALYSIS_TASK = """Проаналізуй увесь запис і запропонуй {n} різні концепції майбутнього YouTube-відео.

Для аналізу:
- знайди сюжетні лінії, смішні, несподівані, напружені, емоційні та епічні моменти (moments) з оцінкою сили 1–5;
- для кожної концепції: назва, логлайн, чому це спрацює, тон, орієнтовна тривалість, варіанти hook;
- послідовність сцен: роль, назва, опис, точні діапазони (ranges) оригінального відео (кілька діапазонів у сцені — якщо треба вирізати зайве всередині), коментарі щодо монтажу, прийоми (переходи, зум, звукові акценти, текст на екрані, мем-вставки, слоу-мо тощо), закадровий текст з місцем вставки;
- слабкі або зайві фрагменти, які варто скоротити (weak_parts);
- місця, де глядачу бракує контексту, і як це виправити (missing_context);
- ідея завершення (ending).
Діапазони сцен однієї концепції не повинні перекриватися. Порядок сцен — порядок у майбутньому відео (не обов'язково хронологічний).
{extra}"""

# ------------------------------------------------------------------ schema

_TS = {"type": "string", "description": "Часова мітка оригінального відео ГГ:ХХ:СС.с"}


def _obj(props, desc=None):
    o = {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}
    if desc:
        o["description"] = desc
    return o


RANGE = _obj({"start": _TS, "end": _TS})
SCENE = _obj({
    "role": {"type": "string", "enum": ROLES},
    "title": {"type": "string"},
    "description": {"type": "string"},
    "ranges": {"type": "array", "items": RANGE},
    "edit_notes": {"type": "string"},
    "techniques": {"type": "array", "items": {"type": "string"}},
    "voiceover": {"type": "array", "items": _obj({"at": _TS, "text": {"type": "string"}})},
})
VARIANT = _obj({
    "title": {"type": "string"},
    "logline": {"type": "string"},
    "why_it_works": {"type": "string"},
    "tone": {"type": "string"},
    "estimated_duration": {"type": "string"},
    "hook_options": {"type": "array", "items": _obj({"start": _TS, "end": _TS, "why": {"type": "string"}})},
    "scenes": {"type": "array", "items": SCENE},
    "weak_parts": {"type": "array", "items": _obj({"start": _TS, "end": _TS, "reason": {"type": "string"}})},
    "missing_context": {"type": "array", "items": _obj({"at": _TS, "problem": {"type": "string"}, "fix": {"type": "string"}})},
    "ending": {"type": "string"},
})
ANALYSIS_SCHEMA = _obj({
    "overview": {"type": "string"},
    "moments": {"type": "array", "items": _obj({
        "start": _TS, "end": _TS, "type": {"type": "string", "enum": MOMENT_TYPES},
        "description": {"type": "string"}, "strength": {"type": "integer"}})},
    "variants": {"type": "array", "items": VARIANT},
})

SAVE_TOOL = {
    "name": "save_story_variant",
    "description": ("Зберегти варіант сюжету (повну послідовність сцен) у редакторі структури користувача. "
                    "Використовуй, коли користувач просить перебудувати сюжет, запропонувати інший початок, "
                    "кульмінацію, скоротити частину тощо — тобто коли результат є новою або зміненою структурою. "
                    "replace_variant_id: id наявного варіанта, який треба замінити, або порожній рядок для нового."),
    "input_schema": _obj({"replace_variant_id": {"type": "string"}, "variant": VARIANT}),
    "strict": True,
    "eager_input_streaming": True,
}


# ------------------------------------------------------------------ transcript

def transcript_for_ai(project):
    """Readable transcript of the ORIGINAL recording with timestamps."""
    toks = project.tokens
    speakers = project.data["speakers"]
    lines = []
    for p in initial_pieces(toks):
        a, b = p["a"], p["b"]
        parts = []
        last_mark = toks[a]["r0"]
        sp = None
        i = a
        while i <= b:
            t = toks[i]
            if t["k"] == "g":
                dur, loud = 0.0, False
                while i <= b and toks[i]["k"] == "g":
                    dur += toks[i]["t1"] - toks[i]["t0"]
                    loud = loud or toks[i].get("hl", 0) >= 2
                    i += 1
                parts.append("%s[пауза %.0fс]" % ("⚡" if loud else "", dur))
                continue
            i += 1
            sp = sp or t.get("sp")
            if t["r0"] - last_mark >= 15:
                parts.append("{%s}" % fmt_ts(t["r0"])[:8])
                last_mark = t["r0"]
            parts.append(("⚡" if t.get("hl", 0) >= 2 else "") + t["w"])
        name = speakers.get(sp, {}).get("name", sp) if sp else "—"
        lines.append("[%s–%s] %s: %s" % (fmt_ts(toks[a]["r0"]), fmt_ts(toks[b]["r1"]), name, " ".join(parts)))
    total = toks[-1]["r1"] if toks else 0
    head = "ТРАНСКРИПЦІЯ ЗАПИСУ «%s» (тривалість %s)\nМовці: %s\n\n" % (
        project.data["resolve"].get("timeline", ""), fmt_ts(total),
        ", ".join("%s" % v["name"] for v in speakers.values()) or "—")
    return head + "\n".join(lines)


def stories_for_ai(project):
    out = []
    link = project.data["edit"].get("story_link") or {}
    for an in project.data["analyses"]:
        for v in an["variants"]:
            mark = " (ЗАСТОСОВАНО ДО МОНТАЖУ)" if link.get("variant") == v["id"] else ""
            out.append("Варіант id=%s%s: «%s» — %s" % (v["id"], mark, v.get("title", ""), v.get("logline", "")))
            for n, sc in enumerate(v.get("scenes", []), 1):
                rng = ", ".join("%s–%s" % (fmt_ts(r["start"]), fmt_ts(r["end"])) for r in sc.get("ranges", []))
                out.append("  %d. [%s] %s (%s)%s" % (n, sc.get("role", ""), sc.get("title", ""), rng,
                                                    " [вимкнено]" if sc.get("disabled") else ""))
    return "\n".join(out) or "Сюжетних варіантів ще немає."


# ------------------------------------------------------------------ normalisation

def _clamp_range(project, start, end):
    total = project.tokens[-1]["r1"] if project.tokens else 0
    s = max(0.0, min(parse_ts(start), total))
    e = max(0.0, min(parse_ts(end), total))
    if e < s:
        s, e = e, s
    return s, e


def normalize_variant(project, raw, warnings):
    v = {"id": new_id("v"), "title": raw.get("title", "Без назви"), "logline": raw.get("logline", ""),
         "why_it_works": raw.get("why_it_works", ""), "tone": raw.get("tone", ""),
         "estimated_duration": raw.get("estimated_duration", ""), "ending": raw.get("ending", ""),
         "hook_options": [], "scenes": [], "weak_parts": [], "missing_context": []}
    for h in raw.get("hook_options", []) or []:
        try:
            s, e = _clamp_range(project, h["start"], h["end"])
            v["hook_options"].append({"start": s, "end": e, "why": h.get("why", "")})
        except (ValueError, KeyError):
            pass
    for sc in raw.get("scenes", []) or []:
        ranges = []
        for r in sc.get("ranges", []) or []:
            try:
                s, e = _clamp_range(project, r["start"], r["end"])
            except (ValueError, KeyError):
                warnings.append("Некоректна мітка в сцені «%s»" % sc.get("title", ""))
                continue
            if e - s < 0.2 or not project.tokens_in_range(s, e):
                warnings.append("Порожній діапазон у сцені «%s»" % sc.get("title", ""))
                continue
            ranges.append({"start": s, "end": e})
        vo = []
        for x in sc.get("voiceover", []) or []:
            try:
                vo.append({"at": parse_ts(x.get("at", 0)), "text": x.get("text", "")})
            except ValueError:
                vo.append({"at": ranges[0]["start"] if ranges else 0, "text": x.get("text", "")})
        role = sc.get("role", "other")
        v["scenes"].append({"id": new_id("s"), "role": role if role in ROLES else "other",
                            "title": sc.get("title", ""), "description": sc.get("description", ""),
                            "ranges": ranges, "edit_notes": sc.get("edit_notes", ""),
                            "techniques": list(sc.get("techniques", []) or []), "voiceover": vo})
    for key, fields in (("weak_parts", ("start", "end")), ):
        for w in raw.get(key, []) or []:
            try:
                s, e = _clamp_range(project, w[fields[0]], w[fields[1]])
                v[key].append({"start": s, "end": e, "reason": w.get("reason", "")})
            except (ValueError, KeyError):
                pass
    for m in raw.get("missing_context", []) or []:
        try:
            v["missing_context"].append({"at": parse_ts(m.get("at", 0)), "problem": m.get("problem", ""),
                                         "fix": m.get("fix", "")})
        except ValueError:
            pass
    return v


def normalize_analysis(project, raw, model, instructions=""):
    warnings = []
    an = {"id": new_id("a"), "created": time.time(), "model": model, "instructions": instructions,
          "overview": raw.get("overview", ""), "moments": [], "variants": [], "warnings": warnings}
    for m in raw.get("moments", []) or []:
        try:
            s, e = _clamp_range(project, m["start"], m["end"])
        except (ValueError, KeyError):
            continue
        an["moments"].append({"start": s, "end": e, "type": m.get("type", "other"),
                              "description": m.get("description", ""),
                              "strength": max(1, min(5, int(m.get("strength", 3) or 3)))})
    for raw_v in raw.get("variants", []) or []:
        an["variants"].append(normalize_variant(project, raw_v, warnings))
    return an


# ------------------------------------------------------------------ client

class ClaudeService:
    def __init__(self, settings, client_factory=None):
        self.settings = settings
        self._factory = client_factory

    def client(self):
        if self._factory:
            return self._factory()
        try:
            import anthropic
        except ImportError:
            raise RuntimeError("Пакет anthropic не встановлено: pip install anthropic")
        key = self.settings.get("anthropic_api_key")
        if key:
            return anthropic.Anthropic(api_key=key, max_retries=3)
        return anthropic.Anthropic(max_retries=3)  # env / `ant auth login` profile

    def _common(self):
        model = self.settings.get("claude_model") or DEFAULT_MODEL
        params = {
            "model": model,
            "max_tokens": 64000,
            "thinking": {"type": "adaptive", "display": "summarized"},
        }
        effort = self.settings.get("claude_effort") or "high"
        if model.startswith(("claude-opus-5-5", "claude-fable", "claude-opus-5", "claude-sonnet-5-5")):
            params["betas"] = [FALLBACK_BETA]
            params["fallbacks"] = "default"
        return model, params, effort

    def _system(self):
        return [{"type": "text", "text": SYSTEM_PROMPT}]

    @staticmethod
    def _transcript_block(project):
        return {"type": "text", "text": transcript_for_ai(project), "cache_control": {"type": "ephemeral", "ttl": "1h"}}

    def _stream(self, emit, **kwargs):
        """Run a streaming request; emit("thinking"|"text"|"tool_json", delta)."""
        client = self.client()
        with client.beta.messages.stream(**kwargs) as stream:
            for event in stream:
                if event.type == "content_block_delta":
                    d = event.delta
                    if d.type == "text_delta":
                        emit("text", d.text)
                    elif d.type == "thinking_delta":
                        emit("thinking", d.thinking)
                    elif d.type == "input_json_delta":
                        emit("tool_json", d.partial_json)
            msg = stream.get_final_message()
        if msg.stop_reason == "refusal":
            details = getattr(msg, "stop_details", None)
            raise RuntimeError("Claude відмовився виконати запит%s." % (
                ": %s" % details.explanation if details and getattr(details, "explanation", None) else ""))
        return msg

    # -------------------------------------------------------------- analysis
    def analyze(self, project, instructions="", variants=3, emit=lambda *a: None):
        model, params, effort = self._common()
        extra = ("\nДодаткові побажання користувача: " + instructions.strip()) if instructions.strip() else ""
        task = ANALYSIS_TASK.format(n=variants, extra=extra)
        msg = self._stream(
            emit,
            system=self._system(),
            messages=[{"role": "user", "content": [self._transcript_block(project), {"type": "text", "text": task}]}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": ANALYSIS_SCHEMA}},
            **params,
        )
        if msg.stop_reason == "max_tokens":
            raise RuntimeError("Відповідь Claude обрізано (max_tokens). Спробуйте менше варіантів.")
        text = "".join(b.text for b in msg.content if b.type == "text")
        try:
            raw = json.loads(text)
        except ValueError:
            raise RuntimeError("Claude повернув некоректний JSON.")
        an = normalize_analysis(project, raw, msg.model, instructions)
        an["usage"] = _usage(msg)
        return an

    # -------------------------------------------------------------- chat
    def chat(self, project, user_text, emit=lambda *a: None, on_variant=None):
        """One chat turn.  ``project.data['chat']`` keeps both the display log
        and the exact API history (append-only, without thinking blocks)."""
        model, params, effort = self._common()
        chat = project.data.setdefault("chat", [])
        api_history = project.data.setdefault("chat_api", [])
        state = ("<поточний_стан>\n%s\n\nСюжетні варіанти:\n%s\n</поточний_стан>\n\n" %
                 (project.edit_summary_for_ai(), stories_for_ai(project)))
        user_msg = {"role": "user", "content": [{"type": "text", "text": state + user_text}]}
        chat.append({"role": "user", "text": user_text, "ts": time.time()})
        messages = [{"role": "user", "content": [self._transcript_block(project),
                                                 {"type": "text", "text": "Це транскрипція запису, з яким ми працюємо."}]},
                    {"role": "assistant", "content": [{"type": "text", "text": "Зрозумів. Я бачу транскрипцію і готовий допомагати з сюжетом і монтажем."}]}]
        messages += api_history + [user_msg]
        turn_new = [user_msg]
        reply_text = []
        saved = []
        for _ in range(6):
            msg = self._stream(emit, system=self._system(), messages=messages, tools=[SAVE_TOOL],
                               output_config={"effort": effort}, **params)
            content = [b.model_dump(exclude_none=True) for b in msg.content]
            messages.append({"role": "assistant", "content": content})
            turn_new.append({"role": "assistant", "content": [c for c in content if c["type"] not in ("thinking", "redacted_thinking", "fallback")]})
            reply_text += [b.text for b in msg.content if b.type == "text"]
            if msg.stop_reason != "tool_use":
                break
            results = []
            for b in msg.content:
                if b.type != "tool_use":
                    continue
                inp = b.input if isinstance(b.input, dict) else {}
                if b.name != SAVE_TOOL["name"] or not isinstance(inp.get("variant"), dict) or not inp["variant"].get("scenes"):
                    results.append({"type": "tool_result", "tool_use_id": b.id, "is_error": True,
                                    "content": "INVALID_JSON: очікується {replace_variant_id, variant{..., scenes[...]}}"})
                    continue
                warnings = []
                variant = normalize_variant(project, inp["variant"], warnings)
                vid = on_variant(variant, inp.get("replace_variant_id") or "") if on_variant else variant["id"]
                saved.append(vid)
                emit("variant", vid)
                results.append({"type": "tool_result", "tool_use_id": b.id,
                                "content": "Збережено як варіант id=%s (%d сцен).%s" % (
                                    vid, len(variant["scenes"]), (" Попередження: " + "; ".join(warnings[:5])) if warnings else "")})
            tool_msg = {"role": "user", "content": results}
            messages.append(tool_msg)
            turn_new.append(tool_msg)
        else:
            reply_text.append("\n(Зупинено після кількох викликів інструментів.)")
        # stored history must end with an assistant turn (no dangling tool_use)
        if turn_new[-1]["role"] == "user":
            turn_new.append({"role": "assistant", "content": [{"type": "text", "text": "Готово."}]})
        api_history.extend(turn_new)
        text = "\n".join(t for t in reply_text if t).strip()
        chat.append({"role": "assistant", "text": text, "ts": time.time(), "variants": saved, "usage": _usage(msg)})
        return text, saved


def _usage(msg):
    u = getattr(msg, "usage", None)
    if not u:
        return {}
    return {k: getattr(u, k, None) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                             "cache_creation_input_tokens")}
