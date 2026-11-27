// The text of the timeline as data (no DOM): what the editor shows and what a selection means.
//
// It reads like a document: paragraphs end at long pauses (or when they get long), not at every change of
// speaker; a speaker's words form a coloured run.  Topic headings start new sections.  Every word, pause,
// cut word and clip without speech is a *token*; selection, dragging and search work on whole tokens.
//
// Token kinds: "w" word on the timeline, "p" pause, "clip" clip without transcript/speech (these three are
// *live*: they play), "cut" a word no clip uses, "chip" a collapsed run of cut words.

import { tc, dur, nWords } from "./util.js";
import { speaker, wordFrame } from "./store.js";

export const PAUSE_MIN = 3.0;     // s: shorter pauses are not shown
export const PARA_PAUSE = 6.0;    // s: a pause this long starts a paragraph
export const PARA_WORDS = 90;     // a paragraph this long ends at the next sentence end
export const CUT_COLLAPSE = 24;   // more cut words in a row are shown as one chip
const GREY = "#9a9a9e";
const SENTENCE_END = /[.!?…]$/;

export const LIVE = new Set(["w", "p", "clip"]);

// topics from the language model: the one a word belongs to = the last topic starting at or before it
function topicFinder(topics) {
  const by = {};
  for (const tp of topics || []) (by[tp.m] = by[tp.m] || []).push(tp);
  for (const k in by) by[k].sort((x, y) => x.i - y.i);
  return (m, i) => {
    const arr = by[m];
    let lo = 0, hi = arr ? arr.length - 1 : -1, best = null;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (arr[mid].i <= i) { best = arr[mid]; lo = mid + 1; } else hi = mid - 1;
    }
    return best;
  };
}

// The document: nodes = [{type: "topic", title, summary} | {type: "para", frame, runs: [{color, items}]}
// | {type: "silence", token, sec, frame}],
// items = token indexes or {splice: title}.  Built in one pass over the blocks of the view.
class Builder {
  constructor(src) {
    this.src = src;
    this.fps = src.view.fps;
    this.topicAt = topicFinder(src.topics);
    this.tokens = [];
    this.wordTok = new Map();   // "block:word" -> token index
    this.blockTok = [];         // block -> [first token, end)
    this.nodes = [];
    this.para = null;
    this.run = null;
    this.speakerKey = null;
    this.paraWords = 0;
    this.topic = null;
  }

  startPara(color, frame) {
    this.para = { type: "para", frame, runs: [] };
    this.nodes.push(this.para);
    this.paraWords = 0;
    this.speakerKey = null;
    this.startRun(color);
  }

  startRun(color) {
    if (!this.para) return this.startPara(color, null);
    this.run = { color, items: [] };
    this.para.runs.push(this.run);
  }

  put(item) {
    if (!this.para) this.startPara(GREY, null);  // before the first paragraph (cut words, a splice)
    this.run.items.push(item);
  }

  token(t) {
    this.tokens.push(t);
    this.put(this.tokens.length - 1);
  }

  heading(tp) {
    this.topic = tp;
    this.nodes.push({ type: "topic", title: tp.title, summary: tp.summary });
    this.para = null;
  }

  // a short pause stays in the line; a silent moment gets a row of its own between paragraphs
  pause(b, k, w, sec) {
    const t = { kind: "p", b: k, w, sec, text: `${Math.round(sec)} с`,
      title: `Пауза ${dur(sec)} без мови (на стрімі — геймплей). Можна виділити й вирізати, як слова.` };
    if (sec < PARA_PAUSE) { this.token(t); return; }
    const words = this.src.tr[b.m].words;
    this.tokens.push(Object.assign(t, { text: `тиша ${dur(sec)}`, silence: true }));
    this.nodes.push({ type: "silence", token: this.tokens.length - 1, sec,
      frame: w < b.i0 ? b.s : wordFrame(b, words[w][1]) });
    this.para = null;  // the speech after it starts a new paragraph
  }

  cutRun(k, c, key) {
    if (!this.src.showCut) return;
    const words = this.src.tr[key].words, n = c.i1 - c.i0, id = `${key}:${c.i0}:${c.i1}`;
    if (n > CUT_COLLAPSE && !this.src.expanded.has(id)) {
      this.token({ kind: "chip", k, i0: c.i0, i1: c.i1, id,
        text: `вирізано ${nWords(n)} · ${dur(words[c.i1 - 1][1] - words[c.i0][0])}`,
        title: "Клік — показати. Виділіть і натисніть Del, щоб повернути у відео." });
      return;
    }
    for (let i = c.i0; i < c.i1; i++) this.token({ kind: "cut", k, i, text: words[i][2] });
  }

