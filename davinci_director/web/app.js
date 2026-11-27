/* DaVinci Director UI.  The text is always rendered from the current Resolve
   timeline (server "view"); every edit is sent to the server, executed on the
   real timeline, and the text is re-rendered from the new timeline state. */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const S = {
  view: { blocks: [], fps: 25, start: 0, v: 0 },
  tr: {},            // media key -> transcript payload
  fragOf: {},        // media key -> Int32Array word -> fragment index
  tv: -1,
  stories: [],
  link: {},
  undo: {},
  playhead: null,
  follow: true,
  activeStory: null,
  busy: false,
  hits: [], hitIdx: -1,
  place: null,       // {op: "move"|"paste", sel, n} while the user picks where to put a piece
  clipN: 0,          // words in the copied piece
  expandedCuts: new Set(),
  tok: [], tokEls: [], wordTok: new Map(), blockTok: [],
  sel: null, selKey: null, painted: [], selecting: null, drag: null,
  showCut: (() => { try { return localStorage.getItem("dd.showCut") !== "0"; } catch (e) { return true; } })(),
  panels: (() => {
    const d = { outline: true, side: "stories" };
    try { return Object.assign(d, JSON.parse(localStorage.getItem("dd.panels") || "{}")); } catch (e) { return d; }
  })(),
};

// ------------------------------------------------------------------ api

async function api(path, body) {
  const opt = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const r = await fetch("/api/" + path, opt);
  const data = await r.json().catch(() => ({}));
  if (!r.ok || data.error) throw new Error(data.error || ("HTTP " + r.status));
  return data;
}

async function act(label, fn) {
  if (S.busy) return;
  S.busy = true;
  $("#statusEdit").textContent = label + "…";
  document.body.style.cursor = "progress";
  try {
    const res = await fn();
    $("#statusEdit").textContent = label + ": готово";
    return res;
  } catch (e) {
    $("#statusEdit").textContent = "";
    toast(e.message, "err");
  } finally {
    S.busy = false;
    document.body.style.cursor = "";
  }
}

function toast(msg, kind = "") {
  const t = document.createElement("div");
  t.className = "toast " + kind;
  t.textContent = msg;
  $("#toasts").appendChild(t);
  // errors stay until clicked when they are long (instructions to follow)
  t.onclick = () => t.remove();
  if (!(kind === "err" && msg.length > 120)) setTimeout(() => t.remove(), kind === "err" ? 9000 : 3500);
}

// ------------------------------------------------------------------ time

function tc(frame, fps) {
  const n = Math.round(fps) || 25;
  frame = Math.max(0, Math.round(frame));
  const f = frame % n, s = Math.floor(frame / n);
  const p = (x) => String(x).padStart(2, "0");
  return `${p(Math.floor(s / 3600))}:${p(Math.floor(s / 60) % 60)}:${p(s % 60)}:${p(f)}`;
}
function dur(sec) {
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec / 3600), m = Math.floor(sec / 60) % 60, s = sec % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}
function wordFrame(b, t) {
  return Math.min(Math.max(b.s + Math.round((t - b.src0) * S.view.fps), b.s), b.e);
}

// ------------------------------------------------------------------ state

async function loadState() {
  const st = await api("state");
  S.view = st.view; S.link = st.link; S.undo = st.undo; S.stories = st.stories || []; S.topics = st.topics || [];
  setTranscripts(st.transcripts, st.view.tv);
  S.playhead = st.link.playhead;
  renderLink(); renderEditor(); renderStories(); renderSpeakers(); renderUndo();
  (st.jobs || []).forEach(renderJob);
}

function setTranscripts(tr, tv) {
  S.tr = tr || {};
  S.tv = tv;
  S.fragOf = {};
  for (const [k, t] of Object.entries(S.tr)) {
    const arr = new Int32Array(t.words.length);
    t.frags.forEach(([a, z], i) => { for (let w = a; w < z; w++) arr[w] = i; });
    S.fragOf[k] = arr;
  }
}

function connectEvents() {
  const es = new EventSource("/api/events");
  es.onmessage = async (ev) => {
    const m = JSON.parse(ev.data);
    if (m.type === "view") {
      if (m.view.tv !== S.tv) {
        const t = await api("transcripts");
        setTranscripts(t.transcripts, t.tv);
        renderSpeakers();
      }
      const same = m.view.v === S.view.v;
      S.view = m.view;
      if (m.undo) { S.undo = m.undo; renderUndo(); }
      if (!same) { renderEditor(); renderStories(); }
    } else if (m.type === "playhead") {
      onPlayheadReport(m.frame);
    } else if (m.type === "link") {
      S.link = m.link; renderLink();
    } else if (m.type === "job") {
      renderJob(m.job);
    }
  };
  es.onerror = () => { $("#connDot").className = "dot"; $("#connText").textContent = "Немає зв'язку із застосунком"; };
  es.onopen = () => loadState().catch((e) => toast(e.message, "err"));
}

// ------------------------------------------------------------------ top bar

function renderLink() {
  const L = S.link || {};
  const info = L.info || {};
  const dot = $("#connDot");
  if (L.connected) {
    dot.className = "dot " + ((L.check || []).length ? "warn" : "on");
    $("#connText").textContent = [info.project, info.tl].filter(Boolean).join(" / ") || "Resolve підключено";
    $("#conn").title = `${info.product || "DaVinci Resolve"} ${info.version || ""}` + ((L.check || []).length ? "\nПеревірка стану: " + L.check.join("; ") : "");
  } else {
    dot.className = "dot";
    $("#connText").textContent = "Resolve не підключено";
    $("#conn").title = "Запустіть у Resolve: Workspace > Scripts > DaVinci Director";
  }
  const sel = $("#trackSel");
  const tracks = L.tracks && L.tracks.length ? L.tracks : [L.track || "video1"];
  const label = (k) => (k.startsWith("video") ? "V" : "A") + k.replace(/\D/g, "");
  sel.innerHTML = tracks.map((k) => `<option value="${k}" ${k === L.track ? "selected" : ""}>${label(k)}</option>`).join("");
  renderTrackInfo();
}

// the track header under the big timecode: «Стрім 14.11» / «12 кліпів · 1:42:10»
function renderTrackInfo() {
  const blocks = (S.view && S.view.blocks) || [];
  $("#tlName").textContent = (S.view && S.view.tl) || "Таймлайн";
  $("#trkSub").textContent = blocks.length
    ? `${blocks.length} ${plural(blocks.length, "кліп", "кліпи", "кліпів")} · ${dur((blocks[blocks.length - 1].e - blocks[0].s) / S.view.fps)}`
    : "немає кліпів";
}

// panel switches on the top bar (like Media Pool / Effects in Resolve); remembered between sessions
function applyPanels() {
  const P = S.panels;
  $("#pnlOutline").classList.toggle("hidden", !P.outline);
  $("#tglOutline").classList.toggle("on", P.outline);
  $("#pnlSide").classList.toggle("hidden", !P.side);
  $$("[data-side]").forEach((b) => b.classList.toggle("on", P.side === b.dataset.side));
  if (P.side) showTab(P.side, false);
  $("#tglText").classList.add("on");
  try { localStorage.setItem("dd.panels", JSON.stringify(P)); } catch (e) { /* optional */ }
}

function renderUndo() {
  $("#btnUndo").disabled = !S.undo.undo;
  $("#btnRedo").disabled = !S.undo.redo;
  $("#btnUndo").title = S.undo.undo ? `Скасувати: ${S.undo.undo} (Ctrl+Z)` : "Скасувати (Ctrl+Z)";
  $("#btnRedo").title = S.undo.redo ? `Повторити: ${S.undo.redo} (Ctrl+Shift+Z)` : "Повторити (Ctrl+Shift+Z)";
}

function renderJob(job) {
  const el = $("#statusJob");
  if (job.status === "running") {
    el.innerHTML = `${esc(job.label)}: ${esc(job.message || "")} <span class="progress"><i style="width:${(job.progress * 100).toFixed(1)}%"></i></span> ${(job.progress * 100).toFixed(0)}% <button class="mini" data-cancel="${job.id}" title="Скасувати">&times;</button>`;
    if (job.kind === "transcribe") $("#btnTranscribe").disabled = true;
  } else {
    el.textContent = "";
    $("#btnTranscribe").disabled = false;
    if (job.status === "done") toast(job.label + ": готово", "ok");
    else if (job.status === "error") toast(job.label + ": " + job.message, "err");
    (job.log || []).forEach((m) => toast(m));
  }
}

// ------------------------------------------------------------------ editor rendering
//
// The transcript reads like a document: paragraphs end at long pauses (or when they get long),
// not at every change of speaker — each word carries its speaker, so a speaker's words are simply
// coloured (legend above).  Topic headings (from the language model) start new sections.  Splice marks
// where the timeline has a cut, cut words struck through in place, pauses as small tokens.  Every
// word / pause / cut word is a "token"; selection works on whole tokens (click, drag, shift+click, keys).

const PAUSE_MIN = 3.0, PARA_PAUSE = 6.0, PARA_WORDS = 90, CUT_COLLAPSE = 24;

function plural(n, one, few, many) {
  const m10 = n % 10, m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return one;
  if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
  return many;
}
const nWords = (n) => `${n} ${plural(n, "слово", "слова", "слів")}`;

function spk(key, idx) {
  const t = S.tr[key];
  return (t && (t.speakers[idx] || t.speakers[0])) || { id: "S1", name: "Мовець", color: "#cfcfcf" };
}

function renderLegend() {
  const el = $("#legend");
  const keys = Object.keys(S.tr).filter((k) => S.view.blocks.some((b) => b.m === k));
  if (!keys.length) { el.innerHTML = ""; return; }
  el.innerHTML = `<div class="lg-list">` + keys.map((k) => S.tr[k].speakers.map((s) =>
    `<button class="lg" data-m="${k}" data-sid="${s.id}" title="${esc(s.name)} — клік: перейменувати"><i style="background:${s.color}"></i>${esc(s.name)}</button>`).join("")).join("") + `</div>` +
    `<label class="chk lg-cut" title="Показувати вирізані слова на їхніх місцях"><input type="checkbox" id="showCut" ${S.showCut ? "checked" : ""}> вирізане</label>` +
    `<button class="lg lg-help" id="btnHelp" title="Як монтувати текстом (?)">?</button>`;
}

function helpDialog() {
  modal("Як монтувати текстом", `<div class="help">
    <p class="hint">Текст і таймлайн Resolve — один монтаж: що робите з текстом, те стається з кліпами.</p>
    <h4>Виділити</h4>
    <ul><li><b>Клік</b> по слову — це слово (і плейхед Resolve стає на нього).</li>
      <li><b>Перетягніть</b> мишею по словах — шматок. <b>Shift+клік</b> — від виділеного до цього слова.</li>
      <li><b>2×клік</b> — речення, <b>3×клік</b> — абзац. <b>Shift+←/→</b> — розширити по слову.</li></ul>
    <h4>Прибрати й повернути</h4>
    <ul><li><b>Del</b> — вирізати виділене з відео.</li>
      <li>Увімкніть <b>«вирізане»</b> над текстом: вирізані слова видно закресленими; виділіть їх і натисніть <b>Del</b>, щоб повернути.</li></ul>
    <h4>Перенести</h4>
    <ul><li>Виділіть слова, потім <b>затисніть на виділеному й тягніть</b>. Блимаючий курсор «сюди» показує, куди стане шматок:
      над лівою половиною слова — перед ним, над правою — після нього. Відпустіть — і шматок там.</li>
      <li>З затиснутим <b>Ctrl</b> — не перенести, а <b>копія</b>. <b>Esc</b> під час перетягування — скасувати.</li>
      <li>Також є <b>Ctrl+C / Ctrl+V</b> (вставляє перед виділеним словом) і <b>Ctrl+D</b> — дублювати поруч.</li></ul>
    <h4>Мовці</h4>
    <ul><li>Колір слова — хто його сказав. Якщо мовця визначено неправильно: виділіть ці слова й натисніть ім'я потрібного мовця на панелі виділення (або клавішу <b>1–9</b> — номер мовця в легенді над текстом); <b>«+»</b> — новий мовець.</li>
      <li>Клік по імені мовця над текстом — перейменувати.</li></ul>
    <h4>Інше</h4>
    <ul><li><b>Space</b> — програти виділене в Resolve. <b>Ctrl+Z / Ctrl+Y</b> — скасувати / повторити.</li>
      <li><b>F2</b> — виправити слово. ПКМ — меню з усіма діями й мовцем репліки. <b>Esc</b> — скасувати вибір.</li></ul></div>`,
    [["Зрозуміло", "primary"]]);
}

