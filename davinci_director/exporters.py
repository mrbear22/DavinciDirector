"""Exports: SRT subtitles, plain-text transcript, CMX3600 EDL, story outline."""

from .model import fmt_ts
from .resolve_ops import frames_to_timecode, timecode_to_frames


def _srt_ts(sec):
    ms = int(round(max(0.0, sec) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def edit_words(project):
    """Kept words in edit order with their time on the edited timeline."""
    out = []
    toks = project.tokens
    for seg in project.render():
        for part in seg["parts"]:
            p = project.piece(part["pid"])
            cut = set(p["cut"])
            for i in range(part["a"], part["b"] + 1):
                if i in cut:
                    continue
                t = toks[i]
                if t["k"] != "w":
                    continue
                t0 = seg["r0"] + (t["t0"] - seg["s0"])
                out.append((t0, t0 + (t["t1"] - t["t0"]), t))
    return out


def source_words(project):
    return [(t["r0"], t["r1"], t) for t in project.tokens if t["k"] == "w"]


def srt(project, scope="edit", max_chars=42, max_dur=5.0):
    words = edit_words(project) if scope == "edit" else source_words(project)
    cues, cur = [], []
    for w in words:
        if cur:
            text = " ".join(x[2]["w"] for x in cur + [w])
            if (len(text) > max_chars or w[1] - cur[0][0] > max_dur or w[0] - cur[-1][1] > 0.8
                    or w[2].get("sp") != cur[-1][2].get("sp")):
                cues.append(cur)
                cur = []
        cur.append(w)
        if w[2]["w"].endswith((".", "!", "?", "…")) and len(" ".join(x[2]["w"] for x in cur)) > max_chars * 0.6:
            cues.append(cur)
            cur = []
    if cur:
        cues.append(cur)
    lines = []
    for n, cue in enumerate(cues, 1):
        lines.append("%d\n%s --> %s\n%s\n" % (n, _srt_ts(cue[0][0]), _srt_ts(cue[-1][1]),
                                              " ".join(x[2]["w"] for x in cue)))
    return "\n".join(lines)


def text(project, scope="edit"):
    speakers = project.data["speakers"]
    out = []
    toks = project.tokens
    pieces = project.pieces if scope == "edit" else [{"a": 0, "b": len(toks) - 1, "cut": [], "off": False, "label": ""}]
    for p in pieces:
        if p["off"]:
            continue
        cut = set(p["cut"])
        cur_sp, buf, start = None, [], None
        for i in range(p["a"], p["b"] + 1):
            t = toks[i]
            if i in cut or t["k"] != "w":
                continue
            if t.get("sp") != cur_sp and buf:
                out.append("[%s] %s: %s" % (fmt_ts(start), speakers.get(cur_sp, {}).get("name", cur_sp), " ".join(buf)))
                buf = []
            if not buf:
                start = t["r0"]
            cur_sp = t.get("sp")
            buf.append(t["w"])
        if buf:
            out.append("[%s] %s: %s" % (fmt_ts(start), speakers.get(cur_sp, {}).get("name", cur_sp), " ".join(buf)))
        if p.get("label"):
            out.append("")
    return "\n".join(out) + "\n"


def edl(project):
    """CMX3600 EDL of the edit (fallback: File > Import > Timeline in Resolve)."""
    frames = project.render_frames()
    tfps = project.data["resolve"].get("fps") or 25.0
    title = (project.data["resolve"].get("timeline") or "EDIT") + " DD EDIT"
    lines = ["TITLE: %s" % title[:70], "FCM: NON-DROP FRAME", ""]
    rec_base = int(round(3600 * tfps))  # 01:00:00:00
    clips = project.data["clips"]
    for n, f in enumerate(frames, 1):
        clip = next(c for c in clips if c["media_id"] == f["media_id"])
        mfps = clip.get("media_fps") or tfps
        try:
            tc0 = timecode_to_frames(clip.get("start_tc") or "00:00:00:00", tfps)
        except ValueError:
            tc0 = 0
        s_in = tc0 + int(round(f["start"] * tfps / mfps))
        s_out = s_in + f["rec_dur"]
        r_in = rec_base + f["rec"]
        r_out = r_in + f["rec_dur"]
        reel = "AX"
        lines.append("%03d  %-8s AA/V  C        %s %s %s %s" % (
            n, reel, frames_to_timecode(s_in, tfps), frames_to_timecode(s_out, tfps),
            frames_to_timecode(r_in, tfps), frames_to_timecode(r_out, tfps)))
        lines.append("* FROM CLIP NAME: %s" % clip.get("media_name", ""))
        lines.append("")
    return "\n".join(lines)


def story_markdown(project, variant_id=None):
    out = []
    for an in project.data["analyses"]:
        for v in an["variants"]:
            if variant_id and v["id"] != variant_id:
                continue
            out.append("# %s\n\n%s\n" % (v.get("title", ""), v.get("logline", "")))
            if v.get("why_it_works"):
                out.append("**Чому спрацює:** %s\n" % v["why_it_works"])
            for n, sc in enumerate(v.get("scenes", []), 1):
                rng = ", ".join("%s–%s" % (fmt_ts(r["start"]), fmt_ts(r["end"])) for r in sc.get("ranges", []))
                out.append("## %d. %s — %s\n`%s`\n\n%s\n" % (n, sc.get("role", "").upper(), sc.get("title", ""), rng,
                                                           sc.get("description", "")))
                if sc.get("edit_notes"):
                    out.append("- Монтаж: %s" % sc["edit_notes"])
                if sc.get("techniques"):
                    out.append("- Прийоми: %s" % ", ".join(sc["techniques"]))
                for vo in sc.get("voiceover", []):
                    out.append("- VO @%s: «%s»" % (fmt_ts(vo["at"]), vo["text"]))
                out.append("")
            if v.get("ending"):
                out.append("**Завершення:** %s\n" % v["ending"])
    return "\n".join(out)