  word(b, k, i) {
    const t = this.src.tr[b.m], words = t.words, w = words[i];
    const tp = this.topicAt(b.m, i);
    if (tp && tp !== this.topic) this.heading(tp);
    const gap = i > b.i0 ? w[0] - words[i - 1][1] : 0;
    const longPara = this.paraWords >= PARA_WORDS && i > 0 && SENTENCE_END.test(words[i - 1][2]);
    if (!this.para || gap >= PARA_PAUSE || longPara) this.startPara(speaker(b.m, w[3]).color, wordFrame(b, w[0]));
    const sp = b.m + ":" + w[3];  // speaker of this file (another file numbers its speakers on its own)
    if (sp !== this.speakerKey) {
      this.startRun(speaker(b.m, w[3]).color);
      this.speakerKey = sp;
    }
    this.wordTok.set(k + ":" + i, this.tokens.length);
    this.token({ kind: "w", b: k, w: i, text: w[2], low: w.length > 4 && w[4] < 0.45,
      title: `${t.tag}${this.src.fragOf[b.m][i] + 1} · ${tc(wordFrame(b, w[0]), this.fps)}` });
    this.paraWords++;
    const next = i + 1 < b.i1 ? words[i + 1][0] : b.src1;
    if (next - w[1] >= PAUSE_MIN) this.pause(b, k, i, next - w[1]);
  }

  block(b, k) {
    const first = this.tokens.length;
    if (k > 0) this.put({ splice: `Склейка · ${tc(b.s, this.fps)}` });
    if (!b.m || !this.src.tr[b.m]) {
      this.startPara(GREY, b.s);
      this.token({ kind: "clip", b: k, text: `${b.name} · ${dur((b.e - b.s) / this.fps)} · без транскрипції` });
    } else {
      this.speechBlock(b, k);
    }
    this.blockTok[k] = [first, this.tokens.length];
  }

  speechBlock(b, k) {
    const words = this.src.tr[b.m].words;
    for (const c of b.cut.filter((c) => c.side === "before")) this.cutRun(k, c, b.m);
    const firstT0 = b.i1 > b.i0 ? words[b.i0][0] : b.src1;
    const leadPause = firstT0 - b.src0 >= PAUSE_MIN;
    if (leadPause) {
      const sec = firstT0 - b.src0;
      if (!this.para && sec < PARA_PAUSE) this.startPara(speaker(b.m, words[b.i0] ? words[b.i0][3] : 0).color, b.s);
      this.pause(b, k, b.i0 - 1, sec);
    }
    for (let i = b.i0; i < b.i1; i++) this.word(b, k, i);
    if (b.i1 <= b.i0 && !leadPause) {
      if (!this.para) this.startPara(GREY, b.s);
      this.token({ kind: "clip", b: k, text: `${dur((b.e - b.s) / this.fps)} без мови` });
    }
    for (const c of b.cut.filter((c) => c.side === "after")) this.cutRun(k, c, b.m);
  }
}

// src: {view, tr, fragOf, topics, showCut, expanded (Set of chip ids shown in full)}
export function buildDocument(src) {
  const d = new Builder(src);
  src.view.blocks.forEach((b, k) => d.block(b, k));
  return { nodes: d.nodes, tokens: d.tokens, wordTok: d.wordTok, blockTok: d.blockTok,
    transcribed: src.view.blocks.some((b) => b.m) };
}

// ---------------------------------------------------------------- what a selection means

// a token as a position the server understands
export function toPos(t) {
  if (t.kind === "clip") return { b: t.b, w: null };
  if (t.kind === "p") return { b: t.b, w: t.w, p: true };
  return { b: t.b, w: t.w };
}

// identity of a token across re-renders (indexes change, this does not while the words stay)
export const tokenKey = (t) => [t.kind, t.b ?? t.k, t.w ?? t.i ?? t.i0].join(":");

// Selected tokens [a, z] -> {kind: "live", sel, n, live} (on the timeline) or {kind: "cut", k, i0, i1, n}
export function selection(tokens, a, z) {
  const toks = tokens.slice(a, z + 1);
  if (!toks.length) return null;
  const live = toks.filter((t) => LIVE.has(t.kind));
  if (live.length) {
    return { kind: "live", sel: { a: toPos(live[0]), z: toPos(live[live.length - 1]) },
      n: live.filter((t) => t.kind === "w").length, live };
  }
  const k = toks[0].k;
  let i0 = Infinity, i1 = -Infinity;
  for (const t of toks.filter((t) => t.k === k)) {
    i0 = Math.min(i0, t.kind === "chip" ? t.i0 : t.i);
    i1 = Math.max(i1, t.kind === "chip" ? t.i1 : t.i + 1);
  }
  return { kind: "cut", k, i0, i1, n: i1 - i0 };
}

// timeline frame where a live token starts (or ends)
export function tokenFrame(view, tr, t, start) {
  const b = view.blocks[t.b];
  if (t.kind === "clip") return start ? b.s : b.e;
  const words = tr[b.m].words;
  if (t.kind === "p") {
    if (start) return t.w < b.i0 ? b.s : wordFrame(b, words[t.w][1]);
    return t.w >= b.i1 - 1 ? b.e : wordFrame(b, words[t.w + 1][0]);
  }
  return wordFrame(b, start ? words[t.w][0] : words[t.w][1]);
}

// words of a live selection grouped by file: [{m, i0, i1}]
export function wordRanges(view, sel) {
  const out = [];
  for (const t of sel.live.filter((t) => t.kind === "w")) {
    const m = view.blocks[t.b].m, last = out[out.length - 1];
    if (last && last.m === m && last.i1 === t.w) last.i1 = t.w + 1;
    else out.push({ m, i0: t.w, i1: t.w + 1 });
  }
  return out;
}

export const endsSentence = (t) => t.kind === "w" && SENTENCE_END.test(t.text);