function renderEditor() {
  const ed = $("#editor");
  const blocks = S.view.blocks || [];
  const scroll = ed.scrollTop;
  S.tok = []; S.wordTok = new Map(); S.blockTok = [];
  renderLegend(); renderOutline(); renderOverview(); renderTrackInfo();
  if (!S.link.connected && !blocks.length) {
    ed.innerHTML = `<div class="empty"><h2>Відкрийте панель з DaVinci Resolve</h2>
      <ol><li>Відкрийте проєкт і таймлайн із записом у DaVinci Resolve.</li>
      <li>Виберіть <code>Workspace &gt; Scripts &gt; DaVinci Director</code>.</li></ol>
      <p>Текст і таймлайн стануть одним монтажем: правки в тексті змінюють кліпи в Resolve, а зміни в Resolve — текст.</p></div>`;
    hideTools();
    return;
  }
  if (!blocks.length) {
    ed.innerHTML = `<div class="empty"><h2>На доріжці ${esc(S.view.track || "")} немає кліпів</h2><p>Покладіть запис на таймлайн у Resolve або оберіть іншу доріжку вгорі.</p></div>`;
    hideTools();
    return;
  }
  const fps = S.view.fps;
  let h = `<div class="doc">`;
  if (!blocks.some((b) => b.m)) {
    h += `<div class="empty" style="margin:2vh 0 4vh"><h2>Таймлайн ще не транскрибовано</h2><p>Натисніть «Транскрибувати» вгорі.</p></div>`;
  }
  let open = false, turnOpen = false, curSp = null, paraWords = 0, curTopic = null;
  // topics from the language model: the one a word belongs to = the last topic that starts at or before it
  const topicsBy = {};
  for (const tp of S.topics || []) (topicsBy[tp.m] = topicsBy[tp.m] || []).push(tp);
  for (const k in topicsBy) topicsBy[k].sort((x, y) => x.i - y.i);
  const topicAt = (m, i) => {
    const arr = topicsBy[m];
    if (!arr) return null;
    let lo = 0, hi = arr.length - 1, best = null;
    while (lo <= hi) { const mid = (lo + hi) >> 1; if (arr[mid].i <= i) { best = arr[mid]; lo = mid + 1; } else hi = mid - 1; }
    return best;
  };
  const tok = (o, cls, inner, attrs = "") => {
    const i = S.tok.length;
    S.tok.push(o);
    return `<span class="t ${cls}" data-t="${i}"${attrs}>${inner}</span>`;
  };
  const closeTurn = () => { if (turnOpen) h += `</span>`; turnOpen = false; };
  // a run of one speaker's words inside a paragraph, in that speaker's colour
  const turn = (color) => {
    closeTurn();
    h += `<span class="turn" style="--spk:${color}">`;
    turnOpen = true;
  };
  const para = (color, frame) => {
    closeTurn();
    if (open) h += `</p>`;
    h += `<p class="para"><span class="ptc" data-goto="${frame}">${dur((frame - S.view.start) / fps)}</span>`;
    open = true; paraWords = 0; curSp = null;
    turn(color);
  };
  const cutRun = (k, c, key) => {
    if (!S.showCut) return;
    const t = S.tr[key], id = `${key}:${c.i0}:${c.i1}`, n = c.i1 - c.i0;
    if (n > CUT_COLLAPSE && !S.expandedCuts.has(id)) {
      const secs = t.words[c.i1 - 1][1] - t.words[c.i0][0];
      h += tok({ kind: "chip", k, i0: c.i0, i1: c.i1, id }, "chip", `вирізано ${nWords(n)} · ${dur(secs)}`,
        ` title="Клік — показати. Виділіть і натисніть Del, щоб повернути у відео."`) + " ";
      return;
    }
    for (let i = c.i0; i < c.i1; i++) h += tok({ kind: "cut", k, i }, "c", esc(t.words[i][2])) + " ";
  };
  blocks.forEach((b, k) => {
    const first = S.tok.length;
    if (k > 0) h += `<span class="splice" title="Склейка · ${tc(b.s, fps)}"></span>`;
    if (!b.m || !S.tr[b.m]) {
      para("#9a9a9e", b.s); curSp = null;
      h += tok({ kind: "clip", b: k }, "clip", `${esc(b.name)} · ${dur((b.e - b.s) / fps)} · без транскрипції`);
      S.blockTok[k] = [first, S.tok.length];
      return;
    }
    const t = S.tr[b.m], words = t.words, fo = S.fragOf[b.m];
    for (const c of b.cut.filter((c) => c.side === "before")) cutRun(k, c, b.m);
    const firstT0 = b.i1 > b.i0 ? words[b.i0][0] : b.src1;
    if (firstT0 - b.src0 >= PAUSE_MIN) {
      if (!open) para(spk(b.m, words[b.i0] ? words[b.i0][3] : 0).color, b.s);
      h += tok({ kind: "p", b: k, w: b.i0 - 1, sec: firstT0 - b.src0 }, "p", `${Math.round(firstT0 - b.src0)} с`) + " ";
    }
    for (let i = b.i0; i < b.i1; i++) {
      const w = words[i];
      const gapPrev = i > b.i0 ? w[0] - words[i - 1][1] : 0;
      const tp = topicAt(b.m, i);
      if (tp && tp !== curTopic) {  // a new topic: heading, then a new paragraph
        curTopic = tp;
        closeTurn();
        if (open) { h += `</p>`; open = false; }
        h += `<h3 class="topic"><span class="topic-t">${esc(tp.title)}</span>${tp.summary ? `<span class="topic-s">${esc(tp.summary)}</span>` : ""}</h3>`;
      }
      if (!open || gapPrev >= PARA_PAUSE || (paraWords >= PARA_WORDS && /[.!?…]$/.test(words[i - 1] ? words[i - 1][2] : ""))) {
        para(spk(b.m, w[3]).color, wordFrame(b, w[0]));
      }
      if (b.m + ":" + w[3] !== curSp) {  // speaker of this file (another clip may number speakers differently)
        turn(spk(b.m, w[3]).color);
        curSp = b.m + ":" + w[3];
      }
      const fid = `${t.tag}${fo[i] + 1}`;
      const low = w.length > 4 && w[4] < 0.45 ? " low" : "";
      S.wordTok.set(k + ":" + i, S.tok.length);
      h += tok({ kind: "w", b: k, w: i }, "w" + low, esc(w[2]), ` title="${fid} · ${tc(wordFrame(b, w[0]), fps)}"`) + " ";
      paraWords++;
      const nextT0 = i + 1 < b.i1 ? words[i + 1][0] : b.src1;
      if (nextT0 - w[1] >= PAUSE_MIN) {
        h += tok({ kind: "p", b: k, w: i, sec: nextT0 - w[1] }, "p", `${Math.round(nextT0 - w[1])} с`) + " ";
      }
    }
    if (b.i1 <= b.i0 && !(firstT0 - b.src0 >= PAUSE_MIN)) {
      if (!open) para("#9a9a9e", b.s);
      h += tok({ kind: "clip", b: k }, "clip", `${dur((b.e - b.s) / fps)} без мови`);
    }
    for (const c of b.cut.filter((c) => c.side === "after")) cutRun(k, c, b.m);
    S.blockTok[k] = [first, S.tok.length];
  });
  closeTurn();
  if (open) h += `</p>`;
  h += `</div><div class="seltools hidden" id="seltools"></div>`;
  ed.innerHTML = h;
  ed.scrollTop = scroll;
  S.tokEls = $$(".t", ed);
  // a selection survives a re-render only if it still points at the same words
  if (S.sel && S.selKey) restoreSel(); else { S.sel = null; S.painted = []; }
  paintSel();
  showPlayhead(false);
  if ($("#searchInput").value) runSearch(false);
}

// ------------------------------------------------------------------ structure: outline + overview

function blockFirstWords(b, n = 9) {
  if (!b.m || !S.tr[b.m]) return `<i>${esc(b.name)}</i>`;
  const t = S.tr[b.m];
  let out = "", last = -1;
  for (let i = b.i0; i < Math.min(b.i1, b.i0 + n); i++) {
    const s = t.words[i][3];
    if (s !== last) { if (last >= 0) out += "</span> "; out += `<span style="color:${spk(b.m, s).color}">`; last = s; }
    out += esc(t.words[i][2]) + " ";
  }
  return (last >= 0 ? out + "</span>" : "<i>без мови</i>") + (b.i1 - b.i0 > n ? "…" : "");
}

function renderOutline() {
  const el = $("#outline");
  const blocks = S.view.blocks || [];
  const fps = S.view.fps;
  if (!blocks.length) { el.innerHTML = `<div class="hint ol-empty">Кліпи таймлайна з'являться тут.</div>`; $("#outlineSum").textContent = ""; return; }
  const total = blocks[blocks.length - 1].e - blocks[0].s;
  const maxD = Math.max(...blocks.map((b) => b.e - b.s));
  $("#outlineSum").textContent = `${blocks.length} ${plural(blocks.length, "кліп", "кліпи", "кліпів")} · ${dur(total / fps)}`;
  el.innerHTML = blocks.map((b, k) => `<div class="ol-row" data-k="${k}">
      <div class="ol-top"><span class="ol-n">${k + 1}</span><span class="ol-tc">${dur((b.s - S.view.start) / fps)}</span><span class="ol-d">${dur((b.e - b.s) / fps)}</span></div>
      <div class="ol-bar"><i style="width:${Math.max(2, (b.e - b.s) / maxD * 100)}%"></i></div>
      <div class="ol-txt">${blockFirstWords(b)}</div></div>`).join("");
}

function renderOverview() {
  const el = $("#overview");
  const blocks = S.view.blocks || [];
  if (!blocks.length) { el.innerHTML = ""; return; }
  const t0 = blocks[0].s, span = Math.max(1, blocks[blocks.length - 1].e - t0);
  el.dataset.t0 = t0; el.dataset.span = span;
  el.innerHTML = blocks.map((b, k) => {
    const c = b.m && S.tr[b.m] && b.i1 > b.i0 ? spk(b.m, S.tr[b.m].words[b.i0][3]).color : "#6b6b70";
    return `<i class="ov-clip" data-k="${k}" style="left:${(b.s - t0) / span * 100}%;width:${(b.e - b.s) / span * 100}%;--c:${c}" title="${k + 1} · ${tc(b.s, S.view.fps)}"></i>`;
  }).join("") + `<i class="ov-sel hidden" id="ovSel"></i><i class="ov-ph" id="ovPh"></i>`;
  paintOverviewSel();
}

