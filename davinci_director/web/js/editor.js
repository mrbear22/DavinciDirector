// The text editor: shows the document (see document.js) and lets the editor select, drag and correct it.
// Edits themselves go through Actions; the editor owns only what is on screen: selection, dragging,
// the playhead mark, search hits.

import { $, $$, html, dur, nWords } from "./util.js";
import { state, emit, wordFrame } from "./store.js";
import { buildDocument, selection, tokenFrame, tokenKey, toPos, wordRanges, endsSentence, LIVE } from "./document.js";
import { showMenu } from "./ui.js";

const DRAG_START_PX = 5;
const SILENCE_FULL_SEC = 60;
const AUTOSCROLL_EDGE = 30, AUTOSCROLL_STEP = 16;
const CLASS = { w: "w", p: "p", cut: "c", chip: "chip", clip: "clip" };

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function emptyState() {
  const blocks = state.view.blocks || [];
  if (!state.link.connected && !blocks.length) {
    return html`<div class="empty"><h2>Відкрийте панель з DaVinci Resolve</h2>
      <ol><li>Відкрийте проєкт і таймлайн із записом у DaVinci Resolve.</li>
      <li>Виберіть <code>Workspace &gt; Scripts &gt; DaVinci Director</code>.</li></ol>
      <p>Текст і таймлайн стануть одним монтажем: правки в тексті змінюють кліпи в Resolve, а зміни в Resolve — текст.</p></div>`;
  }
  if (!blocks.length) {
    return html`<div class="empty"><h2>На доріжці ${state.view.track || ""} немає кліпів</h2><p>Покладіть запис на таймлайн у Resolve або оберіть іншу доріжку вгорі.</p></div>`;
  }
  return null;
}

export class Editor {
  constructor(root) {
    this.root = root;
    this.actions = null;  // set by main.js (Actions needs the editor)
    this.doc = { nodes: [], tokens: [], wordTok: new Map(), blockTok: [] };
    this.els = [];
    this.sel = null;      // {a, z}: token indexes (a = where it started)
    this.selKeys = null;  // the same, as token keys, to find it again after a re-render
    this.painted = [];
    this.selecting = null;
    this.drag = null;
    this.hits = [];
    this.hitIdx = -1;
    this.phEl = null;
    this.expanded = new Set();
    this.tools = el("div", "seltools hidden");
    this.tools.id = "seltools";
    this.caret = el("div", "drop-caret hidden");
    this.caret.append(el("span", "drop-tag"));
    this.listen();
  }

  // ------------------------------------------------------------ rendering

  render() {
    const scroll = this.root.scrollTop;
    const empty = emptyState();
    if (empty) {
      this.doc = { nodes: [], tokens: [], wordTok: new Map(), blockTok: [] };
      this.els = [];
      this.root.innerHTML = String(empty);
      return;
    }
    this.doc = buildDocument({ view: state.view, tr: state.tr, fragOf: state.fragOf, topics: state.topics,
      showCut: state.showCut, expanded: this.expanded });
    this.els = new Array(this.doc.tokens.length);
    const page = el("div", "doc");
    if (!this.doc.transcribed) {
      page.insertAdjacentHTML("beforeend", `<div class="empty" style="margin:2vh 0 4vh"><h2>Таймлайн ще не транскрибовано</h2><p>Натисніть «Транскрибувати» вгорі.</p></div>`);
    }
    const render = { topic: (n) => this.topicEl(n), para: (n) => this.paraEl(n), silence: (n) => this.silenceEl(n) };
    for (const node of this.doc.nodes) page.append(render[node.type](node));
    this.root.replaceChildren(page, this.tools, this.caret);
    this.root.scrollTop = scroll;
    this.phEl = null;
    this.restoreSelection();
  }

  topicEl(node) {
    const h = el("h3", "topic");
    h.append(el("span", "topic-t", node.title));
    if (node.summary) h.append(el("span", "topic-s", node.summary));
    return h;
  }