function paintOverviewSel() {
  const el = $("#ovSel"), ov = $("#overview");
  if (!el) return;
  const r = selFrames();
  if (!r) { el.classList.add("hidden"); return; }
  const t0 = +ov.dataset.t0, span = +ov.dataset.span;
  el.classList.remove("hidden");
  el.style.left = (r[0] - t0) / span * 100 + "%";
  el.style.width = Math.max(0.2, (r[1] - r[0]) / span * 100) + "%";
}

// Resolve reports the playhead only a few times a second; while it plays the panel runs the playhead
// on its own between reports (fps x elapsed time), so the timecode and the sounding word keep up.
function onPlayheadReport(frame) {
  const now = performance.now(), fps = S.view.fps || 25, prev = S.phReport;
  S.phReport = { f: frame, t: now };
  // playing = it moved forward at about real-time speed since the previous report
  const playing = prev && frame > prev.f && now - prev.t < 1500 &&
    Math.abs((frame - prev.f) / fps - (now - prev.t) / 1000) < 0.35 + (now - prev.t) / 2000;
  S.phPlaying = !!playing;
  S.playhead = frame;
  showPlayhead();
  if (S.phPlaying && !S.phRaf) S.phRaf = setTimeout(phTick, 40);
}
function phTick() {
  S.phRaf = 0;
  const r = S.phReport;
  if (!S.phPlaying || !r) return;
  const dt = (performance.now() - r.t) / 1000;
  if (dt > 0.9) { S.phPlaying = false; S.playhead = r.f; showPlayhead(); return; }  // stopped: no more reports
  const f = r.f + Math.floor(dt * (S.view.fps || 25));
  if (f !== S.playhead) { S.playhead = f; showPlayhead(); }
  S.phRaf = setTimeout(phTick, 40);
}

function showPlayhead(scroll = true) {
  const f = S.playhead;
  $("#tcBig").textContent = f == null || !S.view.fps ? "--:--:--:--" : tc(f, S.view.fps);
  const ov = $("#overview"), ph = $("#ovPh");
  if (ph && ov.dataset.span && f != null) ph.style.left = Math.min(100, Math.max(0, (f - +ov.dataset.t0) / +ov.dataset.span * 100)) + "%";
  const blocks = S.view.blocks || [];
  const k = f == null ? -1 : blocks.findIndex((b) => b.s <= f && f < b.e);
  const row = k < 0 ? null : $(`.ol-row[data-k="${k}"]`);
  if (row !== S.phRow || (row && !row.classList.contains("cur"))) {
    $$(".ol-row.cur").forEach((e) => e.classList.remove("cur"));
    S.phRow = row;
    if (row) { row.classList.add("cur"); if (scroll && S.follow) row.scrollIntoView({ block: "nearest" }); }
  }
  let el = null;
  const b = blocks[k];
  if (b && b.m && S.tr[b.m]) {
    const words = S.tr[b.m].words;
    let lo = b.i0, hi = b.i1 - 1, best = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (wordFrame(b, words[mid][0]) <= f) { best = mid; lo = mid + 1; } else hi = mid - 1;
    }
    const ti = S.wordTok.get(k + ":" + best);
    if (ti != null) el = S.tokEls[ti];
  }
  if (el && el === S.phEl && el.isConnected && el.classList.contains("ph")) return;
  $$(".t.ph").forEach((e) => e.classList.remove("ph"));
  S.phEl = el;
  if (!el) return;
  el.classList.add("ph");
  if (scroll && S.follow && !S.busy && !S.selecting && document.activeElement !== $("#searchInput")) {
    const ed = $("#editor"), r = el.getBoundingClientRect(), er = ed.getBoundingClientRect();
    if (r.top < er.top + 40 || r.bottom > er.bottom - 60) el.scrollIntoView({ block: "center", behavior: S.phPlaying ? "smooth" : "auto" });
  }
}

// ------------------------------------------------------------------ selection (whole tokens)

const LIVE = new Set(["w", "p", "clip"]);

function selRange() {
  if (!S.sel || !(S.sel.a >= 0) || !(S.sel.z >= 0)) return null;
  const n = S.tok.length - 1;
  if (n < 0) return null;
  return [Math.min(n, Math.min(S.sel.a, S.sel.z)), Math.min(n, Math.max(S.sel.a, S.sel.z))];
}

function paintSel() {
  for (const i of S.painted || []) S.tokEls[i] && S.tokEls[i].classList.remove("sel");
  S.painted = [];
  const r = selRange();
  if (r) for (let i = r[0]; i <= r[1]; i++) { S.tokEls[i].classList.add("sel"); S.painted.push(i); }
  S.selKey = r ? [S.tok[r[0]], S.tok[r[1]]].map(tokKey) : null;
  paintOverviewSel();
  updateStatusSel();
  showTools();
}

function tokKey(t) { return [t.kind, t.b ?? t.k, t.w ?? t.i ?? t.i0].join(":"); }
function restoreSel() {
  const find = (key) => S.tok.findIndex((t) => tokKey(t) === key);
  const a = find(S.selKey[0]), z = find(S.selKey[1]);
  S.sel = a >= 0 && z >= 0 ? { a, z } : null;
  S.painted = [];
}

function setSel(a, z = a) { S.sel = { a, z }; paintSel(); }
function clearSel() { S.sel = null; S.selKey = null; paintSel(); }

function toPos(t) {
  if (t.kind === "clip") return { b: t.b, w: null };
  if (t.kind === "p") return { b: t.b, w: t.w, p: true };
  return { b: t.b, w: t.w };
}

function selectionPayload() {
  const r = selRange();
  if (!r || !S.tok[r[0]] || !S.tok[r[1]]) return null;  // tokens are being rebuilt
  const toks = S.tok.slice(r[0], r[1] + 1);
  const live = toks.filter((t) => LIVE.has(t.kind));
  if (live.length) {
    const words = live.filter((t) => t.kind === "w").length;
    return { kind: "live", sel: { a: toPos(live[0]), z: toPos(live[live.length - 1]) }, n: words, live };
  }
  const k = toks[0].k;
  let i0 = Infinity, i1 = -Infinity;
  for (const t of toks) {
    if (t.k !== k) continue;
    if (t.kind === "cut") { i0 = Math.min(i0, t.i); i1 = Math.max(i1, t.i + 1); }
    if (t.kind === "chip") { i0 = Math.min(i0, t.i0); i1 = Math.max(i1, t.i1); }
  }
  return { kind: "cut", k, i0, i1, n: i1 - i0 };
}

function tokFrames(t, start) {
  const b = S.view.blocks[t.b];
  if (t.kind === "clip") return start ? b.s : b.e;
  const words = S.tr[b.m].words;
  if (t.kind === "p") {
    if (start) return t.w < b.i0 ? b.s : wordFrame(b, words[t.w][1]);
    return t.w >= b.i1 - 1 ? b.e : wordFrame(b, words[t.w + 1][0]);
  }
  return wordFrame(b, start ? words[t.w][0] : words[t.w][1]);
}

function selFrames() {
  const p = selectionPayload();
  if (!p || p.kind !== "live") return null;
  return [tokFrames(p.live[0], true), tokFrames(p.live[p.live.length - 1], false)];
}

function caretPos() {
  const r = selRange();
  if (!r) return null;
  const t = S.tok.slice(r[0]).find((x) => LIVE.has(x.kind));
  return t ? toPos(t) : null;
}

const SEL_HINT = "Клік — слово · перетягніть — шматок · Shift+клік — до цього слова · 2×клік — речення · 3×клік — абзац · ? — довідка";

function updateStatusSel() {
  const p = selectionPayload();
  if (!p) { $("#statusSel").textContent = SEL_HINT; return; }
  if (p.kind === "cut") { $("#statusSel").textContent = `Вирізане: ${nWords(p.n)} — Del повертає у відео`; return; }
  const fr = selFrames();
  $("#statusSel").textContent = `Виділено ${nWords(p.n)} · ${dur((fr[1] - fr[0]) / S.view.fps)} — Space: програти в Resolve, Del: вирізати`;
}

// floating actions next to the selection
function showTools() {
  const box = $("#seltools");
  if (!box) return;
  const p = selectionPayload();
  if (!p || S.selecting) { box.classList.add("hidden"); return; }
  const r = selRange();
  const el = S.tokEls[r[1]];
  const ed = $("#editor");
  const er = ed.getBoundingClientRect(), tr = el.getBoundingClientRect(), fr = S.tokEls[r[0]].getBoundingClientRect();
  if (p.kind === "cut") {
    box.innerHTML = `<span class="st-info">${nWords(p.n)} вирізано</span><button data-act="del">Повернути у відео <kbd>Del</kbd></button>`;
  } else {
    const f = selFrames();
    box.innerHTML = `<span class="st-info">${nWords(p.n)} · ${dur((f[1] - f[0]) / S.view.fps)}</span>
      <button data-act="play">Програти <kbd>Space</kbd></button>
      <button data-act="del" title="Прибрати з відео (слова лишаться закресленими, якщо ввімкнено «вирізане»)">Вирізати <kbd>Del</kbd></button>
      ${p.live.some((t) => t.kind === "w") ? `<button data-act="fix" title="Виправити текст виділених слів (на відео не впливає)">Виправити <kbd>F2</kbd></button>` : ""}
      ${speakerButtons()}`;
  }
  box.classList.remove("hidden");
  // one row unless it really does not fit (measured at the left edge, where nothing squeezes it)
  box.classList.remove("wrap");
  box.style.left = "0px";
  if (box.offsetWidth > ed.clientWidth - 24) box.classList.add("wrap");
  const w = box.offsetWidth;
  let left = Math.min(Math.max(8, fr.left - er.left), ed.clientWidth - w - 12);
  let top = fr.top - er.top + ed.scrollTop - box.offsetHeight - 8;
  if (fr.top - er.top < box.offsetHeight + 12) top = tr.bottom - er.top + ed.scrollTop + 8;
  box.style.left = left + "px";
  box.style.top = top + "px";
}
function hideTools() { const b = $("#seltools"); if (b) b.classList.add("hidden"); }
// "these words were said by …": one coloured button per speaker (keys 1–9) and «+» for a new one
function speakerButtons() {
  const sp = selSpeakers();
  if (!sp) return "";
  return `<span class="st-spk" title="Хто сказав виділені слова: клік по мовцю (або клавіша 1–9)">${sp.list.map((s, i) =>
    `<button data-spk="${esc(s.id)}" style="--spk:${s.color}" title="Ці слова сказав(ла) ${esc(s.name)}${i < 9 ? " — клавіша " + (i + 1) : ""}"><i></i>${esc(s.name)}</button>`).join("")}
    <button data-spk="new" title="Новий мовець">+</button></span>`;
}

// ------------------------------------------------------------------ edit actions

const edit = (label, body) => act(label, () => api("edit", Object.assign({ v: S.view.v }, body)).then((r) => {
  if (r.undo) { S.undo = r.undo; renderUndo(); }
  return r;
}));

function doDelete() {
  const p = selectionPayload();
  if (!p) return toast("Виділіть слова: клік, перетягування або Shift+клік.");
  const after = () => clearSel();
  if (p.kind === "cut") return edit("Відновлення", { op: "restore", k: p.k, i0: p.i0, i1: p.i1 }).then(after);
  return edit("Вирізання", { op: "delete", sel: p.sel }).then(after);
}
function doCopy(cutToo) {
  const p = selectionPayload();
  if (!p || p.kind !== "live") return toast("Виділіть слова, що є на таймлайні.");
  return act("Копіювання", () => api("edit", { op: "copy", sel: p.sel })).then((r) => {
    if (!r) return;
    S.undo.clipboard = r.res.clips;
    S.clipN = p.n;
    if (cutToo) return edit("Вирізання", { op: "delete", sel: p.sel }).then(clearSel);
    toast(`Скопійовано ${nWords(p.n)}. Ctrl+V вставляє перед виділеним словом.`);
    showTools();
  });
}
// copy a piece straight to a place (Ctrl + drag)
function doCopyTo(sel, pos) {
  return act("Копіювання", () => api("edit", { op: "copy", sel })).then((r) => {
    if (!r) return;
    S.undo.clipboard = r.res.clips;
    return edit("Копія", { op: "paste", pos }).then(clearSel);
  });
}
function doPaste() {
  const pos = caretPos() || { b: null, end: true };
  return edit("Вставлення", { op: "paste", pos });
}
// ---- speakers of the selected words
function selWordRanges() {
  const p = selectionPayload();
  if (!p || p.kind !== "live") return [];
  const out = [];
  for (const t of p.live) {
    if (t.kind !== "w") continue;
    const m = S.view.blocks[t.b].m, last = out[out.length - 1];
    if (last && last.m === m && last.i1 === t.w) last.i1 = t.w + 1;
    else out.push({ m, i0: t.w, i1: t.w + 1 });
  }
  return out;
}
function selSpeakers() {  // speakers of the file the selection starts in
  const r = selWordRanges();
  return r.length ? { m: r[0].m, list: S.tr[r[0].m].speakers } : null;
}
function setSpeaker(sid) {
  const ranges = selWordRanges();
  if (!ranges.length) return toast("Виділіть слова, які сказав інший мовець.");
  const n = ranges.reduce((a, r) => a + r.i1 - r.i0, 0);
  return act("Мовець", () => api("words/speaker", { ranges, sid })).then(() => {
    const sp = selSpeakers();
    const s = sp && sp.list.find((x) => x.id === sid);
    $("#statusEdit").textContent = `${nWords(n)} → ${s ? s.name : "новий мовець"}`;
  });
}
function speakerByNumber(n) {
  const sp = selSpeakers();
  if (!sp) return toast("Виділіть слова, які сказав інший мовець.");
  if (n > sp.list.length) return toast(`Мовця ${n} немає. Новий мовець — кнопка «+» на панелі виділення.`);
  return setSpeaker(sp.list[n - 1].id);
}

function doDuplicate() {
  const p = selectionPayload();
  if (!p || p.kind !== "live") return toast("Виділіть слова для дублювання.");
  return edit("Дублювання", { op: "duplicate", sel: p.sel });
}
function doMove(sel, pos) { return edit("Переміщення", { op: "move", sel, pos }).then(clearSel); }
function goto(body) { return api("goto", body).catch((e) => toast(e.message, "err")); }

// play the selection (or the paragraph under the caret) in Resolve's viewer
async function doPlay() {
  let p = selectionPayload();
  if (!p || p.kind !== "live") {
    toast("Виділіть шматок, який хочете переглянути.");
    return;
  }
  // Space in Resolve toggles playback, so we never press it blindly: whether Resolve plays is known from
  // the playhead the script reports.  Playing already → stop first; at the end → stop our part only.
  const py = window.pywebview && window.pywebview.api;
  clearTimeout(S.previewTimer);
  try {
    if (py && py.space) {
      if ((await api("playback", {})).playing) {
        await py.space(true);  // stop what is playing (focus comes back here)
        for (let i = 0; i < 14 && (await api("playback", {})).playing; i++) await new Promise((ok) => setTimeout(ok, 150));
      }
      const r = await api("preview", { sel: p.sel });  // In/Out + playhead at the start of the selection
      await py.space(false);  // play; Resolve stays in front
      const fps = S.view.fps, ms = (r.f2 - r.f1) / fps * 1000;
      $("#statusEdit").textContent = `Відтворення в Resolve: ${dur((r.f2 - r.f1) / fps)}`;
      S.previewTimer = setTimeout(async () => {
        const st = await api("playback", {}).catch(() => null);
        if (!st || st.frame == null) return;
        // still running through our part (even if the editor came back to this window) → stop it;
        // somewhere else entirely → the editor took over, leave it alone
        const ours = st.frame > r.f1 && st.frame <= r.f2 + fps * 3;
        if (ours && (st.playing || ms < 1500)) await py.space(true);
      }, ms);
    } else if (py && py.play) {
      const r = await api("preview", { sel: p.sel });
      await py.play(r.seconds);
    } else {
      const r = await api("preview", { sel: p.sel });
      await api("preview/start", { seconds: r.seconds });  // browser fallback
    }
  } catch (e) { toast(e.message, "err"); }
}

// ------------------------------------------------------------------ editor events

function tokAt(x, y) {
  const el = document.elementFromPoint(x, y);
  const t = el && el.closest && el.closest("#editor .t");
  return t ? +t.dataset.t : null;
}

// ---- drag & drop of the selection: where it lands = the gap nearest to the cursor
//
// Over the left half of a word the piece goes before it, over the right half — after it (= before the
// next word).  An animated caret with «сюди» shows that gap while dragging.

function dropAt(x, y) {
  const i = tokAt(x, y);
  if (i == null) return null;
  const r = selRange();
  const rect = S.tokEls[i].getBoundingClientRect();
  let j = i, after = x > rect.left + rect.width / 2;
  if (after) {  // after token i = before the next live token
    j = i + 1;
    while (j < S.tok.length && !LIVE.has(S.tok[j].kind)) j++;
  } else if (!LIVE.has(S.tok[i].kind)) {
    while (j < S.tok.length && !LIVE.has(S.tok[j].kind)) j++;
  }
  if (r && j > r[0] && j <= r[1] + 1) return { inside: true };  // onto the piece itself or right next to it
  if (j >= S.tok.length) {  // after the very last word: end of the track
    const last = S.tokEls[S.tokEls.length - 1].getBoundingClientRect();
    return { pos: { b: null, end: true }, x: last.right + 3, y: last.top, h: last.height };
  }
  // the caret stands at the left edge of token j, unless j wrapped to the next line: then after token i
  const rj = S.tokEls[j].getBoundingClientRect();
  const atI = after && Math.abs(rj.top - rect.top) > rect.height / 2;
  return { pos: toPos(S.tok[j]), x: atI ? rect.right + 2 : rj.left - 2, y: atI ? rect.top : rj.top, h: atI ? rect.height : rj.height };
}

function showDropCaret(d) {
  const ed = $("#editor");
  let c = $("#dropCaret");
  if (!c) {
    c = document.createElement("div");
    c.id = "dropCaret";
    c.className = "drop-caret";
    c.innerHTML = `<span class="drop-tag"></span>`;
  }
  if (c.parentNode !== ed) ed.appendChild(c);  // the editor's content is re-rendered: re-attach
  if (!d || !d.pos) { c.classList.add("hidden"); return; }
  const er = ed.getBoundingClientRect();
  c.style.left = d.x - er.left + ed.scrollLeft + "px";
  c.style.top = d.y - er.top + ed.scrollTop + "px";
  c.style.height = d.h + "px";
  $(".drop-tag", c).textContent = S.drag && S.drag.copy ? "копія сюди" : "сюди";
  c.classList.remove("hidden");
}

function setupEditor() {
  const ed = $("#editor");
  ed.addEventListener("mousedown", (ev) => {
    if (ev.button !== 0) return;
    if (ev.target.closest("#seltools")) return;
    const g = ev.target.closest("[data-goto]");
    if (g) { goto({ frame: +g.dataset.goto }); return; }
    const t = ev.target.closest(".t");
    if (!t) { if (!ev.target.closest(".para")) clearSel(); return; }
    ev.preventDefault();
    ed.focus({ preventScroll: true });
    const i = +t.dataset.t;
    if (ev.detail === 3) {  // triple click: the whole paragraph
      const toks = $$(".t", t.closest(".para")).map((e) => +e.dataset.t);
      setSel(toks[0], toks[toks.length - 1]);
      return;
    }
    const r = selRange();
    if (ev.shiftKey && S.sel) { S.sel.z = i; paintSel(); return; }
    // pressing on what is already selected (one word or many) and moving = dragging it somewhere else
    if (r && i >= r[0] && i <= r[1] && selectionPayload().kind === "live") {
      S.drag = { sel: selectionPayload(), x: ev.clientX, y: ev.clientY, active: false, drop: null };
      return;
    }
    S.selecting = { i, x: ev.clientX, y: ev.clientY, moved: false };
    S.sel = { a: i, z: i };
    paintSel();
  });

  document.addEventListener("mousemove", (ev) => {
    if (S.selecting) {
      const i = tokAt(ev.clientX, ev.clientY);
      if (i != null && i !== S.sel.z) { S.sel.z = i; S.selecting.moved = true; paintSel(); }
      autoscroll(ev);
      return;
    }
    const d = S.drag;
    if (!d) return;
    if (!d.active && Math.hypot(ev.clientX - d.x, ev.clientY - d.y) > 5) {
      d.active = true;
      const words = d.sel.live.filter((t) => t.kind === "w").map((t) => S.tr[S.view.blocks[t.b].m].words[t.w][2]);
      d.ghost = document.createElement("div");
      d.ghost.className = "drag-ghost";
      d.ghost.textContent = words.slice(0, 6).join(" ") + (words.length > 6 ? " …" : "");
      document.body.appendChild(d.ghost);
      ed.classList.add("dragging");
      hideTools();
    }
    if (!d.active) return;
    d.copy = ev.ctrlKey || ev.metaKey;
    d.ghost.classList.toggle("copy", d.copy);
    d.ghost.style.left = ev.clientX + 14 + "px";
    d.ghost.style.top = ev.clientY + 14 + "px";
    d.drop = dropAt(ev.clientX, ev.clientY);
    showDropCaret(d.drop);
    autoscroll(ev);
  });

  document.addEventListener("mouseup", (ev) => {
    if (S.selecting) {
      const s = S.selecting;
      S.selecting = null;
      const t = S.tok[s.i];
      if (!s.moved) {
        if (t.kind === "chip") { S.expandedCuts.add(t.id); renderEditor(); return; }
        if (LIVE.has(t.kind)) goto(t.kind === "w" ? { b: t.b, w: t.w } : { frame: tokFrames(t, true) });
      }
      paintSel();
      return;
    }
    const d = S.drag;
    if (!d) return;
    S.drag = null;
    d.ghost && d.ghost.remove();
    ed.classList.remove("dragging");
    showDropCaret(null);
    if (d.active && d.drop && d.drop.pos) {
      if (ev.ctrlKey || ev.metaKey) doCopyTo(d.sel.sel, d.drop.pos);
      else doMove(d.sel.sel, d.drop.pos);
    } else if (!d.active) showTools();
  });

  ed.addEventListener("dblclick", (ev) => {
    const t = ev.target.closest(".t.w");
    if (!t) return;
    // whole sentence inside the paragraph
    const toks = $$(".t", t.closest(".para")).map((e) => +e.dataset.t);
    const i = +t.dataset.t;
    const endsSentence = (j) => S.tok[j].kind === "w" && /[.!?…]$/.test(S.tokEls[j].textContent);
    let a = toks.indexOf(i), z = a;
    while (a > 0 && !endsSentence(toks[a - 1])) a--;
    while (z < toks.length - 1 && !endsSentence(toks[z])) z++;
    setSel(toks[a], toks[z]);
  });

  ed.addEventListener("click", (ev) => {
    const b = ev.target.closest("#seltools button");
    if (!b) return;
    if (b.dataset.spk) { setSpeaker(b.dataset.spk); return; }
    const a = b.dataset.act;
    if (a === "play") doPlay();
    else if (a === "del") doDelete();
    else if (a === "fix") {
      editSelection();
    }
  });

  ed.addEventListener("contextmenu", (ev) => {
    ev.preventDefault();
    const t = ev.target.closest(".t");
    if (t) {
      const i = +t.dataset.t, r = selRange();
      if (!r || i < r[0] || i > r[1]) setSel(i);
    }
    const p = selectionPayload();
    const items = [];
    if (p && p.kind === "live") {
      items.push(["Програти в Resolve", "Space", doPlay], null,
        ["Вирізати з відео", "Del", doDelete],
        ["Копіювати", "Ctrl+C", () => doCopy(false)],
        ["Вирізати в буфер", "Ctrl+X", () => doCopy(true)], ["Дублювати поруч", "Ctrl+D", doDuplicate],
        ["Вставити скопійоване перед цим словом", "Ctrl+V", doPaste, !S.undo.clipboard]);
      const first = p.live.find((x) => x.kind === "w");
      if (first) {
        const b = S.view.blocks[first.b];
        items.push(null, [p.n > 1 ? "Виправити текст" : "Виправити слово", "F2", editSelection]);
        items.push({ label: "Виділені слова сказав:" });
        S.tr[b.m].speakers.forEach((s, idx) => items.push([s.name, idx < 9 ? String(idx + 1) : "", () => setSpeaker(s.id)]));
        items.push(["Новий мовець", "", () => setSpeaker("new")]);
      }
    } else if (p && p.kind === "cut") {
      items.push(["Повернути у відео", "Del", doDelete]);
    } else {
      items.push(["Вставити в кінець", "", () => edit("Вставлення", { op: "paste", pos: { b: null, end: true } }), !S.undo.clipboard]);
    }
    showMenu(ev.clientX, ev.clientY, items);
  });

  ed.addEventListener("scroll", () => { if (!S.selecting) showTools(); });

  $("#legend").addEventListener("click", (ev) => {
    if (ev.target.closest("#btnHelp")) { helpDialog(); return; }
    const b = ev.target.closest(".lg[data-sid]");
    if (b) renameSpeaker(b.dataset.m, b.dataset.sid);
  });
  $("#legend").addEventListener("change", (ev) => {
    if (ev.target.id === "showCut") { S.showCut = ev.target.checked; try { localStorage.setItem("dd.showCut", S.showCut ? "1" : "0"); } catch (e) { /* optional */ } renderEditor(); }
  });
  $("#outline").addEventListener("click", (ev) => {
    const row = ev.target.closest(".ol-row");
    if (!row) return;
    const k = +row.dataset.k;
    const [a, z] = S.blockTok[k] || [];
    if (a != null && z > a) { S.tokEls[a].scrollIntoView({ block: "center" }); setSel(a, z - 1); }
    goto({ frame: S.view.blocks[k].s });
  });
  $("#overview").addEventListener("click", (ev) => {
    const ov = $("#overview"), r = ov.getBoundingClientRect();
    const f = Math.round(+ov.dataset.t0 + (ev.clientX - r.left) / r.width * +ov.dataset.span);
    goto({ frame: f });
  });
}