  paraEl(node) {
    const p = el("p", "para");
    if (node.frame != null) {
      const at = el("span", "ptc", dur((node.frame - state.view.start) / state.view.fps));
      at.dataset.goto = node.frame;
      p.append(at);
    }
    for (const run of node.runs.filter((r) => r.items.length)) {
      const span = el("span", "turn");
      span.style.setProperty("--spk", run.color);
      for (const item of run.items) span.append(...(typeof item === "number" ? [this.tokenEl(item), " "] : [this.spliceEl(item)]));
      p.append(span);
    }
    return p;
  }

  // a silent moment: its own row, the bar as long as the silence (up to a minute fills the width)
  silenceEl(node) {
    const row = el("div", "silence");
    const at = el("span", "ptc", dur((node.frame - state.view.start) / state.view.fps));
    at.dataset.goto = node.frame;
    const bar = el("span", "silence-bar");
    bar.style.setProperty("--len", Math.min(1, node.sec / SILENCE_FULL_SEC));
    row.append(at, this.tokenEl(node.token), bar);
    return row;
  }

  tokenEl(i) {
    const t = this.doc.tokens[i];
    const e = el("span", "t " + CLASS[t.kind] + (t.low ? " low" : "") + (t.silence ? " long" : ""), t.text);
    e.dataset.t = i;
    if (t.title) e.title = t.title;
    this.els[i] = e;
    return e;
  }

  spliceEl(item) {
    const s = el("span", "splice");
    s.title = item.splice;
    return s;
  }

  // ------------------------------------------------------------ selection

  range() {
    const n = this.doc.tokens.length - 1;
    if (!this.sel || n < 0) return null;
    const a = Math.min(this.sel.a, this.sel.z), z = Math.max(this.sel.a, this.sel.z);
    return [Math.min(n, a), Math.min(n, z)];
  }

  payload() {
    const r = this.range();
    return r ? selection(this.doc.tokens, r[0], r[1]) : null;
  }

  frames() {
    const p = this.payload();
    if (!p || p.kind !== "live") return null;
    return [tokenFrame(state.view, state.tr, p.live[0], true), tokenFrame(state.view, state.tr, p.live[p.live.length - 1], false)];
  }

  // where pasting goes: before the first live token of the selection
  caretPos() {
    const r = this.range();
    const t = r && this.doc.tokens.slice(r[0]).find((x) => LIVE.has(x.kind));
    return t ? toPos(t) : null;
  }

  wordRanges() {
    const p = this.payload();
    return p && p.kind === "live" ? wordRanges(state.view, p) : [];
  }

  select(a, z = a) {
    this.sel = { a, z };
    this.paint();
  }

  extendTo(i) {
    this.sel.z = i;
    this.paint();
  }

  clear() {
    this.sel = null;
    this.paint();
  }

  paint() {
    for (const i of this.painted) this.els[i] && this.els[i].classList.remove("sel");
    this.painted = [];
    const r = this.range();
    if (r) for (let i = r[0]; i <= r[1]; i++) { this.els[i].classList.add("sel"); this.painted.push(i); }
    this.selKeys = r ? [tokenKey(this.doc.tokens[r[0]]), tokenKey(this.doc.tokens[r[1]])] : null;
    emit("selection", this.payload());
    this.showTools();
  }

  // a selection survives a re-render only if it still points at the same words
  restoreSelection() {
    this.painted = [];
    if (this.selKeys) {
      const find = (key) => this.doc.tokens.findIndex((t) => tokenKey(t) === key);
      const a = find(this.selKeys[0]), z = find(this.selKeys[1]);
      this.sel = a >= 0 && z >= 0 ? { a, z } : null;
    } else {
      this.sel = null;
    }
    this.paint();
  }

  selectBlock(k) {
    const [a, z] = this.doc.blockTok[k] || [];
    if (a == null || z <= a) return;
    this.els[a].scrollIntoView({ block: "center" });
    this.select(a, z - 1);
  }

  selectAll() {
    if (this.doc.tokens.length) this.select(0, this.doc.tokens.length - 1);
  }