function autoscroll(ev) {
  const ed = $("#editor"), r = ed.getBoundingClientRect();
  if (ev.clientY < r.top + 30) ed.scrollTop -= 16; else if (ev.clientY > r.bottom - 30) ed.scrollTop += 16;
}

// correct the text of the selected words (one word, a phrase or a whole sentence of one clip)
function editSelection() {
  const p = selectionPayload();
  const ws = p && p.live ? p.live.filter((x) => x.kind === "w") : [];
  if (!ws.length) return;
  const kb = ws[0].b, sameClip = ws.filter((x) => x.b === kb);
  editWords(kb, sameClip[0].w, sameClip[sameClip.length - 1].w + 1);
}

function editWords(k, i0, i1) {
  const b = S.view.blocks[k], key = b.m;
  const first = S.wordTok.get(k + ":" + i0), last = S.wordTok.get(k + ":" + (i1 - 1));
  if (!key || first == null || last == null) return;
  const t = S.tr[key];
  const orig = t.words.slice(i0, i1).map((w) => w[2]).join(" ");
  const els = S.tokEls.slice(first, last + 1);
  const width = els.reduce((s, e) => s + e.offsetWidth + 4, 0);
  const input = document.createElement("textarea");
  input.value = orig;
  input.className = "word-edit";
  input.rows = 1;
  input.spellcheck = false;
  input.style.width = Math.min($("#editor").clientWidth - 40, Math.max(80, width + 40)) + "px";
  hideTools();
  els[0].replaceWith(input);
  els.slice(1).forEach((e) => (e.style.display = "none"));
  const fit = () => { input.style.height = "auto"; input.style.height = input.scrollHeight + "px"; };
  fit();
  input.addEventListener("input", fit);
  input.focus(); input.select();
  let done = false;
  const finish = (save) => {
    if (done) return;
    done = true;
    const text = input.value.replace(/\s+/g, " ").trim();
    if (save && text && text !== orig) {
      act("Виправлення", () => i1 - i0 === 1 && !text.includes(" ")
        ? api("word", { m: key, i: i0, text })
        : api("words", { m: key, i0, i1, text }));
    } else renderEditor();
  };
  input.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (e.key === "Enter") { e.preventDefault(); finish(true); }
    if (e.key === "Escape") finish(false);
  });
  input.addEventListener("blur", () => finish(true));
}

function renameSpeaker(key, sid) {
  const s = S.tr[key].speakers.find((x) => x.id === sid);
  modal("Перейменувати мовця", `<div class="frow"><span class="fl">Ім'я</span><span class="fc"><input type="text" id="spName" value="${esc(s.name)}"></span></div>`,
    [["Скасувати"], ["Зберегти", "primary", () => act("Мовець", () => api("speaker/rename", { m: key, sid, name: $("#spName").value }))]], "narrow");
  setTimeout(() => $("#spName").select(), 30);
}

// ------------------------------------------------------------------ keyboard

function setupKeys() {
  document.addEventListener("keydown", (ev) => {
    const tag = (ev.target.tagName || "").toLowerCase();
    const typing = tag === "input" || tag === "textarea" || tag === "select";
    const mod = ev.ctrlKey || ev.metaKey;
    // shortcuts by the physical key, so Ctrl+Z works on the Ukrainian layout too (ev.key would be "я")
    const k = /^Key[A-Z]$/.test(ev.code || "") ? ev.code.slice(3).toLowerCase() : (ev.key || "").toLowerCase();
    if (mod && k === "f") { ev.preventDefault(); $("#searchInput").focus(); $("#searchInput").select(); return; }
    if (ev.key === "Escape") {
      hideMenu(); closeModal();
      if (S.drag) { S.drag.ghost && S.drag.ghost.remove(); S.drag = null; $("#editor").classList.remove("dragging"); showDropCaret(null); }
      clearSel(); return;
    }
    if (typing || !$("#modalBack").classList.contains("hidden")) return;
    if (ev.key === "?" || (ev.key === "F1")) { ev.preventDefault(); helpDialog(); return; }
    if (!ev.ctrlKey && !ev.metaKey && !ev.altKey && /^[1-9]$/.test(ev.key) && selWordRanges().length) {
      ev.preventDefault(); speakerByNumber(+ev.key); return;  // these words were said by speaker N
    }
    const n = (S.tok || []).length;
    if (ev.key === "Delete" || ev.key === "Backspace") { ev.preventDefault(); doDelete(); }
    else if (ev.key === " ") { ev.preventDefault(); doPlay(); }
    else if (ev.key === "ArrowRight" || ev.key === "ArrowLeft") {
      if (!S.sel || !n) return;
      ev.preventDefault();
      const d = ev.key === "ArrowRight" ? 1 : -1;
      if (ev.shiftKey) { S.sel.z = Math.min(n - 1, Math.max(0, S.sel.z + d)); paintSel(); }
      else { const i = Math.min(n - 1, Math.max(0, (d > 0 ? Math.max(S.sel.a, S.sel.z) : Math.min(S.sel.a, S.sel.z)) + d)); setSel(i); }
      S.tokEls[S.sel.z].scrollIntoView({ block: "nearest" });
    }
    else if (mod && k === "a") { if (n) { ev.preventDefault(); setSel(0, n - 1); } }
    else if (ev.key === "F2") { ev.preventDefault(); editSelection(); }
    else if (mod && k === "z" && !ev.shiftKey) { ev.preventDefault(); edit("Скасування", { op: "undo" }); }
    else if (mod && (k === "y" || (k === "z" && ev.shiftKey))) { ev.preventDefault(); edit("Повтор", { op: "redo" }); }
    else if (mod && k === "c") { if (selectionPayload()) { ev.preventDefault(); doCopy(false); } }
    else if (mod && k === "x") { ev.preventDefault(); doCopy(true); }
    else if (mod && k === "v") { ev.preventDefault(); doPaste(); }
    else if (mod && k === "d") { ev.preventDefault(); doDuplicate(); }
  });
}

// ------------------------------------------------------------------ search

function runSearch(jump = true) {
  const q = $("#searchInput").value.trim().toLowerCase();
  $$(".t.hit").forEach((e) => e.classList.remove("hit", "cur"));
  S.hits = [];
  if (!q) { $("#searchCount").textContent = ""; return; }
  const terms = q.split(/\s+/);
  const inCut = $("#searchCut").checked;
  if (inCut && !S.showCut) { S.showCut = true; renderEditor(); return; }
  const idx = [];
  S.tok.forEach((t, i) => { if (t.kind === "w" || (inCut && t.kind === "cut")) idx.push(i); });
  const clean = (i) => S.tokEls[i].textContent.toLowerCase().replace(/[.,!?…:;«»"()]/g, "");
  for (let j = 0; j + terms.length <= idx.length; j++) {
    let ok = true;
    for (let m = 0; m < terms.length; m++) {
      const t = clean(idx[j + m]);
      if (m === terms.length - 1 ? !t.startsWith(terms[m]) && !t.includes(terms[m]) : t !== terms[m]) { ok = false; break; }
    }
    if (ok) S.hits.push([idx[j], idx[j + terms.length - 1]]);
  }
  S.hits.forEach(([a, z]) => { for (let i = a; i <= z; i++) S.tokEls[i].classList.add("hit"); });
  S.hitIdx = S.hits.length ? Math.min(Math.max(S.hitIdx, 0), S.hits.length - 1) : -1;
  updateHit(jump);
}
function updateHit(jump) {
  $$(".t.hit.cur").forEach((e) => e.classList.remove("cur"));
  $("#searchCount").textContent = S.hits.length ? `${S.hitIdx + 1} / ${S.hits.length}` : "0";
  const g = S.hits[S.hitIdx];
  if (!g) return;
  for (let i = g[0]; i <= g[1]; i++) S.tokEls[i].classList.add("cur");
  if (jump) S.tokEls[g[0]].scrollIntoView({ block: "center" });
}
function stepHit(d) {
  if (!S.hits.length) return;
  S.hitIdx = (S.hitIdx + d + S.hits.length) % S.hits.length;
  updateHit(true);
  const g = S.hits[S.hitIdx];
  setSel(g[0], g[1]);  // Del / Space act on the match
}

// ------------------------------------------------------------------ menus & modals

function showMenu(x, y, items) {
  const m = $("#ctxMenu");
  m.innerHTML = "";
  for (const it of items) {
    if (it === null) { m.appendChild(document.createElement("hr")); continue; }
    if (it.label) { const d = document.createElement("div"); d.className = "menu-label"; d.textContent = it.label; m.appendChild(d); continue; }
    const [label, key, fn, disabled] = it;
    const b = document.createElement("button");
    b.innerHTML = `<span>${esc(label)}</span><span class="k">${esc(key || "")}</span>`;
    b.disabled = !!disabled;
    b.onclick = () => { hideMenu(); fn(); };
    m.appendChild(b);
  }
  m.classList.remove("hidden");
  const r = m.getBoundingClientRect();
  m.style.left = Math.min(x, innerWidth - r.width - 6) + "px";
  m.style.top = Math.min(y, innerHeight - r.height - 6) + "px";
}
function hideMenu() { $("#ctxMenu").classList.add("hidden"); }
document.addEventListener("mousedown", (e) => { if (!e.target.closest("#ctxMenu")) hideMenu(); });

// buttons: [label, kind, fn]; kind "primary" (Enter), "ghost", "left" (secondary actions at the left edge)
function modal(title, body, buttons, size = "") {
  const m = $("#modal");
  m.className = "modal " + size;
  m.innerHTML = `<div class="modal-head">${esc(title)}<button class="mini" data-close title="Закрити (Esc)"><svg><use href="#i-close"/></svg></button></div>
    <div class="modal-body">${body}</div><div class="modal-foot"><div class="foot-left"></div></div>`;
  const foot = $(".modal-foot", m), left = $(".foot-left", m);
  for (const [label, kind = "", fn] of buttons) {
    const b = document.createElement("button");
    b.className = "btn " + kind.replace("left", "").trim();
    b.textContent = label;
    b.onclick = async () => { if (fn && (await fn()) === false) return; closeModal(); };
    (kind.includes("left") ? left : foot).appendChild(b);
  }
  $("[data-close]", m).onclick = closeModal;
  m.onkeydown = (e) => {
    const p = $(".modal-foot .btn.primary", m);
    if (e.key === "Enter" && p && e.target.tagName !== "TEXTAREA" && e.target.tagName !== "BUTTON") { e.preventDefault(); p.click(); }
  };
  $("#modalBack").classList.remove("hidden");
}
const note = (html, cls = "") => `<div class="note ${cls}"><svg><use href="#i-info"/></svg><div>${html}</div></div>`;
function closeModal() { $("#modalBack").classList.add("hidden"); }

// ------------------------------------------------------------------ transcribe / export / import

// what is installed changes rarely: the dialog opens at once with the last known state and refreshes it
// in the background (the first check after a start imports torch in a separate process — seconds)
function downloadsNote(comp) {
  const need = ["whisper", "speakers"].filter((k) => comp[k] && !comp[k].installed).map((k) => comp[k].size);
  const asr = (comp.models || [])[0];
  if (asr && !asr.available) need.push(asr.size);
  if (!need.length) return "";
  return note(`Перший запуск: програма один раз завантажить модель і компоненти (${need.map(esc).join(" + ")}). Далі все працює без інтернету.`);
}

async function transcribeDialog() {
  const s = S.settings || {};
  const comp = S.comp || {};
  const asr = (comp.models || [])[0] || { label: "Parakeet TDT 0.6B v3 UK" };
  api("components").then((c) => {
    S.comp = c;
    const box = $("#tComp");
    if (box && box.isConnected) box.innerHTML = downloadsNote(c);
  }).catch(() => { /* the note is optional */ });
  modal("Транскрибування", `
    <p class="lead">Розпізнає мову в кліпах доріжки <b>${esc(S.view.track || "")}</b>. Кожне слово прив'язується до свого місця у відео.</p>
    <div class="fsec"><div class="fsec-h">Розпізнавання</div>
      <div class="frow"><span class="fl">Модель</span><span class="fc"><span class="val" title="${esc(asr.credit || "")}">${esc(asr.label)}</span><span class="sub">українська, англійські назви латиницею</span></span></div>
      <div class="frow"><span class="fl">Що розпізнати</span><span class="fc"><span class="seg">
        <label><input type="radio" name="tScope" value="used" checked><span>Кліпи на таймлайні</span></label>
        <label><input type="radio" name="tScope" value="whole"><span>Файли повністю</span></label></span></span></div>
      <div class="frow"><span class="fl"></span><span class="fc"><label class="chk"><input type="checkbox" id="tForce"> Розпізнати заново, замінивши наявний текст</label></span></div>
    </div>
    <div class="fsec"><div class="fsec-h">Мовці</div>
      <div class="frow"><span class="fl">Визначати</span><span class="fc"><label class="chk"><input type="checkbox" id="tDiar" ${s.diarize !== false ? "checked" : ""}> Розфарбувати текст за мовцями</label></span></div>
      <div class="frow" id="tSpkRow"><span class="fl">Кількість</span><span class="fc"><input type="number" class="num" id="tSpk" min="0" max="12" value="${s.num_speakers || ""}" placeholder="авто"><span class="sub">порожньо — визначити автоматично</span></span></div>
      <div class="frow" id="tHfRow"><span class="fl">Токен Hugging Face</span><span class="fc">${s.has_hf_token
        ? `<span class="val">збережено</span><button class="btn sm ghost" id="tHfChange">Змінити</button><input type="text" id="tHf" class="hidden" placeholder="hf_…" style="flex:1">`
        : `<input type="text" id="tHf" placeholder="hf_… — потрібен один раз для моделі мовців">`}</span></div>
    </div>
    <div id="tComp">${downloadsNote(comp)}</div>`,
    [["Лише мовці", "left", async () => {
      const upd = { num_speakers: +$("#tSpk").value || 0, diarize: true };
      if ($("#tHf").value.trim()) upd.hf_token = $("#tHf").value.trim();
      try { S.settings = await api("settings", upd); await api("diarize", { speakers: upd.num_speakers }); }
      catch (e) { toast(e.message, "err"); return false; }
    }], ["Скасувати"], ["Почати", "primary", async () => {
      const opts = { diarize: $("#tDiar").checked, speakers: +$("#tSpk").value || 0,
        whole: ($('input[name="tScope"]:checked') || {}).value === "whole", force: $("#tForce").checked };
      const upd = { diarize: opts.diarize, num_speakers: opts.speakers };
      if ($("#tHf").value.trim()) upd.hf_token = $("#tHf").value.trim();
      try { S.settings = await api("settings", upd); await api("transcribe", opts); }
      catch (e) { toast(e.message, "err"); return false; }
    }]]);
  const sync = () => ["#tSpkRow", "#tHfRow"].forEach((r) => $(r).classList.toggle("hidden", !$("#tDiar").checked));
  $("#tDiar").onchange = sync; sync();
  if ($("#tHfChange")) $("#tHfChange").onclick = () => { $("#tHfChange").remove(); $("#tHf").classList.remove("hidden"); $("#tHf").focus(); };
}

async function copyText(url, label) {
  try {
    const r = await fetch(url);
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || "HTTP " + r.status);
    await navigator.clipboard.writeText(await r.text());
    toast(label + " — скопійовано. Вставте в чат Claude (Ctrl+V).", "ok");
  } catch (e) { toast(e.message, "err"); }
}

async function exportDialog() {
  let est;
  try { est = await api("export/estimate"); } catch (e) { return toast(e.message, "err"); }
  const k = (n) => (n / 1000).toFixed(n < 10000 ? 1 : 0) + " тис.";
  const br = est.brief || {}, durs = est.durations || {};
  const mins = (s) => (s < 120 ? `${s} с` : `${Math.round(s / 60)} хв`);
  const durOpts = Object.entries(durs).map(([v, [label, lo, hi]]) =>
    `<option value="${v}" ${br.duration === v ? "selected" : ""}>${esc(label)} · ${mins(lo)}–${mins(hi)}</option>`).join("")
    + `<option value="custom" ${br.duration === "custom" ? "selected" : ""}>своя тривалість…</option>`;
  const single = est.mode === "single";
  const how = single
    ? note(`Обсяг ≈ <b>${k(est.tokens)}</b> токенів — один запит. Скопіюйте його в чат мовної моделі (Claude, ChatGPT…), а відповідь вставте через «Імпорт».`)
    : note(`Обсяг ≈ <b>${k(est.tokens)}</b> токенів — забагато для одного чату, тому два етапи.<br>
       <b>Етап 1:</b> ${est.parts} частин — кожну окремим чатом, кожну відповідь імпортуйте (це ключові моменти).<br>
       <b>Етап 2</b> (моментів зараз: ${est.beats}): з моментів модель складе відео за вашим завданням.`);
  const parts = single ? "" : `<div class="frow"><span class="fl">Етап 1</span><span class="fc">${Array.from({ length: est.parts }, (_, i) =>
    `<button class="btn sm" data-copypart="${i + 1}">Копіювати частину ${i + 1}</button>`).join("")}</span></div>`;
  const tn = est.topics_parts || 1;
  const topics = `<div class="fsec"><div class="fsec-h">Теми</div>
    <p class="lead">Окремий короткий запит: модель поділить розмову на теми, і після імпорту вони стануть заголовками в тексті.${est.topics ? ` Зараз тем: <b>${est.topics}</b>.` : ""}</p>
    <div class="frow"><span class="fl"></span><span class="fc">${Array.from({ length: tn }, (_, i) =>
      `<button class="btn sm" data-topics="${i + 1}">Копіювати запит на теми${tn > 1 ? ` (${i + 1})` : ""}</button>`).join("")}
      ${est.topics ? `<button class="btn sm ghost" id="topicsClear">Прибрати теми</button>` : ""}</span></div></div>`;
  modal("Завдання для мовної моделі", `
    <p class="lead">Опишіть, яке відео потрібне. Програма складе запит із транскрипцією, а модель запропонує варіанти сюжету з таймкодами.</p>
    <div class="fsec"><div class="fsec-h">Відео</div>
      <div class="frow"><span class="fl">Тривалість</span><span class="fc"><select id="brDur">${durOpts}</select>
        <span id="brCustom" class="${br.duration === "custom" ? "" : "hidden"}">від <input type="number" class="num" id="brMin" min="5" value="${br.min_sec || ""}"> до
        <input type="number" class="num" id="brMax" min="5" value="${br.max_sec || ""}"> с</span></span></div>
      <div class="frow"><span class="fl">Варіантів</span><span class="fc"><input type="number" class="num" id="brVar" min="1" max="5" value="${br.variants || 3}"></span></div>
      <div class="frow top"><span class="fl">Що потрібно</span><span class="fc"><textarea id="brNotes" rows="4" placeholder="Своїми словами: про що відео, для кого, настрій і темп, що обов'язково лишити, що викинути, як почати й закінчити…">${esc(br.notes || "")}</textarea></span></div>
      <div class="frow"><span class="fl">Матеріал</span><span class="fc"><span class="seg">
        <label><input type="radio" name="exScope" value="all" checked><span>Уся транскрипція</span></label>
        <label><input type="radio" name="exScope" value="tl"><span>Лише таймлайн</span></label></span></span></div>
    </div>
    <div class="fsec"><div class="fsec-h">Запит</div>${how}${parts}</div>
    ${topics}`,
    [single ? ["Завантажити файл", "left ghost", () => { saveBrief().then(() => { location.href = `/api/export/claude?only=${onlyTl()}`; }); }]
            : ["Завантажити частини (.zip)", "left ghost", () => { saveBrief().then(() => { location.href = `/api/export/claude?only=${onlyTl()}`; }); }],
     ["Закрити", "", () => { saveBrief(); }],
     single ? ["Копіювати запит", "primary", () => { saveBrief().then(() => copyText(`/api/export/claude?raw=1&only=${onlyTl()}`, "Запит")); return false; }]
            : ["Етап 2: відео з моментів", "primary", () => {
                if (!est.beats) { toast("Спершу імпортуйте відповіді на частини етапу 1.", "err"); return false; }
                saveBrief().then(() => copyText("/api/export/stage2?raw=1", "Етап 2")); return false; }]], "wide");
  $("#brDur").onchange = () => $("#brCustom").classList.toggle("hidden", $("#brDur").value !== "custom");
  $$("[data-topics]").forEach((b) => (b.onclick = () =>
    copyText(`/api/export/topics?raw=1&part=${b.dataset.topics}&only=${onlyTl()}`, tn > 1 ? `Теми, частина ${b.dataset.topics}` : "Запит на теми")));
  if ($("#topicsClear")) $("#topicsClear").onclick = () => api("topics/clear", {}).then(() => {
    S.topics = []; renderEditor(); closeModal(); toast("Теми прибрано.", "ok");
  }).catch((e) => toast(e.message, "err"));
  $$("[data-copypart]").forEach((b) => (b.onclick = () => saveBrief().then(() =>
    copyText(`/api/export/claude?raw=1&part=${b.dataset.copypart}&only=${onlyTl()}`, "Частину " + b.dataset.copypart))));
}
// the brief is read from the dialog and saved with the project before every copy / download
function saveBrief() {
  if (!$("#brDur")) return Promise.resolve();
  return api("brief", { duration: $("#brDur").value, min_sec: $("#brMin").value, max_sec: $("#brMax").value,
    variants: +$("#brVar").value || 1, notes: $("#brNotes").value }).catch((e) => toast(e.message, "err"));
}
const onlyTl = () => ($('input[name="exScope"]:checked') || {}).value === "tl" ? "1" : "";

function importDialog() {
  modal("Імпорт відповіді", `
    <p class="lead">Вставте відповідь мовної моделі цілком — програма сама знайде в ній сюжети, теми чи моменти.</p>
    <textarea class="big" id="impText" placeholder="Вставте відповідь сюди (Ctrl+V)"></textarea>
    <div class="frow" style="grid-template-columns:auto 1fr"><button class="btn sm" id="impFile">Або вибрати файл…</button><span class="sub" id="impName"></span></div>`,
    [["Скасувати"], ["Імпортувати", "primary", async () => {
      const text = $("#impText").value.trim();
      if (!text) { toast("Вставте JSON або виберіть файл.", "err"); return false; }
      try {
        const r = await api("stories/import", { text });
        S.stories = r.stories;
        S.topics = r.topics || S.topics;
        if (r.kind === "topics") {
          renderEditor();
          toast(`Тем: ${r.count} (усього ${r.total}). Вони з'явилися в тексті заголовками.`
            + (r.warnings && r.warnings.length ? "\n" + r.warnings.join("\n") : ""), "ok");
          return;
        }
        if (r.kind === "beats") {
          toast(`Імпортовано моментів: ${r.count} (усього ${r.total}). Коли всі частини готові — «Експорт для Claude» > «Етап 2».`
            + (r.warnings && r.warnings.length ? "\n" + r.warnings.join("\n") : ""), "ok");
          return;
        }
        S.activeStory = r.imported[0];
        showTab("stories");
        renderStories();
        toast(`Імпортовано варіантів: ${r.imported.length}`, "ok");
      } catch (e) { toast(e.message, "err"); return false; }
    }]]);
  $("#impFile").onclick = () => {
    const fi = $("#fileInput");
    fi.value = "";
    fi.onchange = async () => {
      const f = fi.files[0];
      if (!f) return;
      $("#impName").textContent = f.name;
      $("#impText").value = await f.text();
    };
    fi.click();
  };
}

// ------------------------------------------------------------------ stories panel

function fragInfo(fid) {
  const m = /^([A-Z]+)0*(\d+)$/.exec(fid);
  if (!m) return null;
  const key = Object.keys(S.tr).find((k) => S.tr[k].tag === m[1]);
  if (!key) return null;
  const t = S.tr[key], n = +m[2] - 1;
  if (!t.frags[n]) return null;
  const [a, z] = t.frags[n];
  const onTl = S.view.blocks.some((b) => b.m === key && b.i0 < z && b.i1 > a);
  return { key, n, a, z, t0: t.words[a][0], t1: t.words[z - 1][1], onTl, text: t.words.slice(a, z).map((w) => w[2]).join(" ") };
}

function sceneDuration(sc) {
  let s = 0;
  for (const f of sc.fragments) { const i = fragInfo(f); if (i) s += i.t1 - i.t0 + 0.3; }
  return s;
}

function renderStories() {
  const box = $("#tab-stories");
  const list = S.stories || [];
  if (!list.length) {
    box.innerHTML = `<div class="sec hint">
      <div class="sec-title">Пошук сюжетів через Claude</div>
      <p>1. Транскрибуйте таймлайн.</p>
      <p>2. «Експорт для Claude» — опишіть, що потрібно (тривалість, кількість варіантів, побажання), і скопіюйте готовий запит у чат мовної моделі. Довгий запис автоматично ділиться на частини й проходить у два етапи.</p>
      <p>3. «Імпорт сюжетів» — завантажте відповідь. Тут з'являться варіанти зі сценами, рекомендаціями та закадровим текстом.</p>
      <p>4. Застосуйте варіант до таймлайна Resolve повністю або сцена за сценою.</p>
      <div class="actions"><button class="btn" data-a="export">Експорт для Claude</button><button class="btn" data-a="import">Імпорт сюжетів</button></div></div>`;
    return;
  }
  if (!list.find((s) => s.uid === S.activeStory)) S.activeStory = list[0].uid;
  let h = `<div class="variants">`;
  for (const s of list) {
    const total = s.scenes.filter((x) => x.enabled).reduce((a, x) => a + sceneDuration(x), 0);
    h += `<div class="variant ${s.uid === S.activeStory ? "active" : ""}" data-story="${s.uid}">
      <span class="t">${esc(s.title)}</span><span class="meta">${s.scenes.length} сц. · ${dur(total)}</span>
      ${s.logline ? `<span class="log">${esc(s.logline)}</span>` : ""}</div>`;
  }
  h += `</div>`;
  const s = list.find((x) => x.uid === S.activeStory);
  if (s) h += storyDetail(s);
  box.innerHTML = h;
}

function storyDetail(s) {
  let h = `<div class="story-head"><h3>${esc(s.title)}</h3>
    ${s.concept ? `<div class="concept">${esc(s.concept)}</div>` : ""}
    ${s.target_duration_sec ? `<div class="hint">Цільова тривалість: ${dur(s.target_duration_sec)}</div>` : ""}
    ${s.warnings && s.warnings.length ? `<div class="warns">${s.warnings.slice(0, 6).map(esc).join("<br>")}${s.warnings.length > 6 ? "<br>…" : ""}</div>` : ""}
    <div class="actions" style="margin-top:10px">
      <button class="btn primary" data-a="apply-new" title="Створює новий таймлайн у проєкті Resolve з кліпами в порядку сцен">Застосувати: новий таймлайн</button>
      <button class="btn" data-a="apply-replace" title="Перебудовує доріжку поточного таймлайна">Замінити монтаж</button>
      <button class="btn ghost" data-a="story-more">Ще</button></div></div>`;
  s.scenes.forEach((sc, i) => {
    const d = sceneDuration(sc);
    const frags = sc.fragments.map((f) => { const fi = fragInfo(f); return `<span class="frag ${fi && fi.onTl ? "" : "off"}" data-fid="${f}" title="${fi ? esc(fi.text.slice(0, 200)) + (fi.onTl ? "" : "\n(зараз не на таймлайні)") : "не знайдено"}">${f}</span>`; }).join("");
    const first = fragInfo(sc.fragments[0] || "");
    const range = first ? `${dur(first.t0)}${sc.fragments.length > 1 ? "–" + dur((fragInfo(sc.fragments[sc.fragments.length - 1]) || first).t1) : "–" + dur(first.t1)}` : "";
    h += `<div class="scene ${sc.enabled ? "" : "off"}" data-scene="${sc.uid}" draggable="true">
      <div class="grip" title="Перетягніть, щоб змінити порядок"><svg><use href="#i-grip"/></svg></div>
      <div class="body">
        <div class="scene-top"><span class="scene-n">${i + 1}</span><span class="scene-title">${esc(sc.title)}</span><span class="role" data-role="${esc(sc.role)}">${esc(sc.role_label || sc.role)}</span></div>
        <div class="hint">${range ? "джерело " + range + " · " : ""}≈ ${dur(d)}</div>
        ${sc.summary ? `<p>${esc(sc.summary)}</p>` : ""}
        ${sc.role_note ? `<p><span class="lbl">Роль: </span>${esc(sc.role_note)}</p>` : ""}
        ${sc.edit_notes ? `<p><span class="lbl">Монтаж: </span>${esc(sc.edit_notes)}</p>` : ""}
        ${sc.context ? `<p><span class="lbl">Контекст: </span>${esc(sc.context)}</p>` : ""}
        ${sc.transition ? `<p><span class="lbl">Перехід: </span>${esc(sc.transition)}</p>` : ""}
        <div class="frags">${frags || '<span class="hint">без фрагментів</span>'}</div>
        ${first ? `<div class="quote">${esc(first.text.slice(0, 220))}</div>` : ""}
        <div class="vo"><span class="lbl">Закадровий текст${sc.voiceover && sc.voiceover.placement ? " (" + esc(sc.voiceover.placement) + ")" : ""}</span>
          <textarea data-vo="${sc.uid}" placeholder="немає">${esc(sc.voiceover ? sc.voiceover.text : "")}</textarea></div>
        <div class="scene-actions">
          <label class="chk"><input type="checkbox" data-en="${sc.uid}" ${sc.enabled ? "checked" : ""}> у варіанті</label>
          <button class="btn sm" data-a="scene-go" data-sc="${sc.uid}" title="Показати перший фрагмент сцени в Resolve"><svg><use href="#i-play"/></svg>Перейти</button>
          <button class="btn sm" data-a="scene-ph" data-sc="${sc.uid}" title="Вставити сцену на таймлайн у позицію плейхеда (на найближчу межу кліпа)">У плейхед</button>
          <button class="btn sm" data-a="scene-end" data-sc="${sc.uid}">У кінець</button>
        </div></div></div>`;
  });
  const notes = [].concat(s.editing_notes || []);
  if (notes.length || s.music_and_sound || s.ending) {
    h += `<div class="sec hint"><div class="sec-title">Рекомендації</div>${notes.map((n) => `<p>${esc(n)}</p>`).join("")}
      ${s.music_and_sound ? `<p><span class="lbl">Музика і звук: </span>${esc(s.music_and_sound)}</p>` : ""}
      ${s.ending ? `<p><span class="lbl">Фінал: </span>${esc(s.ending)}</p>` : ""}</div>`;
  }
  return h;
}

function setupStories() {
  const box = $("#tab-stories");
  box.addEventListener("click", (ev) => {
    const v = ev.target.closest("[data-story]");
    if (v) { S.activeStory = v.dataset.story; renderStories(); return; }
    const f = ev.target.closest(".frag");
    if (f) return goto({ fid: f.dataset.fid });
    const b = ev.target.closest("[data-a]");
    if (!b) return;
    const a = b.dataset.a, uid = S.activeStory;
    if (a === "export") exportDialog();
    else if (a === "import") importDialog();
    else if (a === "apply-new") act("Застосування сюжету", () => api("story/apply", { uid, mode: "new" })).then((r) => r && toast("У Resolve створено новий таймлайн з монтажем варіанта", "ok"));
    else if (a === "apply-replace") modal("Замінити монтаж?", `<p class="lead">Усі кліпи доріжки <b>${esc(S.view.track)}</b> поточного таймлайна буде замінено сценами варіанта. Інші доріжки не змінюються.</p>${note("Скасувати можна через Ctrl+Z у Resolve.")}`,
      [["Скасувати"], ["Замінити", "primary", () => act("Застосування сюжету", () => api("story/apply", { uid, mode: "replace" }))]]);
    else if (a === "story-more") showMenu(ev.clientX, ev.clientY, [
      ["Завантажити варіант як JSON", "", () => { location.href = "/api/export/story?uid=" + uid; }],
      ["Увімкнути всі сцени", "", () => patchStory(uid, { scenes: findStory(uid).scenes.map((s) => ({ uid: s.uid, enabled: true })) })],
      null, ["Видалити варіант", "", () => patchStory(uid, { delete: true })]]);
    else if (a === "scene-go") { const sc = findScene(uid, b.dataset.sc); if (sc && sc.fragments[0]) goto({ fid: sc.fragments[0] }); }
    else if (a === "scene-ph" || a === "scene-end") act("Вставлення сцени", () => api("scene/apply", { uid, scene: b.dataset.sc, where: a === "scene-ph" ? "playhead" : "end" }));
  });
  box.addEventListener("change", (ev) => {
    const en = ev.target.closest("[data-en]");
    if (en) patchStory(S.activeStory, { scenes: [{ uid: en.dataset.en, enabled: en.checked }] });
    const vo = ev.target.closest("[data-vo]");
    if (vo) patchStory(S.activeStory, { scenes: [{ uid: vo.dataset.vo, voiceover: vo.value }] }, false);
  });
  // drag & drop scene order
  let dragUid = null;
  box.addEventListener("dragstart", (ev) => {
    const sc = ev.target.closest(".scene");
    if (!sc || ev.target.closest("textarea")) return;
    dragUid = sc.dataset.scene; sc.classList.add("dragging");
    ev.dataTransfer.effectAllowed = "move";
    ev.dataTransfer.setData("text/plain", dragUid);
  });
  box.addEventListener("dragover", (ev) => {
    if (!dragUid) return;
    ev.preventDefault();
    $$(".scene.drop-above", box).forEach((e) => e.classList.remove("drop-above"));
    const sc = ev.target.closest(".scene");
    if (sc && sc.dataset.scene !== dragUid) sc.classList.add("drop-above");
  });
  box.addEventListener("drop", (ev) => {
    ev.preventDefault();
    const sc = ev.target.closest(".scene");
    const s = findStory(S.activeStory);
    if (!dragUid || !s) return;
    const order = s.scenes.map((x) => x.uid).filter((u) => u !== dragUid);
    const at = sc ? order.indexOf(sc.dataset.scene) : order.length;
    order.splice(at < 0 ? order.length : at, 0, dragUid);
    dragUid = null;
    patchStory(S.activeStory, { scene_order: order });
  });
  box.addEventListener("dragend", () => { dragUid = null; $$(".dragging,.drop-above", box).forEach((e) => e.classList.remove("dragging", "drop-above")); });
}

const findStory = (uid) => (S.stories || []).find((s) => s.uid === uid);
const findScene = (uid, sc) => (findStory(uid) || { scenes: [] }).scenes.find((x) => x.uid === sc);
function patchStory(uid, patch, rerender = true) {
  return api("story/update", { uid, patch }).then((r) => { S.stories = r.stories; if (rerender) renderStories(); }).catch((e) => toast(e.message, "err"));
}

// ------------------------------------------------------------------ speakers panel

function renderSpeakers() {
  const box = $("#tab-speakers");
  const keys = Object.keys(S.tr);
  if (!keys.length) { box.innerHTML = `<div class="sec hint">Мовці з'являться після транскрибування. Імена можна змінити тут або через ПКМ на імені в тексті.</div>`; return; }
  box.innerHTML = keys.map((k) => {
    const t = S.tr[k];
    const counts = new Array(t.speakers.length).fill(0);
    t.words.forEach((w) => { counts[w[3]] = (counts[w[3]] || 0) + 1; });
    return `<div class="speakers-media"><div class="file" title="${esc(t.path)}">${esc(t.tag)} · ${esc(t.path.split(/[\\/]/).pop())}${t.lang ? " · " + esc(t.lang) : ""}</div>
      ${t.speakers.map((s, i) => `<div class="spk-row"><span class="swatch" style="background:${s.color}"></span>
        <input type="text" value="${esc(s.name)}" data-m="${k}" data-sid="${s.id}"><span class="n">${counts[i] || 0} сл.</span></div>`).join("")}</div>`;
  }).join("");
}

function setupSpeakers() {
  $("#tab-speakers").addEventListener("change", (ev) => {
    const i = ev.target.closest("input[data-sid]");
    if (i) act("Мовець", () => api("speaker/rename", { m: i.dataset.m, sid: i.dataset.sid, name: i.value }));
  });
}

function showTab(name, sync = true) {
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  $$(".tab-body").forEach((b) => b.classList.toggle("hidden", b.id !== "tab-" + name));
  if (sync && S.panels && S.panels.side !== name) { S.panels.side = name; applyPanels(); }
}

// ------------------------------------------------------------------ wiring

function setupTop() {
  $("#btnUndo").onclick = () => edit("Скасування", { op: "undo" });
  $("#btnRedo").onclick = () => edit("Повтор", { op: "redo" });
  $("#btnTranscribe").onclick = transcribeDialog;
  $("#btnExport").onclick = exportDialog;
  $("#btnImport").onclick = importDialog;
  $("#btnRefresh").onclick = () => act("Оновлення", () => api("refresh", {}));
  $("#btnFollow").classList.toggle("on", S.follow);
  $("#btnFollow").onclick = () => { S.follow = !S.follow; $("#btnFollow").classList.toggle("on", S.follow); if (S.follow) showPlayhead(); };
  $("#trackSel").onchange = (e) => act("Доріжка", () => api("track", { track: e.target.value }));
  $("#btnMore").onclick = (ev) => {
    const r = ev.currentTarget.getBoundingClientRect();
    showMenu(r.left - 180, r.bottom + 4, [
      ["Субтитри монтажу (.srt)", "", () => { location.href = "/api/export/srt"; }],
      ["Текст монтажу (.txt)", "", () => { location.href = "/api/export/txt"; }],
      ["Схема відповіді Claude (.json)", "", () => { location.href = "/api/export/schema"; }],
      null,
      ["Прибрати розриви на доріжці", "", () => edit("Розриви", { op: "close_gaps" })],
      ["Компоненти й моделі…", "", componentsDialog],
      ["Перемкнути таймлайн…", "", timelineDialog],
    ]);
  };
  $$(".tab").forEach((t) => (t.onclick = () => showTab(t.dataset.tab)));
  $("#tglOutline").onclick = () => { S.panels.outline = !S.panels.outline; applyPanels(); };
  $$("[data-side]").forEach((b) => (b.onclick = () => {
    S.panels.side = S.panels.side === b.dataset.side ? null : b.dataset.side;  // the active switch hides the panel
    applyPanels();
  }));
  applyPanels();
  $("#searchInput").addEventListener("input", () => { S.hitIdx = 0; runSearch(true); });
  $("#searchInput").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); stepHit(e.shiftKey ? -1 : 1); } });
  $("#searchCut").onchange = () => runSearch(true);
  $("#searchPrev").onclick = () => stepHit(-1);
  $("#searchNext").onclick = () => stepHit(1);
  $("#statusJob").addEventListener("click", (e) => { const c = e.target.closest("[data-cancel]"); if (c) api("cancel", { id: c.dataset.cancel }); });
  $("#modalBack").addEventListener("mousedown", (e) => { if (e.target.id === "modalBack") closeModal(); });
}