  // arrows: move the selection by a token, or extend it with Shift
  step(d, extend) {
    const n = this.doc.tokens.length;
    if (!this.sel || !n) return;
    if (extend) this.extendTo(Math.min(n - 1, Math.max(0, this.sel.z + d)));
    else {
      const edge = d > 0 ? Math.max(this.sel.a, this.sel.z) : Math.min(this.sel.a, this.sel.z);
      this.select(Math.min(n - 1, Math.max(0, edge + d)));
    }
    this.els[this.sel.z].scrollIntoView({ block: "nearest" });
  }

  // ------------------------------------------------------------ floating actions next to the selection

  showTools() {
    const p = this.payload();
    if (!p || this.selecting || this.drag) { this.hideTools(); return; }
    this.tools.innerHTML = String(p.kind === "cut"
      ? html`<span class="st-info">${nWords(p.n)} вирізано</span><button data-act="del">Повернути у відео <kbd>Del</kbd></button>`
      : this.liveTools(p));
    this.tools.classList.remove("hidden", "wrap");
    this.placeTools();
  }

  liveTools(p) {
    const f = this.frames();
    const hasWords = p.live.some((t) => t.kind === "w");
    return html`<span class="st-info">${nWords(p.n)} · ${dur((f[1] - f[0]) / state.view.fps)}</span>
      <button data-act="play">Програти <kbd>Space</kbd></button>
      <button data-act="del" title="Прибрати з відео (слова лишаться закресленими, якщо ввімкнено «вирізане»)">Вирізати <kbd>Del</kbd></button>
      ${hasWords ? html`<button data-act="fix" title="Виправити текст виділених слів (на відео не впливає)">Виправити <kbd>F2</kbd></button>` : ""}
      ${this.speakerButtons()}`;
  }

  // "these words were said by …": one coloured button per speaker (keys 1–9) and «+» for a new one
  speakerButtons() {
    const r = this.wordRanges();
    if (!r.length) return "";
    return html`<span class="st-spk" title="Хто сказав виділені слова: клік по мовцю (або клавіша 1–9)">${state.tr[r[0].m].speakers.map((s, i) =>
      html`<button data-spk="${s.id}" style="--spk:${s.color}" title="Ці слова сказав(ла) ${s.name}${i < 9 ? " — клавіша " + (i + 1) : ""}"><i></i>${s.name}</button>`)}
      <button data-spk="new" title="Новий мовець">+</button></span>`;
  }

  // above the selection, or under it when there is no room; one row unless it really does not fit
  placeTools() {
    const r = this.range(), box = this.tools, ed = this.root;
    const er = ed.getBoundingClientRect(), first = this.els[r[0]].getBoundingClientRect(), last = this.els[r[1]].getBoundingClientRect();
    box.style.left = "0px";
    if (box.offsetWidth > ed.clientWidth - 24) box.classList.add("wrap");
    const left = Math.min(Math.max(8, first.left - er.left), ed.clientWidth - box.offsetWidth - 12);
    const above = first.top - er.top >= box.offsetHeight + 12;
    box.style.left = left + "px";
    box.style.top = (above ? first.top - er.top - box.offsetHeight - 8 : last.bottom - er.top + 8) + ed.scrollTop + "px";
  }

  hideTools() { this.tools.classList.add("hidden"); }

  // ------------------------------------------------------------ playhead and search marks

  // the word under Resolve's playhead; scrolls to it when following
  markPlayhead(frame, scroll) {
    const blocks = state.view.blocks || [];
    const k = frame == null ? -1 : blocks.findIndex((b) => b.s <= frame && frame < b.e);
    const e = k < 0 ? null : this.wordElAt(blocks[k], k, frame);
    if (e && e === this.phEl && e.isConnected) return;
    if (this.phEl) this.phEl.classList.remove("ph");
    this.phEl = e;
    if (!e) return;
    e.classList.add("ph");
    if (scroll && state.follow && !state.busy && !this.selecting && document.activeElement !== $("#searchInput")) {
      const r = e.getBoundingClientRect(), er = this.root.getBoundingClientRect();
      if (r.top < er.top + 40 || r.bottom > er.bottom - 60) e.scrollIntoView({ block: "center" });
    }
  }