async function componentsDialog() {
  let st;
  try { st = await api("components?disk=1"); } catch (e) { return toast(e.message, "err"); }
  const cap = (t) => t.charAt(0).toUpperCase() + t.slice(1);
  const row = (k) => {
    const c = st[k];
    if (k === "gpu" && !c.local) return "";  // obsolete: shown only while its folder is still there
    const state = c.installed ? `<span class="ds ok">встановлено</span>` : `<span class="ds">${esc(c.size)}</span>`;
    const btn = c.installed
      ? (c.local ? `<button class="btn sm ghost" data-comp-rm="${k}">Видалити</button>` : "<span></span>")
      : `<button class="btn sm" data-comp-get="${k}">Завантажити</button>`;
    return `<div class="drow"><span><div class="dn">${esc(cap(c.label))}</div>${c.disk ? `<div class="dd">на диску ${esc(c.disk)}</div>` : ""}</span>${state}${btn}</div>`;
  };
  const models = st.models.map((m) => `<div class="drow"><span><div class="dn">${esc(m.label || m.name)}</div><div class="dd">${esc(m.size || "")}</div></span>
      ${m.available ? `<span class="ds ok">завантажено</span><span></span>` : `<span class="ds">не завантажено</span><button class="btn sm" data-model-get="${esc(m.name)}">Завантажити</button>`}</div>`).join("");
  const credits = st.models.filter((m) => m.credit).map((m) =>
    `${esc(m.label)}: ${esc(m.credit)}. Ліцензія <a href="${esc(m.license)}" target="_blank" rel="noopener">CC BY 4.0</a>, модель використовується без змін.`).join("<br>");
  modal("Компоненти й моделі", `
    <p class="lead">Програма завантажує їх сама, коли вони вперше потрібні. Тут можна зробити це заздалегідь; перерване завантаження продовжиться з місця зупинки.</p>
    <div class="fsec"><div class="fsec-h">Компоненти</div><div class="dlist">${["whisper", "speakers", "gpu"].map(row).join("")}</div></div>
    <div class="fsec"><div class="fsec-h">Модель розпізнавання</div><div class="dlist">${models}</div></div>
    ${credits ? note(credits) : ""}`, [["Закрити", "primary"]]);
  const job = (path, body) => api(path, body).then(() => { closeModal(); toast("Завантаження у фоні — прогрес у рядку стану внизу."); })
    .catch((e) => toast(e.message, "err"));
  $$("[data-comp-get]").forEach((b) => (b.onclick = () => job("components/install", { name: b.dataset.compGet })));
  $$("[data-model-get]").forEach((b) => (b.onclick = () => job("models/download", { name: b.dataset.modelGet })));
  $$("[data-comp-rm]").forEach((b) => (b.onclick = () => api("components/remove", { name: b.dataset.compRm })
    .then((r) => { toast(r.removed === "later" ? "Буде видалено після перезапуску програми." : "Видалено.", "ok"); closeModal(); })
    .catch((e) => toast(e.message, "err"))));
}

async function timelineDialog() {
  try {
    await api("timelines", {});
    await new Promise((r) => setTimeout(r, 400));
    const st = await api("state");
    const list = (st.link.info || {}).timelines || [];
    const cur = st.link.info && st.link.info.timeline;
    modal("Таймлайн", list.length
      ? `<p class="lead">Виберіть таймлайн проєкту, з яким працювати.</p><div class="dlist">${list.map((t) =>
          `<div class="drow click ${cur && t.name === cur ? "cur" : ""}" data-tlid="${esc(t.id)}"><span class="dn">${esc(t.name)}</span><span class="ds">${cur && t.name === cur ? "поточний" : ""}</span></div>`).join("")}</div>`
      : note("У проєкті немає таймлайнів."), [["Закрити"]], "narrow");
    $$("[data-tlid]").forEach((el) => (el.onclick = () => { closeModal(); act("Перемикання", () => api("set_timeline", { id: el.dataset.tlid })); }));
  } catch (e) { toast(e.message, "err"); }
}

// an interface error must never leave the text silently out of date
window.addEventListener("error", (e) => toast("Помилка інтерфейсу: " + e.message, "err"));
window.addEventListener("unhandledrejection", (e) => toast("Помилка: " + ((e.reason && e.reason.message) || e.reason), "err"));

setupTop();
setupEditor();
setupKeys();
setupStories();
setupSpeakers();
api("state").then((st) => { S.settings = st.settings; }).catch(() => {});
connectEvents();
// checked once in the background, so «Транскрибувати» opens at once
setTimeout(() => api("components").then((c) => { S.comp = c; }).catch(() => {}), 1500);