  wordElAt(b, k, frame) {
    if (!b.m || !state.tr[b.m]) return null;
    const words = state.tr[b.m].words;
    let lo = b.i0, hi = b.i1 - 1, best = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (wordFrame(b, words[mid][0]) <= frame) { best = mid; lo = mid + 1; } else hi = mid - 1;
    }
    const i = this.doc.wordTok.get(k + ":" + best);
    return i == null ? null : this.els[i];
  }

  // words (or the beginning of the last one) in a row; the matches are selected one by one
  search(query, inCut, jump) {
    $$(".t.hit", this.root).forEach((e) => e.classList.remove("hit", "cur"));
    this.hits = [];
    const terms = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
    if (!terms.length) return 0;
    const idx = this.doc.tokens.map((t, i) => (t.kind === "w" || (inCut && t.kind === "cut") ? i : -1)).filter((i) => i >= 0);
    const clean = (i) => this.doc.tokens[i].text.toLowerCase().replace(/[.,!?…:;«»"()]/g, "");
    const matches = (j, m) => {
      const t = clean(idx[j + m]);
      return m === terms.length - 1 ? t.includes(terms[m]) : t === terms[m];
    };
    for (let j = 0; j + terms.length <= idx.length; j++) {
      if (terms.every((_, m) => matches(j, m))) this.hits.push([idx[j], idx[j + terms.length - 1]]);
    }
    for (const [a, z] of this.hits) for (let i = a; i <= z; i++) this.els[i].classList.add("hit");
    this.hitIdx = this.hits.length ? Math.min(Math.max(this.hitIdx, 0), this.hits.length - 1) : -1;
    this.showHit(jump);
    return this.hits.length;
  }

  showHit(jump) {
    $$(".t.hit.cur", this.root).forEach((e) => e.classList.remove("cur"));
    const g = this.hits[this.hitIdx];
    if (!g) return;
    for (let i = g[0]; i <= g[1]; i++) this.els[i].classList.add("cur");
    if (jump) this.els[g[0]].scrollIntoView({ block: "center" });
  }

  stepHit(d) {
    if (!this.hits.length) return;
    this.hitIdx = (this.hitIdx + d + this.hits.length) % this.hits.length;
    this.showHit(true);
    this.select(...this.hits[this.hitIdx]);  // Del / Space act on the match
  }

  // ------------------------------------------------------------ correcting the text in place

  // the selected words of one clip become a text field; Enter saves, Esc cancels
  correctSelection() {
    const p = this.payload();
    const ws = p && p.kind === "live" ? p.live.filter((x) => x.kind === "w") : [];
    if (!ws.length) return;
    const k = ws[0].b, same = ws.filter((x) => x.b === k);
    this.correctWords(k, same[0].w, same[same.length - 1].w + 1);
  }

  correctWords(k, i0, i1) {
    const key = state.view.blocks[k].m;
    const first = this.doc.wordTok.get(k + ":" + i0), last = this.doc.wordTok.get(k + ":" + (i1 - 1));
    if (!key || first == null || last == null) return;
    const orig = state.tr[key].words.slice(i0, i1).map((w) => w[2]).join(" ");
    const els = this.els.slice(first, last + 1);
    const input = el("textarea", "word-edit");
    input.value = orig;
    input.rows = 1;
    input.spellcheck = false;
    input.style.width = Math.min(this.root.clientWidth - 40, Math.max(80, els.reduce((s, e) => s + e.offsetWidth + 4, 40))) + "px";
    this.hideTools();
    els[0].replaceWith(input);
    els.slice(1).forEach((e) => (e.style.display = "none"));
    const fit = () => { input.style.height = "auto"; input.style.height = input.scrollHeight + "px"; };
    fit();
    input.addEventListener("input", fit);
    input.focus();
    input.select();
    let done = false;
    const finish = (save) => {
      if (done) return;
      done = true;
      const text = input.value.replace(/\s+/g, " ").trim();
      if (save && text && text !== orig) this.actions.correct(key, i0, i1, text);
      else this.render();
    };
    input.addEventListener("keydown", (e) => {
      e.stopPropagation();
      if (e.key === "Enter") { e.preventDefault(); finish(true); }
      if (e.key === "Escape") finish(false);
    });
    input.addEventListener("blur", () => finish(true));
  }

  // ------------------------------------------------------------ mouse

  listen() {
    const ed = this.root;
    ed.addEventListener("mousedown", (e) => this.onMouseDown(e));
    document.addEventListener("mousemove", (e) => this.onMouseMove(e));
    document.addEventListener("mouseup", (e) => this.onMouseUp(e));
    ed.addEventListener("dblclick", (e) => this.onDoubleClick(e));
    ed.addEventListener("click", (e) => this.onToolsClick(e));
    ed.addEventListener("contextmenu", (e) => this.onContextMenu(e));
    ed.addEventListener("scroll", () => { if (!this.selecting) this.showTools(); });
  }

  tokenAt(x, y) {
    const e = document.elementFromPoint(x, y);
    const t = e && e.closest && e.closest("#editor .t");
    return t ? +t.dataset.t : null;
  }

  paraTokens(e) {
    return $$(".t", e.closest(".para, .silence")).map((x) => +x.dataset.t);
  }

  onMouseDown(ev) {
    if (ev.button !== 0 || ev.target.closest("#seltools")) return;
    const at = ev.target.closest("[data-goto]");
    if (at) { this.actions.goto({ frame: +at.dataset.goto }); return; }
    const t = ev.target.closest(".t");
    if (!t) { if (!ev.target.closest(".para")) this.clear(); return; }
    ev.preventDefault();
    this.root.focus({ preventScroll: true });
    const i = +t.dataset.t;
    if (ev.detail === 3) {  // triple click: the paragraph
      const toks = this.paraTokens(t);
      this.select(toks[0], toks[toks.length - 1]);
      return;
    }
    if (ev.shiftKey && this.sel) { this.extendTo(i); return; }
    const r = this.range(), p = this.payload();
    // pressing on what is already selected and moving = dragging it somewhere else
    if (r && i >= r[0] && i <= r[1] && p.kind === "live") {
      this.drag = { sel: p, x: ev.clientX, y: ev.clientY, active: false, drop: null, copy: false };
      return;
    }
    this.selecting = { i, moved: false };
    this.select(i);
  }

  onMouseMove(ev) {
    if (this.selecting) {
      const i = this.tokenAt(ev.clientX, ev.clientY);
      if (i != null && i !== this.sel.z) { this.selecting.moved = true; this.extendTo(i); }
      this.autoscroll(ev);
      return;
    }
    const d = this.drag;
    if (!d) return;
    if (!d.active && Math.hypot(ev.clientX - d.x, ev.clientY - d.y) > DRAG_START_PX) this.startDrag(d);
    if (!d.active) return;
    d.copy = ev.ctrlKey || ev.metaKey;
    d.ghost.classList.toggle("copy", d.copy);
    d.ghost.style.left = ev.clientX + 14 + "px";
    d.ghost.style.top = ev.clientY + 14 + "px";
    d.drop = this.dropAt(ev.clientX, ev.clientY);
    this.showCaret(d.drop);
    this.autoscroll(ev);
  }

  onMouseUp(ev) {
    if (this.selecting) {
      const { i, moved } = this.selecting;
      this.selecting = null;
      const t = this.doc.tokens[i];
      if (!moved && t.kind === "chip") { this.expanded.add(t.id); this.render(); return; }
      if (!moved && LIVE.has(t.kind)) this.actions.goto(t.kind === "w" ? { b: t.b, w: t.w } : { frame: tokenFrame(state.view, state.tr, t, true) });
      this.paint();
      return;
    }
    const d = this.drag;
    if (!d) return;
    this.endDrag();
    if (d.active && d.drop && d.drop.pos) {
      if (ev.ctrlKey || ev.metaKey) this.actions.copyTo(d.sel.sel, d.drop.pos);
      else this.actions.move(d.sel.sel, d.drop.pos);
    } else {
      this.showTools();
    }
  }

  // double click: the sentence inside the paragraph
  onDoubleClick(ev) {
    const t = ev.target.closest(".t.w");
    if (!t) return;
    const toks = this.paraTokens(t), tokens = this.doc.tokens;
    let a = toks.indexOf(+t.dataset.t), z = a;
    while (a > 0 && !endsSentence(tokens[toks[a - 1]])) a--;
    while (z < toks.length - 1 && !endsSentence(tokens[toks[z]])) z++;
    this.select(toks[a], toks[z]);
  }

  onToolsClick(ev) {
    const b = ev.target.closest("#seltools button");
    if (!b) return;
    if (b.dataset.spk) this.actions.setSpeaker(b.dataset.spk);
    else if (b.dataset.act === "play") this.actions.play();
    else if (b.dataset.act === "del") this.actions.cutOrRestore();
    else if (b.dataset.act === "fix") this.correctSelection();
  }

  onContextMenu(ev) {
    ev.preventDefault();
    const t = ev.target.closest(".t");
    if (t) {
      const i = +t.dataset.t, r = this.range();
      if (!r || i < r[0] || i > r[1]) this.select(i);
    }
    showMenu(ev.clientX, ev.clientY, this.actions.menu(this.payload()));
  }

  autoscroll(ev) {
    const r = this.root.getBoundingClientRect();
    if (ev.clientY < r.top + AUTOSCROLL_EDGE) this.root.scrollTop -= AUTOSCROLL_STEP;
    else if (ev.clientY > r.bottom - AUTOSCROLL_EDGE) this.root.scrollTop += AUTOSCROLL_STEP;
  }

  // ------------------------------------------------------------ dragging a piece somewhere else

  startDrag(d) {
    d.active = true;
    const words = d.sel.live.filter((t) => t.kind === "w").map((t) => t.text);
    d.ghost = el("div", "drag-ghost", words.slice(0, 6).join(" ") + (words.length > 6 ? " …" : ""));
    document.body.append(d.ghost);
    this.root.classList.add("dragging");
    this.hideTools();
  }

  endDrag() {
    if (!this.drag) return;
    if (this.drag.ghost) this.drag.ghost.remove();
    this.drag = null;
    this.root.classList.remove("dragging");
    this.showCaret(null);
  }

  // Where the piece lands: over the left half of a word — before it, over the right half — after it
  // (= before the next live token).  Returns {pos, x, y, h} for the caret, or {inside} over the piece itself.
  dropAt(x, y) {
    const i = this.tokenAt(x, y);
    if (i == null) return null;
    const tokens = this.doc.tokens, r = this.range();
    const rect = this.els[i].getBoundingClientRect();
    const after = x > rect.left + rect.width / 2;
    let j = after ? i + 1 : i;
    while (j < tokens.length && !LIVE.has(tokens[j].kind)) j++;
    if (r && j > r[0] && j <= r[1] + 1) return { inside: true };
    if (j >= tokens.length) {  // after the very last word: the end of the track
      const end = this.els[this.els.length - 1].getBoundingClientRect();
      return { pos: { b: null, end: true }, x: end.right + 3, y: end.top, h: end.height };
    }
    // the caret stands at the left edge of token j, unless j wrapped to the next line: then after token i
    const rj = this.els[j].getBoundingClientRect();
    const atI = after && Math.abs(rj.top - rect.top) > rect.height / 2;
    return { pos: toPos(tokens[j]), x: atI ? rect.right + 2 : rj.left - 2, y: atI ? rect.top : rj.top, h: atI ? rect.height : rj.height };
  }

  showCaret(d) {
    if (!d || !d.pos) { this.caret.classList.add("hidden"); return; }
    const er = this.root.getBoundingClientRect();
    this.caret.style.left = d.x - er.left + this.root.scrollLeft + "px";
    this.caret.style.top = d.y - er.top + this.root.scrollTop + "px";
    this.caret.style.height = d.h + "px";
    $(".drop-tag", this.caret).textContent = this.drag && this.drag.copy ? "копія сюди" : "сюди";
    this.caret.classList.remove("hidden");
  }
}
