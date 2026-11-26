/* DaVinci Director — front-end (vanilla JS, no build step) */
"use strict";

// =====================================================================
// state & utils
// =====================================================================
const S = {
  settings: {}, resolve: { ok: false }, project: null, history: null,
  render: { segments: [], duration: 0 }, jobs: {},
  clipboard: null, caret: null, focusPid: null,
  search: { q: "", hits: [], idx: -1 },
  storyAn: null, storyVar: null,
  chatStream: null, chatThinking: "",
  playMode: true, segIdx: -1, curClip: null, curToken: null, resolveToken: null,
};
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const P = () => S.project;
const T = () => (S.project ? S.project.tokens : []);

function fmt(sec, short = false) {
  sec = Math.max(0, +sec || 0);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  if (short) return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(Math.floor(s)).padStart(2, "0");
  return String(h).padStart(2, "0") + ":" + String(m).padStart(2, "0") + ":" + s.toFixed(1).padStart(4, "0");
}
function parseTs(v) {
  if (typeof v === "number") return v;
  const parts = String(v).trim().replace(",", ".").split(":").map(Number);
  if (parts.some(isNaN)) throw new Error("bad timestamp");
  return parts.reduce((a, b) => a * 60 + b, 0);
}
const ROLE_COLORS = { hook: "#e0564f", setup: "#4a8fd6", development: "#3fb3c4", escalation: "#d5b23e", climax: "#c958c9",
  resolution: "#5bae66", outro: "#9a8fd8", context: "#6fb6e6", callback: "#e389b4", montage: "#7fd1a8", other: "#a89b7f" };
const ROLE_UA = { hook: "Hook", setup: "Setup", development: "Розвиток", escalation: "Наростання", climax: "Кульмінація",
  resolution: "Розв'язка", outro: "Аутро", context: "Контекст", callback: "Callback", montage: "Монтаж-нарізка", other: "Інше" };
const MOMENT_COLORS = { funny: "#d5b23e", unexpected: "#c958c9", tense: "#e0564f", emotional: "#e389b4", epic: "#e2833a",
  fail: "#b06a5a", wholesome: "#5bae66", skill: "#4a8fd6", drama: "#9a8fd8", other: "#888" };
const MOMENT_UA = { funny: "смішно", unexpected: "несподівано", tense: "напруга", emotional: "емоції", epic: "епік", fail: "фейл",
  wholesome: "мило", skill: "скіл", drama: "драма", other: "інше" };

async function api(path, body, method) {
  const opts = { method: method || (body === undefined ? "GET" : "POST"), headers: {} };
  if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  const r = await fetch(path, opts);
  let data = {};
  try { data = await r.json(); } catch (e) { /* empty */ }
  if (!r.ok) {
    const err = new Error(data.error || ("HTTP " + r.status));
    err.resolve = data.resolve;
    throw err;
  }
  return data;
}
async function act(fn, okMsg) {
  try { const r = await fn(); if (okMsg) toast(okMsg, "ok"); return r; }
  catch (e) { toast(e.message, "error"); throw e; }
}
function toast(text, level = "info", ms = 4200) {
  const el = document.createElement("div");
  el.className = "toast " + level;
  el.textContent = text;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), level === "error" ? ms * 2 : ms);
}
function status(text) { $("#status-msg").textContent = text || ""; }

// =====================================================================
// boot & server events
// =====================================================================
async function boot() {
  bindUI();
  try {
    const st = await api("/api/state");
    applyState(st);
    if (!st.project && st.resolve.ok) await connect(true);
  } catch (e) { toast(e.message, "error"); }
  const es = new EventSource("/api/events");
  es.onmessage = (ev) => onEvent(JSON.parse(ev.data));
  es.onerror = () => setResolveLed(false, "Немає зв'язку з аддоном");
  setInterval(pollPlayhead, 700);
  setInterval(refreshResolveState, 8000);
}

function applyState(st) {
  S.settings = st.settings || S.settings;
  S.resolve = st.resolve || S.resolve;
  if ("project" in st) setProject(st.project);
  if (st.history) S.history = st.history;
  if (st.render) S.render = st.render;
  (st.jobs || []).forEach((j) => (S.jobs[j.id] = j));
  $("#chk-autosync").checked = !!S.settings.auto_sync;
  document.documentElement.style.setProperty("--fs", (S.settings.ui_font_size || 13) + "px");
  renderAll();
}

function setProject(p) {
  S.project = p;
  if (!p) return;
  // per-clip token index for viewer lookups
  S.clipTokens = {};
  p.tokens.forEach((t) => (S.clipTokens[t.c] = S.clipTokens[t.c] || []).push(t));
  if (!S.storyAn && p.analyses && p.analyses.length) S.storyAn = p.analyses[0].id;
}

function onEvent(ev) {
  switch (ev.type) {
    case "job": onJob(ev.job); break;
    case "project":
      if (ev.project) setProject(ev.project);
      else if (S.project && ev.edit) S.project.edit = ev.edit;
      if (ev.history) S.history = ev.history;
      if (ev.render) S.render = ev.render;
      if (ev.project) renderAll(); else { renderEditor(); renderOutline(); renderOverview(); renderToolbar(); renderStories(); }
      break;
    case "toast": toast(ev.text, ev.level); break;
    case "sync":
      if (ev.status === "done") { toast("Таймлайн «" + ev.result.timeline + "» оновлено: " + ev.result.clips + " кліпів, " + ev.result.markers + " маркерів", "ok"); }
      renderToolbar();
      break;
    case "ai_thinking": {
      const el = $("#analyze-thinking");
      if (el) { el.textContent += ev.delta; el.scrollTop = el.scrollHeight; }
      break;
    }
    case "chat_delta":
      if (!S.chatStream) S.chatStream = { text: "", thinking: "" };
      if (ev.part === "text") S.chatStream.text += ev.delta; else S.chatStream.thinking += ev.delta;
      renderChatStream();
      break;
    case "analysis":
      S.storyAn = ev.analysis; S.storyVar = null;
      switchTab("right", "stories");
      renderStories(); renderMoments();
      break;
    case "proxy": loadClip(S.curClip ?? 0, true); break;
  }
}

function onJob(j) {
  S.jobs[j.id] = j;
  renderJobs();
  if (j.status !== "running") {
    setTimeout(() => { delete S.jobs[j.id]; renderJobs(); }, 2500);
    if (j.status === "error") toast(j.label + ": " + j.message, "error");
    else if (j.status === "done" && j.message) toast(j.label + ": " + j.message, "ok");
    if (j.kind === "chat") { S.chatStream = null; renderChat(); }
    if (j.kind === "analyze") closeModal();
    if (j.kind === "transcribe" && j.status === "done") { closeModal(); refreshState(); }
  }
  const pb = $("#analyze-progress");
  if (pb && j.kind === "analyze") { pb.style.width = (j.progress * 100).toFixed(1) + "%"; $("#analyze-msg").textContent = j.message; }
  const tb = $("#tr-progress");
  if (tb && j.kind === "transcribe") { tb.style.width = (j.progress * 100).toFixed(1) + "%"; $("#tr-msg").textContent = j.message; }
}

async function refreshState() {
  try { applyState(await api("/api/state")); } catch (e) { toast(e.message, "error"); }
}
async function refreshResolveState() {
  try {
    const st = await api("/api/state");
    S.resolve = st.resolve; renderResolveState();
  } catch (e) { setResolveLed(false, "Аддон недоступний"); }
}
async function connect(silent) {
  try {
    const r = await api("/api/connect", {});
    applyState(r.state);
    if (r.timeline_changed) toast("Таймлайн змінився після транскрибування. Відкрийте «Транскрибувати» → «Перечитати кліпи», якщо потрібно.", "info", 8000);
    if (!silent) toast("Підключено: " + (P().resolve.timeline || ""), "ok");
    loadClip(0);
  } catch (e) { if (!silent) toast(e.message, "error"); else status(e.message); }
}

// =====================================================================
// rendering: top-level
// =====================================================================
function renderAll() {
  renderCrumbs(); renderToolbar(); renderResolveState(); renderEditor(true); renderSpeakers(); renderOutline();
  renderStories(); renderMoments(); renderChat(); renderOverview(); renderJobs(); renderSearch();
  if (P() && S.curClip === null) loadClip(0);
}
function renderCrumbs() {
  const p = P();
  $("#crumbs").innerHTML = p ? `<span>${esc(p.resolve.project || "")}</span> ▸ <b>${esc(p.resolve.timeline || "")}</b> <span class="dim small">${p.resolve.fps || ""} fps · ${p.clips.length} кліп(ів)</span>`
    : `<span class="dim">Не підключено до таймлайну</span>`;
}
function renderToolbar() {
  const has = !!(P() && P().tokens.length);
  $("#btn-undo").disabled = !(S.history && S.history.undo);
  $("#btn-redo").disabled = !(S.history && S.history.redo);
  $("#btn-undo").title = "Скасувати (Ctrl+Z)" + (S.history && S.history.undo_label ? ": " + S.history.undo_label : "");
  $("#btn-sync").disabled = !has;
  $("#btn-analyze").disabled = !has;
  const dirty = has && P().edit.dirty;
  $("#btn-sync").classList.toggle("attn", !!dirty);
  const ss = $("#sync-state");
  if (has && P().edit.synced_timeline) {
    ss.innerHTML = `<span class="led ${dirty ? "warn" : "ok"}"></span>${esc(P().edit.synced_timeline)}${dirty ? " — є незастосовані зміни" : ""}`;
  } else ss.innerHTML = has ? `<span class="led"></span>Монтажний таймлайн ще не створено` : "";
}
function setResolveLed(ok, text) {
  $("#resolve-state").innerHTML = `<span class="led ${ok ? "ok" : "bad"}"></span>${esc(text)}`;
}
function renderResolveState() {
  const r = S.resolve || {};
  if (r.ok) {
    const i = r.info || {};
    setResolveLed(true, (i.product || "DaVinci Resolve") + " " + (i.version || "") + (r.kind === "bridge" ? " · міст" : r.kind === "direct" ? " · API" : ""));
  } else setResolveLed(false, "Resolve недоступний");
}
function renderJobs() {
  const box = $("#jobs");
  box.innerHTML = "";
  Object.values(S.jobs).forEach((j) => {
    const el = document.createElement("div");
    el.className = "job";
    el.innerHTML = `<span>${esc(j.label)}${j.message ? ": " + esc(j.message) : ""}</span><div class="bar"><i style="width:${(j.progress * 100).toFixed(1)}%"></i></div>` +
      (j.status === "running" ? `<button title="Скасувати">✕</button>` : `<span>${j.status === "done" ? "✓" : j.status === "error" ? "⚠" : ""}</span>`);
    const b = el.querySelector("button");
    if (b) b.onclick = () => api("/api/cancel", { job: j.id });
    box.appendChild(el);
  });
}

// =====================================================================
// transcript editor
// =====================================================================
const pieceEls = new Map(); // pid -> {el, sig}

function speakerOf(p) {
  const counts = {};
  const toks = T();
  for (let i = p.a; i <= p.b; i++) { const sp = toks[i].sp; if (sp) counts[sp] = (counts[sp] || 0) + 1; }
  let best = null, n = 0;
  for (const k in counts) if (counts[k] > n) { best = k; n = counts[k]; }
  return best;
}
function sceneOf(p) {
  if (!p.scene) return null;
  for (const an of P().analyses) for (const v of an.variants) for (const s of v.scenes) if (s.id === p.scene) return s;
  return null;
}
function pieceDuration(p) {
  if (p.off) return 0;
  const cut = new Set(p.cut), toks = T();
  let d = 0;
  for (let i = p.a; i <= p.b; i++) if (!cut.has(i)) d += toks[i].t1 - toks[i].t0;
  return d;
}
function pieceSig(p) {
  return [p.a, p.b, p.off, p.label, p.scene, p.cut.length, p.cut[0], p.cut[p.cut.length - 1], p.dup, S.project._wordsVer || 0, speakerOf(p)].join("|");
}
function pieceHTML(p) {
  const toks = T(), cut = new Set(p.cut), speakers = P().speakers;
  const sp = speakerOf(p), spk = speakers[sp];
  const sc = sceneOf(p);
  let html = `<div class="phead"><span class="handle" draggable="true" title="Перетягніть, щоб переставити">⋮⋮</span>` +
    `<span class="ptime">${fmt(toks[p.a].r0).slice(0, 10)}</span><span class="pdur">${pieceDuration(p).toFixed(1)}s</span>` +
    (spk ? `<span class="spk" data-spk="${esc(sp)}"><i class="dot" style="background:${spk.color}"></i>${esc(spk.name)}</span>` : "") + `</div>`;
  html += `<div class="ptools"><button data-pt="dup" title="Дублювати (Ctrl+D)">⧉</button><button data-pt="up" title="Вгору (Alt+↑)">▴</button><button data-pt="down" title="Вниз (Alt+↓)">▾</button>` +
    `<button data-pt="${p.off ? "restore" : "del"}" title="${p.off ? "Відновити фрагмент" : "Видалити фрагмент"}">${p.off ? "↺" : "✕"}</button></div>`;
  if (sc || p.label || p.dup) {
    html += `<div class="ptop">`;
    if (sc) html += `<span class="scene-tag" style="border-left-color:${ROLE_COLORS[sc.role] || "#888"}">${esc((ROLE_UA[sc.role] || sc.role))} · ${esc(sc.title)}</span>`;
    else if (p.label) html += `<span class="scene-tag">${esc(p.label)}</span>`;
    if (p.dup) html += `<span class="dup-tag">копія</span>`;
    html += `</div>`;
  }
  const parts = [];
  for (let i = p.a; i <= p.b; i++) {
    const t = toks[i];
    let cls = t.k === "g" ? "g" : "w";
    if (cut.has(i)) cls += " x";
    if (t.hl >= 2) cls += " l" + t.hl;
    if (t.k === "w" && t.p < 0.5) cls += " lc";
    if (t.k === "g") parts.push(`<span class="${cls}" data-i="${i}">${t.hl >= 2 ? "⚡ " : ""}··· ${(t.t1 - t.t0).toFixed(1)}s</span>`);
    else parts.push(`<span class="${cls}" data-i="${i}">${esc(t.w)}</span>`);
  }
  html += `<div class="ptext">${parts.join(" ")}</div>`;
  return html;
}

function renderEditor(full) {
  const ed = $("#editor");
  const p = P();
  const has = !!(p && p.tokens.length);
  $("#editor-empty").classList.toggle("hidden", has);
  if (!p) $("#empty-text").textContent = "Не вдалося підключитися до таймлайну. Відкрийте таймлайн з відео в DaVinci Resolve і натисніть «⟲ Таймлайн».";
  else if (!has) $("#empty-text").innerHTML = `Таймлайн <b>${esc(p.resolve.timeline)}</b>: ${p.clips.length} кліп(ів). Запустіть транскрибування, щоб почати текстовий монтаж.`;
  if (!has) { ed.innerHTML = ""; pieceEls.clear(); return; }
  if (full) { ed.innerHTML = ""; pieceEls.clear(); }
  const seen = new Set();
  let prev = null;
  for (const pc of p.edit.pieces) {
    seen.add(pc.id);
    const sig = pieceSig(pc);
    let rec = pieceEls.get(pc.id);
    if (!rec) {
      const el = document.createElement("div");
      el.className = "piece";
      el.dataset.pid = pc.id;
      rec = { el, sig: null };
      pieceEls.set(pc.id, rec);
    }
    if (rec.sig !== sig) { rec.el.innerHTML = pieceHTML(pc); rec.sig = sig; }
    rec.el.classList.toggle("off", !!pc.off);
    rec.el.classList.toggle("focus", pc.id === S.focusPid);
    const want = prev ? prev.nextSibling : ed.firstChild;
    if (want !== rec.el) ed.insertBefore(rec.el, want);
    prev = rec.el;
  }
  for (const [pid, rec] of pieceEls) if (!seen.has(pid)) { rec.el.remove(); pieceEls.delete(pid); }
  applySearchHighlight();
  markToken(S.curToken, "cur");
  markToken(S.resolveToken, "rcur");
}
function invalidatePieces() { for (const rec of pieceEls.values()) rec.sig = null; }

function tokenEls(i) { return $$(`#editor [data-i="${i}"]`); }
function markToken(i, cls) {
  $$("#editor ." + cls).forEach((e) => e.classList.remove(cls));
  if (i === null || i === undefined) return [];
  const els = tokenEls(i);
  els.forEach((e) => e.classList.add(cls));
  return els;
}
function scrollToToken(i, pid) {
  let el = null;
  if (pid) el = $(`#editor .piece[data-pid="${pid}"] [data-i="${i}"]`);
  if (!el) el = tokenEls(i)[0];
  if (!el) return;
  const ed = $("#editor"), r = el.getBoundingClientRect(), er = ed.getBoundingClientRect();
  if (r.top < er.top + 40 || r.bottom > er.bottom - 60) ed.scrollTop += r.top - er.top - er.height / 3;
}

// ---- selection → [{pid,a,b}]
function getSelection_() {
  const sel = window.getSelection();
  if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return null;
  const range = sel.getRangeAt(0);
  if (!$("#editor").contains(range.commonAncestorContainer)) return null;
  const findTok = (node, forward) => {
    let el = node.nodeType === 1 ? node : node.parentElement;
    let tok = el && el.closest("[data-i]");
    if (tok) return tok;
    // between tokens: walk to nearest token
    const pieceEl = el && el.closest(".piece");
    if (!pieceEl) return null;
    const toks = $$("[data-i]", pieceEl);
    if (!toks.length) return null;
    return forward ? toks[0] : toks[toks.length - 1];
  };
  let startEl = findTok(range.startContainer, true);
  let endEl = findTok(range.endContainer, false);
  if (!startEl || !endEl) return null;
  // a selection ending at offset 0 of a token does not include it
  if (range.endContainer.nodeType === 3 && range.endOffset === 0 && endEl !== startEl) {
    const prevTok = endEl.previousElementSibling;
    if (prevTok && prevTok.dataset.i) endEl = prevTok;
  }
  const sp = startEl.closest(".piece").dataset.pid, ep = endEl.closest(".piece").dataset.pid;
  const pieces = P().edit.pieces;
  const si = pieces.findIndex((x) => x.id === sp), ei = pieces.findIndex((x) => x.id === ep);
  const out = [];
  for (let n = si; n <= ei; n++) {
    const pc = pieces[n];
    const a = n === si ? +startEl.dataset.i : pc.a;
    const b = n === ei ? +endEl.dataset.i : pc.b;
    if (a <= b) out.push({ pid: pc.id, a, b });
  }
  return out.length ? out : null;
}
function selOrCaret() {
  const s = getSelection_();
  if (s) return s;
  if (S.caret) return [{ pid: S.caret.pid, a: S.caret.i, b: S.caret.i }];
  return null;
}
function selIsCut(sel) {
  for (const s of sel) {
    const pc = P().edit.pieces.find((x) => x.id === s.pid);
    const cut = new Set(pc.cut);
    for (let i = s.a; i <= s.b; i++) if (!cut.has(i)) return false;
  }
  return true;
}
function selText(sel) {
  const toks = T();
  return sel.map((s) => { const out = []; for (let i = s.a; i <= s.b; i++) if (toks[i].k === "w") out.push(toks[i].w); return out.join(" "); }).join("\n");
}
function clearSel() { const s = window.getSelection(); if (s) s.removeAllRanges(); }

// ---- edit operations
async function editOp(op, args, msg) {
  try {
    const r = await api("/api/edit", { op, args });
    if (P()) P().edit = r.edit;
    S.history = r.history; S.render = r.render;
    renderEditor(); renderOutline(); renderOverview(); renderToolbar();
    if (msg) status(msg);
    return r.result;
  } catch (e) { toast(e.message, "error"); }
}
async function undo() {
  const r = await act(() => api("/api/undo", {}));
  if (r && r.project) { setProject(r.project); S.history = r.history; S.render = r.render; invalidatePieces(); renderAll(); status(r.label ? "Скасовано: " + r.label : ""); }
}
async function redo() {
  const r = await act(() => api("/api/redo", {}));
  if (r && r.project) { setProject(r.project); S.history = r.history; S.render = r.render; invalidatePieces(); renderAll(); status(r.label ? "Повторено: " + r.label : ""); }
}

function cmdDelete() {
  const sel = selOrCaret();
  if (!sel) return;
  if (selIsCut(sel)) editOp("restore", { sel }, "Відновлено");
  else editOp("cut", { sel }, "Вирізано");
  clearSel();
}
function cmdCopy(cut) {
  const sel = getSelection_() || (S.focusPid ? [{ pid: S.focusPid, a: pieceById(S.focusPid).a, b: pieceById(S.focusPid).b }] : null);
  if (!sel) return;
  S.clipboard = sel.map((s) => {
    const pc = pieceById(s.pid);
    return { a: s.a, b: s.b, cut: pc.cut.filter((i) => i >= s.a && i <= s.b) };
  });
  try { navigator.clipboard.writeText(selText(sel)); } catch (e) { /* ignore */ }
  status("Скопійовано " + S.clipboard.length + " фрагмент(и) — Ctrl+V для вставки");
  if (cut) editOp("cut", { sel }, "Вирізано в буфер");
}
function cmdPaste(where) {
  if (!S.clipboard) { toast("Буфер порожній: виділіть текст і натисніть Ctrl+C"); return; }
  let after = null, at = null;
  if (S.caret && where !== "after") { after = S.caret.pid; at = S.caret.i; }
  else if (S.caret) after = S.caret.pid;
  else if (S.focusPid) after = S.focusPid;
  editOp("paste", { items: S.clipboard, after, at }, "Вставлено");
}
function cmdDuplicate() {
  const sel = getSelection_();
  if (sel) editOp("duplicate", { sel }, "Дубльовано виділення");
  else if (S.focusPid) editOp("duplicate", { pids: [S.focusPid] }, "Фрагмент дубльовано");
}
function cmdSplit() {
  if (!S.caret) return toast("Клікніть на слово, з якого почати новий фрагмент");
  editOp("split", { pid: S.caret.pid, at: S.caret.i }, "Фрагмент розділено");
}
function cmdMove(delta) {
  const sel = getSelection_();
  const pids = sel ? [...new Set(sel.map((s) => s.pid))] : S.focusPid ? [S.focusPid] : [];
  if (pids.length) editOp("move_step", { pids, delta });
}
function pieceById(pid) { return P().edit.pieces.find((x) => x.id === pid); }
function setFocusPiece(pid) {
  S.focusPid = pid;
  $$("#editor .piece.focus").forEach((e) => e.classList.remove("focus"));
  const r = pieceEls.get(pid);
  if (r) r.el.classList.add("focus");
}

// ---- inline word editing
function editWord(el) {
  const i = +el.dataset.i;
  const t = T()[i];
  if (t.k !== "w") return;
  const inp = $("#word-editor");
  const r = el.getBoundingClientRect();
  inp.value = t.w;
  inp.style.left = r.left - 4 + "px";
  inp.style.top = r.top - 3 + "px";
  inp.style.width = Math.max(80, r.width + 40) + "px";
  inp.classList.remove("hidden");
  inp.focus(); inp.select();
  const done = async (save) => {
    inp.classList.add("hidden");
    inp.onkeydown = inp.onblur = null;
    const val = inp.value.trim();
    if (!save || !val || val === t.w) return;
    // allow splitting into several words? keep one token: join spaces
    await act(() => api("/api/words", { changes: [{ i, w: val }] }));
    t.w = val; S.project._wordsVer = (S.project._wordsVer || 0) + 1;
    renderEditor();
  };
  inp.onkeydown = (e) => { if (e.key === "Enter") done(true); if (e.key === "Escape") done(false); e.stopPropagation(); };
  inp.onblur = () => done(true);
}

// =====================================================================
// context menu
// =====================================================================
function showMenu(x, y, items) {
  const m = $("#ctxmenu");
  const build = (list) => list.map((it) => {
    if (it === "-") return "<hr>";
    if (it.sub) return `<div class="mi has-sub">${esc(it.label)}<div class="sub">${build(it.sub)}</div></div>`;
    return `<div class="mi${it.disabled ? " disabled" : ""}" data-k="${S._menuFns.push(it.fn) - 1}"><span>${esc(it.label)}</span><span class="k">${esc(it.key || "")}</span></div>`;
  }).join("");
  S._menuFns = [];
  m.innerHTML = build(items);
  m.classList.remove("hidden");
  const w = m.offsetWidth, h = m.offsetHeight;
  m.style.left = Math.min(x, innerWidth - w - 4) + "px";
  m.style.top = Math.min(y, innerHeight - h - 4) + "px";
}
function hideMenu() { $("#ctxmenu").classList.add("hidden"); }

function editorMenu(e) {
  const tokEl = e.target.closest("[data-i]");
  const pieceEl = e.target.closest(".piece");
  if (!pieceEl) return;
  e.preventDefault();
  const pid = pieceEl.dataset.pid;
  setFocusPiece(pid);
  let sel = getSelection_();
  if (!sel && tokEl) { S.caret = { pid, i: +tokEl.dataset.i }; markToken(S.caret.i, "caret"); }
  sel = sel || (tokEl ? [{ pid, a: +tokEl.dataset.i, b: +tokEl.dataset.i }] : null);
  const pc = pieceById(pid);
  const speakers = P().speakers;
  const spItems = Object.entries(speakers).map(([id, s]) => ({ label: s.name, fn: () => assignSpeaker(sel || [{ pid, a: pc.a, b: pc.b }], id) }));
  spItems.push("-", { label: "Новий мовець…", fn: () => { const n = prompt("Ім'я мовця"); if (n) assignSpeaker(sel || [{ pid, a: pc.a, b: pc.b }], null, n); } });
  const tok = tokEl ? +tokEl.dataset.i : pc.a;
  showMenu(e.clientX, e.clientY, [
    { label: sel && selIsCut(sel) ? "Відновити виділене" : "Вирізати виділене", key: "Del", disabled: !sel, fn: cmdDelete },
    { label: "Копіювати", key: "Ctrl+C", fn: () => cmdCopy(false) },
    { label: "Вирізати в буфер (перемістити)", key: "Ctrl+X", disabled: !sel, fn: () => cmdCopy(true) },
    { label: "Вставити перед словом", key: "Ctrl+V", disabled: !S.clipboard || !tokEl, fn: () => cmdPaste("at") },
    { label: "Вставити після фрагмента", disabled: !S.clipboard, fn: () => { S.caret = { pid, i: pc.b }; cmdPaste("after"); } },
    { label: "Дублювати", key: "Ctrl+D", fn: cmdDuplicate },
    "-",
    { label: "Розділити фрагмент тут", key: "S", disabled: !tokEl || tok === pc.a, fn: () => editOp("split", { pid, at: tok }) },
    { label: "Об'єднати з наступним", fn: () => { const ps = P().edit.pieces; const n = ps.findIndex((x) => x.id === pid); if (ps[n + 1]) editOp("merge", { pids: [pid, ps[n + 1].id] }); } },
    { label: "Перемістити вгору", key: "Alt+↑", fn: () => editOp("move_step", { pids: [pid], delta: -1 }) },
    { label: "Перемістити вниз", key: "Alt+↓", fn: () => editOp("move_step", { pids: [pid], delta: 1 }) },
    pc.off ? { label: "Відновити фрагмент", fn: () => editOp("restore_pieces", { pids: [pid] }) }
      : { label: "Видалити фрагмент", fn: () => editOp("delete_pieces", { pids: [pid] }) },
    { label: "Мітка фрагмента…", fn: () => { const l = prompt("Мітка фрагмента (стане маркером на таймлайні)", pc.label || ""); if (l !== null) editOp("set_label", { pid, label: l }); } },
    "-",
    { label: "Призначити мовця", sub: spItems },
    { label: "Редагувати слово", key: "Подв. клік", disabled: !tokEl || T()[tok].k !== "w", fn: () => editWord(tokEl) },
    { label: "Додати як сцену в сюжет", disabled: !currentVariant(), fn: () => addSceneFromSel(sel || [{ pid, a: pc.a, b: pc.b }]) },
    "-",
    { label: "Перейти в Resolve (оригінал)", fn: () => jumpResolve(tok, "source") },
    { label: "Перейти в Resolve (монтаж)", disabled: !P().edit.synced_timeline_id, fn: () => jumpResolve(tok, "edit", pid) },
  ]);
}
async function assignSpeaker(sel, id, name) {
  await act(() => api("/api/speakers", { action: "assign", sel, id, name }));
}

// =====================================================================
// speakers, outline, search
// =====================================================================
function renderSpeakers() {
  const box = $("#speakers");
  const p = P();
  if (!p || !p.tokens.length) { box.innerHTML = `<div class="pad dim">Мовці з'являться після транскрибування.</div>`; return; }
  const counts = {};
  p.tokens.forEach((t) => { if (t.k === "w" && t.sp) counts[t.sp] = (counts[t.sp] || 0) + 1; });
  box.innerHTML = Object.entries(p.speakers).map(([id, s]) =>
    `<div class="row" data-spk="${esc(id)}"><input type="color" value="${s.color}" class="spk-color" style="width:16px;height:14px;border:none;padding:0;background:none" title="Колір"><span class="spk-name">${esc(s.name)}</span><span class="dim small">${counts[id] || 0} сл.</span></div>`).join("");
  $$(".row", box).forEach((row) => {
    const id = row.dataset.spk;
    row.querySelector(".spk-color").onchange = (e) => act(() => api("/api/speakers", { action: "rename", id, color: e.target.value }));
    row.ondblclick = () => {
      const nameEl = row.querySelector(".spk-name");
      nameEl.innerHTML = `<input class="inp sm" value="${esc(p.speakers[id].name)}">`;
      const inp = nameEl.querySelector("input");
      inp.focus(); inp.select();
      const save = () => act(() => api("/api/speakers", { action: "rename", id, name: inp.value }));
      inp.onkeydown = (e) => { if (e.key === "Enter") save(); if (e.key === "Escape") renderSpeakers(); e.stopPropagation(); };
      inp.onblur = save;
    };
  });
}
function renderOutline() {
  const box = $("#outline");
  const p = P();
  if (!p || !p.tokens.length) { box.innerHTML = ""; return; }
  const toks = p.tokens;
  box.innerHTML = p.edit.pieces.map((pc) => {
    const sc = sceneOf(pc);
    const words = [];
    for (let i = pc.a; i <= pc.b && words.length < 14; i++) if (toks[i].k === "w") words.push(toks[i].w);
    const label = sc ? `<b style="color:${ROLE_COLORS[sc.role]}">${esc(sc.title)}</b> ` : pc.label ? `<b>${esc(pc.label)}</b> ` : "";
    return `<div class="ol${pc.off ? " off" : ""}" data-pid="${pc.id}"><span class="tc">${fmt(toks[pc.a].r0, true)}</span><span class="t">${label}${esc(words.join(" ") || "[пауза]")}</span><span class="tc">${pieceDuration(pc).toFixed(0)}s</span></div>`;
  }).join("");
}
function normWord(s) { return s.toLowerCase().replace(/[.,!?…:;"«»“”()\-–—']/g, ""); }
function runSearch(q) {
  S.search.q = q;
  S.search.hits = [];
  S.search.idx = -1;
  const p = P();
  if (p && q.trim()) {
    const terms = q.trim().split(/\s+/).map(normWord).filter(Boolean);
    const toks = p.tokens;
    const words = [];
    toks.forEach((t) => { if (t.k === "w") words.push(t); });
    for (let n = 0; n + terms.length <= words.length; n++) {
      let ok = true;
      for (let k = 0; k < terms.length; k++) {
        const w = normWord(words[n + k].w);
        if (k === terms.length - 1 ? !w.startsWith(terms[k]) : w !== terms[k]) { ok = false; break; }
        if (terms.length === 1 && !w.includes(terms[0])) ok = false;
      }
      if (terms.length === 1) ok = normWord(words[n].w).includes(terms[0]);
      if (ok) S.search.hits.push(words.slice(n, n + terms.length).map((t) => t.i));
    }
  }
  renderSearch();
  applySearchHighlight();
}
function renderSearch() {
  const hits = S.search.hits;
  $("#search-count").textContent = S.search.q ? (hits.length ? (S.search.idx + 1) + "/" + hits.length : "0") : "";
  $("#search-summary").textContent = S.search.q ? `Знайдено: ${hits.length}` : "Пошук по всьому запису";
  $("#btn-cut-matches").disabled = !hits.length;
  $("#btn-restore-matches").disabled = !hits.length;
  const toks = T();
  $("#search-results").innerHTML = hits.slice(0, 500).map((h, n) => {
    const a = h[0], b = h[h.length - 1];
    const ctx = (from, to) => { const o = []; for (let i = Math.max(0, from); i <= Math.min(toks.length - 1, to); i++) if (toks[i].k === "w") o.push(esc(toks[i].w)); return o.join(" "); };
    return `<div class="sr" data-n="${n}"><span class="tc">${fmt(toks[a].r0).slice(0, 10)}</span>…${ctx(a - 8, a - 1)} <mark>${ctx(a, b)}</mark> ${ctx(b + 1, b + 8)}…</div>`;
  }).join("") + (hits.length > 500 ? `<div class="pad dim">… ще ${hits.length - 500}</div>` : "");
}
function applySearchHighlight() {
  $$("#editor .hit").forEach((e) => e.classList.remove("hit", "hit-cur"));
  S.search.hits.forEach((h, n) => h.forEach((i) => tokenEls(i).forEach((e) => { e.classList.add("hit"); if (n === S.search.idx) e.classList.add("hit-cur"); })));
}
function gotoHit(n) {
  const hits = S.search.hits;
  if (!hits.length) return;
  S.search.idx = (n + hits.length) % hits.length;
  applySearchHighlight();
  renderSearch();
  const i = hits[S.search.idx][0];
  scrollToToken(i);
  seekToToken(i);
}

// =====================================================================
// viewer (in-panel preview, edit-order playback)
// =====================================================================
const video = () => $("#video");
function loadClip(ci, force) {
  const p = P();
  if (!p || !p.clips.length) return;
  if (S.curClip === ci && !force) return;
  S.curClip = ci;
  const v = video();
  v.src = "/media/" + ci + "?v=" + Date.now();
  $("#viewer-msg").classList.add("hidden");
  v.onerror = () => showViewerMsg(ci);
}
function showViewerMsg(ci) {
  const m = $("#viewer-msg");
  m.innerHTML = `<div>Браузер не може відтворити цей файл (кодек/контейнер).</div><button class="btn sm">Створити проксі для перегляду (FFmpeg)</button><div class="small">На таймлайн Resolve завжди йде оригінальне медіа.</div>`;
  m.classList.remove("hidden");
  m.querySelector("button").onclick = () => act(() => api("/api/proxy", { clip: ci }));
}
function tokenAt(ci, t) {
  const arr = S.clipTokens && S.clipTokens[ci];
  if (!arr || !arr.length) return null;
  let lo = 0, hi = arr.length - 1;
  while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (arr[mid].t0 <= t) lo = mid; else hi = mid - 1; }
  return arr[lo];
}
function segForToken(i, pid) {
  const segs = S.render.segments;
  for (let n = 0; n < segs.length; n++) for (const part of segs[n].parts)
    if (part.a <= i && i <= part.b && (!pid || part.pid === pid)) return n;
  return -1;
}
function seekToToken(i, pid) {
  const t = T()[i];
  if (!t) return;
  if (S.curClip !== t.c) loadClip(t.c);
  const v = video();
  const go = () => { v.currentTime = t.t0 + 0.001; };
  if (v.readyState >= 1) go(); else v.addEventListener("loadedmetadata", go, { once: true });
  S.segIdx = segForToken(i, pid);
  setCurToken(i);
}
function setCurToken(i) {
  if (S.curToken === i) return;
  S.curToken = i;
  const els = markToken(i, "cur");
  if (els.length && !video().paused) scrollToToken(i);
  updateTimecodes();
}
function updateTimecodes() {
  const t = T()[S.curToken];
  const v = video();
  if (!t) return;
  const src = t.r0 + (v.currentTime - t.t0);
  $("#tc-src").textContent = fmt(src);
  const seg = S.render.segments[S.segIdx];
  if (seg) $("#tc-edit").textContent = fmt(seg.r0 + (v.currentTime - seg.s0)) + " / " + fmt(S.render.duration);
  drawOverviewPlayhead(src);
}
function onVideoTime() {
  const v = video();
  if (S.curClip === null) return;
  if (S.playMode && !v.paused) {
    const segs = S.render.segments;
    let seg = segs[S.segIdx];
    if (!seg || seg.c !== S.curClip || v.currentTime < seg.s0 - 0.25 || v.currentTime > seg.s1 + 0.25) {
      // find a segment containing the current position
      S.segIdx = segs.findIndex((s) => s.c === S.curClip && v.currentTime >= s.s0 - 0.05 && v.currentTime < s.s1);
      seg = segs[S.segIdx];
      if (!seg) { const n = segs.findIndex((s) => s.c === S.curClip && s.s0 > v.currentTime); if (n >= 0) jumpSeg(n); else v.pause(); return; }
    }
    if (v.currentTime >= seg.s1 - 0.04) {
      if (S.segIdx + 1 < segs.length) jumpSeg(S.segIdx + 1); else v.pause();
      return;
    }
  }
  const tok = tokenAt(S.curClip, v.currentTime);
  if (tok) setCurToken(tok.i);
  updateTimecodes();
}
function jumpSeg(n) {
  const seg = S.render.segments[n];
  if (!seg) return;
  S.segIdx = n;
  const v = video();
  const playing = !v.paused;
  if (seg.c !== S.curClip) {
    loadClip(seg.c);
    v.addEventListener("loadedmetadata", () => { v.currentTime = seg.s0; if (playing) v.play(); }, { once: true });
  } else v.currentTime = seg.s0;
}
function togglePlay() {
  const v = video();
  if (!v.src) return;
  if (v.paused) {
    if (S.playMode && S.render.segments.length) {
      const segs = S.render.segments;
      const inSeg = segs.findIndex((s) => s.c === S.curClip && v.currentTime >= s.s0 - 0.05 && v.currentTime < s.s1 - 0.05);
      if (inSeg < 0) jumpSeg(Math.max(0, S.segIdx));
      else S.segIdx = inSeg;
    }
    v.play().catch((e) => toast("Відтворення: " + e.message, "error"));
  } else v.pause();
}

// ---- Resolve sync of the playhead
async function jumpResolve(token, target = "auto", piece) {
  try {
    const r = await api("/api/jump", { token, target, piece });
    status("Resolve: " + (r.timeline === "edit" ? "монтаж" : "оригінал") + " @ " + r.timecode);
  } catch (e) { if (!/not-current/.test(e.message)) toast(e.message, "error"); }
}
async function jumpResolveRec(rec) {
  try { const r = await api("/api/jump", { rec, target: "source" }); status("Resolve: оригінал @ " + r.timecode); }
  catch (e) { toast(e.message, "error"); }
}
let polling = false;
async function pollPlayhead() {
  if (polling || !$("#chk-follow").checked || !P() || !P().tokens.length || !video().paused || document.hidden) return;
  polling = true;
  try {
    const r = await api("/api/playhead");
    if (r.token !== undefined && r.token !== S.resolveToken) {
      S.resolveToken = r.token;
      if (r.token !== null) { markToken(r.token, "rcur"); scrollToToken(r.token, r.piece); }
      else markToken(null, "rcur");
    }
  } catch (e) { /* resolve unavailable */ }
  polling = false;
}

// =====================================================================
// overview strip
// =====================================================================
function renderOverview() {
  const cv = $("#ov-canvas");
  const p = P();
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth, h = cv.clientHeight;
  cv.width = w * dpr; cv.height = h * dpr;
  const g = cv.getContext("2d");
  g.scale(dpr, dpr);
  g.fillStyle = "#1b1b1e"; g.fillRect(0, 0, w, h);
  if (!p || !p.tokens.length) { $("#ov-dur-edit").textContent = "--:--"; $("#ov-dur-src").textContent = ""; return; }
  const toks = p.tokens, total = toks[toks.length - 1].r1 || 1;
  const X = (t) => (t / total) * w;
  // kept map
  const kept = new Uint8Array(toks.length);
  for (const pc of p.edit.pieces) { if (pc.off) continue; const cut = new Set(pc.cut); for (let i = pc.a; i <= pc.b; i++) if (!cut.has(i)) kept[i] = 1; }
  for (const t of toks) {
    const x0 = X(t.r0), x1 = Math.max(x0 + 0.5, X(t.r1));
    g.fillStyle = kept[t.i] ? (t.k === "w" ? "#4f6f52" : "#34463a") : (t.k === "w" ? "#3b2a2a" : "#2a2224");
    g.fillRect(x0, 14, x1 - x0, h - 22);
    if (t.hl >= 2) { g.fillStyle = t.hl === 3 ? "#e0564f" : "#d5b23e"; g.fillRect(x0, h - 7, Math.max(1, x1 - x0), 3); }
  }
  // linked scenes
  const link = p.edit.story_link;
  let story = null;
  if (link) for (const an of p.analyses) for (const v of an.variants) if (v.id === link.variant) story = v;
  if (story) story.scenes.forEach((sc) => sc.ranges.forEach((r) => { g.fillStyle = ROLE_COLORS[sc.role] || "#888"; g.fillRect(X(r.start), 3, Math.max(2, X(r.end) - X(r.start)), 7); }));
  // hour ticks
  g.fillStyle = "#55555c"; g.font = "9px sans-serif";
  const step = total > 7200 ? 1800 : total > 1800 ? 600 : total > 300 ? 60 : 10;
  for (let t = step; t < total; t += step) { g.fillRect(X(t), 10, 1, 4); }
  const d = S.render.duration;
  $("#ov-dur-edit").textContent = fmt(d, true);
  $("#ov-dur-src").textContent = "з " + fmt(total, true) + " (" + Math.round((d / total) * 100) + "%)";
  S._ovTotal = total;
  if (S._ovPlayhead !== undefined) drawOverviewPlayhead(S._ovPlayhead, true);
}
function drawOverviewPlayhead(src, noRedraw) {
  S._ovPlayhead = src;
  const cv = $("#ov-canvas");
  if (!S._ovTotal) return;
  let ph = $("#ov-ph");
  if (!ph) { ph = document.createElement("div"); ph.id = "ov-ph"; ph.style.cssText = "position:absolute;top:0;bottom:0;width:1px;background:#e0564f;pointer-events:none"; cv.parentElement.style.position = "relative"; cv.parentElement.appendChild(ph); }
  ph.style.left = cv.offsetLeft + (src / S._ovTotal) * cv.clientWidth + "px";
  void noRedraw;
}

// =====================================================================
// stories (structure editor)
// =====================================================================
function currentAnalysis() {
  const p = P();
  if (!p || !p.analyses.length) return null;
  return p.analyses.find((a) => a.id === S.storyAn) || p.analyses[0];
}
function currentVariant() {
  const an = currentAnalysis();
  if (!an || !an.variants.length) return null;
  return an.variants.find((v) => v.id === S.storyVar) || an.variants[0];
}
function rangeText(r) {
  const toks = T();
  const out = [];
  for (const t of toks) {
    if (t.r1 <= r.start) continue;
    if (t.r0 >= r.end) break;
    const mid = (t.r0 + t.r1) / 2;
    if (mid >= r.start && mid < r.end) out.push(t.k === "w" ? t.w : "[…]");
  }
  return out.join(" ").replace(/(\[…\]\s*)+/g, "[…] ");
}
function scenesDuration(v) { return v.scenes.filter((s) => !s.disabled).reduce((a, s) => a + s.ranges.reduce((b, r) => b + r.end - r.start, 0), 0); }

function renderStories() {
  const box = $("#stories");
  const p = P();
  if (!p || !p.tokens.length) { box.innerHTML = `<div class="pad dim" style="padding:14px">Після транскрибування натисніть <b>✦ Знайти сюжети з Claude</b> — Claude запропонує кілька концепцій відео зі структурою сцен.</div>`; return; }
  if (!p.analyses.length) {
    box.innerHTML = `<div style="padding:14px;display:flex;flex-direction:column;gap:10px;align-items:flex-start"><div class="dim">Сюжетних варіантів ще немає.</div><button class="btn primary" id="st-analyze">✦ Знайти сюжети з Claude</button><div class="note">Claude проаналізує всю транскрипцію (включно з паузами та сплесками гучності) і запропонує кілька концепцій: hook, розвиток, кульмінацію, завершення, закадровий текст і монтажні прийоми — з точними таймкодами.</div></div>`;
    $("#st-analyze").onclick = openAnalyzeDialog;
    return;
  }
  const an = currentAnalysis();
  const v = currentVariant();
  const link = p.edit.story_link || {};
  let html = `<div class="an-select"><select class="inp" id="an-sel">${p.analyses.map((a) =>
    `<option value="${a.id}" ${a.id === an.id ? "selected" : ""}>${a.source === "chat" ? "Чат з Claude" : "Аналіз"} · ${new Date(a.created * 1000).toLocaleString("uk-UA", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })} · ${a.variants.length} вар.</option>`).join("")}</select>` +
    `<button class="tbtn" id="an-del" title="Видалити аналіз">🗑</button></div>`;
  if (an.overview) html += `<div class="overview-text">${esc(an.overview)}${an.instructions ? `<div class="small dim2" style="margin-top:4px">Побажання: ${esc(an.instructions)}</div>` : ""}</div>`;
  if (an.warnings && an.warnings.length) html += `<div class="warn">⚠ ${esc(an.warnings.slice(0, 3).join("; "))}${an.warnings.length > 3 ? "…" : ""}</div>`;
  html += `<div class="vtabs">${an.variants.map((x, n) => `<button class="vtab ${v && x.id === v.id ? "active" : ""}" data-v="${x.id}"><span class="vt-title">${n + 1}. ${esc(x.title)}</span><span class="vt-sub">${x.scenes.length} сцен · ${fmt(scenesDuration(x), true)}${link.variant === x.id ? ' · <span class="linked">● у монтажі</span>' : ""}</span></button>`).join("")}</div>`;
  if (v) html += variantHTML(v, an, link.variant === v.id);
  box.innerHTML = html;
  bindStories(box, an, v);
}
function variantHTML(v, an, linked) {
  let h = `<div class="vhead"><input class="v-title" data-vf="title" value="${esc(v.title)}">` +
    `<textarea class="fld" data-vf="logline" rows="1">${esc(v.logline)}</textarea>` +
    `<div class="v-meta"><span>Тон: ${esc(v.tone || "—")}</span><span>Оцінка: ${esc(v.estimated_duration || "—")}</span><span>Сцени: ${fmt(scenesDuration(v), true)}</span></div>` +
    (v.why_it_works ? `<div class="v-why">${esc(v.why_it_works)}</div>` : "") +
    `<div class="v-actions">` +
    (linked ? `<span class="linked-pill">● Застосовано до монтажу — зміни структури оновлюють монтаж</span><button class="btn sm" data-va="apply">Перезастосувати</button><button class="btn sm ghost" data-va="unlink">Відв'язати</button>`
      : `<button class="btn primary sm" data-va="apply" title="Замінити поточний монтаж послідовністю сцен цього варіанта (можна скасувати)">Застосувати до монтажу</button>`) +
    `<button class="btn sm" data-va="sync" title="Застосувати і перенести на таймлайн Resolve">▸ На таймлайн</button>` +
    `<button class="btn sm" data-va="play">▶ Переглянути</button>` +
    `<button class="btn sm ghost" data-va="md" title="Експорт сценарію">⤓ MD</button>` +
    `<button class="btn sm ghost" data-va="del" title="Видалити варіант">🗑</button></div></div>`;
  if (v.hook_options && v.hook_options.length) {
    h += `<div class="sec"><div class="sec-h">Варіанти hook</div><div class="sec-b">${v.hook_options.map((o, n) =>
      `<div class="item"><span class="tc" data-jump="${o.start}">${fmt(o.start).slice(0, 10)}–${fmt(o.end).slice(3, 10)}</span><span style="flex:1">${esc(o.why)}</span><button class="btn sm ghost" data-hook="${n}" title="Зробити першою сценою">⇡ Hook</button></div>`).join("")}</div></div>`;
  }
  h += `<div class="sec"><div class="sec-h"><span>Сцени</span><span><button class="btn sm" data-va="add-scene" title="Створити сцену з виділеного в транскрипції тексту">＋ З виділення</button></span></div><div id="scenes">`;
  v.scenes.forEach((sc, n) => { h += sceneHTML(sc, n); });
  h += `</div></div>`;
  if (v.weak_parts && v.weak_parts.length) h += `<div class="sec"><div class="sec-h">Слабкі місця — можна скоротити</div><div class="sec-b">${v.weak_parts.map((w, n) =>
    `<div class="item"><span class="tc" data-jump="${w.start}">${fmt(w.start).slice(0, 10)}–${fmt(w.end).slice(3, 10)}</span><span style="flex:1">${esc(w.reason)}</span><button class="btn sm ghost" data-weak="${n}" title="Вирізати цей діапазон у поточному монтажі">✂</button></div>`).join("")}</div></div>`;
  if (v.missing_context && v.missing_context.length) h += `<div class="sec"><div class="sec-h">Бракує контексту</div><div class="sec-b">${v.missing_context.map((m) =>
    `<div class="item"><span class="tc" data-jump="${m.at}">${fmt(m.at).slice(0, 10)}</span><span style="flex:1">${esc(m.problem)}${m.fix ? `<br><span class="dim">→ ${esc(m.fix)}</span>` : ""}</span></div>`).join("")}</div></div>`;
  if (v.ending) h += `<div class="sec"><div class="sec-h">Завершення</div><div class="sec-b v-why">${esc(v.ending)}</div></div>`;
  return h;
}
function sceneHTML(sc, n) {
  const color = ROLE_COLORS[sc.role] || "#888";
  const excerpt = sc.ranges.map(rangeText).join(" ⟩ ");
  return `<div class="scene${sc.disabled ? " disabled" : ""}" data-scene="${sc.id}" style="border-left-color:${color}">
    <div class="scene-h"><span class="handle" draggable="true">⋮⋮</span><span class="num">${n + 1}</span>
      <span class="role" style="background:${color}" data-role title="Змінити роль">${esc(ROLE_UA[sc.role] || sc.role)}</span>
      <input class="scene-title" data-sf="title" value="${esc(sc.title)}">
      <span class="scene-tools"><button class="tbtn sm" data-sa="play" title="Переглянути сцену">▶</button><button class="tbtn sm" data-sa="up" title="Вгору">▴</button><button class="tbtn sm" data-sa="down" title="Вниз">▾</button>
      <button class="tbtn sm" data-sa="toggle" title="${sc.disabled ? "Увімкнути" : "Вимкнути"} сцену">${sc.disabled ? "◌" : "◉"}</button><button class="tbtn sm" data-sa="del" title="Видалити сцену">✕</button></span></div>
    <div class="scene-b">
      <div class="ranges">${sc.ranges.map((r, k) => `<span class="rchip" data-r="${k}"><span data-rj="${k}" title="Клік — перейти; Alt+клік — редагувати">${fmt(r.start).slice(0, 10)} – ${fmt(r.end).slice(3, 10)}</span><b data-rx="${k}" title="Прибрати діапазон">×</b></span>`).join("")}
        <button class="btn sm ghost" data-sa="range-add" title="Додати діапазон з виділеного тексту">＋ діапазон</button><span class="dim small">${sc.ranges.reduce((a, r) => a + r.end - r.start, 0).toFixed(1)}s</span></div>
      <textarea class="fld" data-sf="description" rows="1" placeholder="Опис сцени">${esc(sc.description)}</textarea>
      ${excerpt ? `<div class="excerpt" title="Клік — розгорнути">${esc(excerpt)}</div>` : `<div class="warn" style="padding:0">Немає діапазонів — додайте з виділення</div>`}
      <div><div class="flabel">Монтаж</div><textarea class="fld" data-sf="edit_notes" rows="1" placeholder="Коментарі щодо монтажу">${esc(sc.edit_notes)}</textarea></div>
      ${sc.techniques && sc.techniques.length ? `<div class="chips">${sc.techniques.map((t) => `<span class="chip">${esc(t)}</span>`).join("")}</div>` : ""}
      ${(sc.voiceover || []).map((vo, k) => `<div class="vo"><span class="vo-at" data-jump="${vo.at}">VO ${fmt(vo.at).slice(3, 10)}</span><textarea data-vo="${k}" rows="1">${esc(vo.text)}</textarea><button class="tbtn sm" data-vox="${k}" title="Прибрати">×</button></div>`).join("")}
      <div><button class="btn sm ghost" data-sa="vo-add">＋ Закадровий текст</button></div>
    </div></div>`;
}
function autosize(el) { el.style.height = "auto"; el.style.height = el.scrollHeight + 2 + "px"; }

async function storyOp(an, v, action, extra) {
  try {
    const r = await api("/api/story", Object.assign({ analysis: an.id, variant: v.id, action }, extra || {}));
    if (r.relinked) status("Монтаж оновлено за структурою");
    return r;
  } catch (e) { toast(e.message, "error"); }
}
function bindStories(box, an, v) {
  $("#an-sel", box).onchange = (e) => { S.storyAn = e.target.value; S.storyVar = null; renderStories(); };
  $("#an-del", box).onclick = () => { if (confirm("Видалити цей аналіз з усіма варіантами?")) act(() => api("/api/analysis/delete", { analysis: an.id })); };
  $$(".vtab", box).forEach((b) => (b.onclick = () => { S.storyVar = b.dataset.v; renderStories(); }));
  if (!v) return;
  $$("textarea.fld, .vo textarea", box).forEach(autosize);
  $$("[data-vf]", box).forEach((el) => {
    el.oninput = () => el.tagName === "TEXTAREA" && autosize(el);
    el.onchange = () => storyOp(an, v, "update_variant", { fields: { [el.dataset.vf]: el.value } });
  });
  $$("[data-va]", box).forEach((b) => (b.onclick = async () => {
    const a = b.dataset.va;
    if (a === "apply") { await act(() => api("/api/story/apply", { analysis: an.id, variant: v.id }), "Монтаж побудовано за сюжетом «" + v.title + "»"); }
    if (a === "unlink") await act(() => api("/api/story/unlink", {}));
    if (a === "sync") { if ((P().edit.story_link || {}).variant !== v.id) await act(() => api("/api/story/apply", { analysis: an.id, variant: v.id })); syncTimeline(); }
    if (a === "play") playVariant(v);
    if (a === "md") window.open("/api/export/md?variant=" + v.id);
    if (a === "del" && confirm("Видалити варіант «" + v.title + "»?")) storyOp(an, v, "delete_variant");
    if (a === "add-scene") addSceneFromSel(getSelection_());
  }));
  $$("[data-jump]", box).forEach((el) => (el.onclick = () => jumpRec(+el.dataset.jump)));
  $$("[data-hook]", box).forEach((b) => (b.onclick = () => {
    const o = v.hook_options[+b.dataset.hook];
    storyOp(an, v, "add_scene", { role: "hook", title: "Hook: " + (o.why || "").slice(0, 40), ranges: [{ start: o.start, end: o.end }], index: 0 });
  }));
  $$("[data-weak]", box).forEach((b) => (b.onclick = () => {
    const w = v.weak_parts[+b.dataset.weak];
    const idx = []; T().forEach((t) => { const m = (t.r0 + t.r1) / 2; if (m >= w.start && m < w.end) idx.push(t.i); });
    editOp("cut_tokens", { indices: idx }, "Вирізано слабке місце");
  }));
  // scenes
  $$(".scene", box).forEach((el) => {
    const sc = v.scenes.find((s) => s.id === el.dataset.scene);
    const upd = (fields) => storyOp(an, v, "update_scene", { scene: sc.id, fields });
    $$("[data-sf]", el).forEach((f) => {
      f.oninput = () => f.tagName === "TEXTAREA" && autosize(f);
      f.onchange = () => upd({ [f.dataset.sf]: f.value });
    });
    $$("[data-vo]", el).forEach((f) => {
      f.oninput = () => autosize(f);
      f.onchange = () => { const vo = sc.voiceover.map((x, k) => (k === +f.dataset.vo ? Object.assign({}, x, { text: f.value }) : x)); upd({ voiceover: vo }); };
    });
    $$("[data-vox]", el).forEach((b) => (b.onclick = () => upd({ voiceover: sc.voiceover.filter((x, k) => k !== +b.dataset.vox) })));
    $$("[data-rj]", el).forEach((s) => (s.onclick = (e) => {
      const r = sc.ranges[+s.dataset.rj];
      if (e.altKey) {
        const val = prompt("Діапазон (ГГ:ХХ:СС.с – ГГ:ХХ:СС.с)", fmt(r.start) + " – " + fmt(r.end));
        if (!val) return;
        try {
          const [a, b] = val.split(/[–-]{1}\s/).map((x) => parseTs(x.trim()));
          upd({ ranges: sc.ranges.map((x, k) => (k === +s.dataset.rj ? { start: a, end: b } : x)) });
        } catch (er) { toast("Невірний формат діапазону", "error"); }
      } else jumpRec(r.start);
    }));
    $$("[data-rx]", el).forEach((b) => (b.onclick = () => upd({ ranges: sc.ranges.filter((x, k) => k !== +b.dataset.rx) })));
    const ex = $(".excerpt", el);
    if (ex) ex.onclick = () => ex.classList.toggle("open");
    $("[data-role]", el).onclick = (e) => {
      showMenu(e.clientX, e.clientY, Object.keys(ROLE_UA).map((r) => ({ label: ROLE_UA[r], fn: () => upd({ role: r }) })));
    };
    $$("[data-sa]", el).forEach((b) => (b.onclick = () => {
      const a = b.dataset.sa;
      const order = v.scenes.map((s) => s.id);
      const n = order.indexOf(sc.id);
      if (a === "del") storyOp(an, v, "delete_scene", { scene: sc.id });
      if (a === "toggle") upd({ disabled: !sc.disabled });
      if (a === "up" && n > 0) { order.splice(n, 1); order.splice(n - 1, 0, sc.id); storyOp(an, v, "reorder", { order }); }
      if (a === "down" && n < order.length - 1) { order.splice(n, 1); order.splice(n + 1, 0, sc.id); storyOp(an, v, "reorder", { order }); }
      if (a === "play" && sc.ranges.length) playRanges(sc.ranges);
      if (a === "range-add") {
        const sel = getSelection_();
        if (!sel) return toast("Виділіть текст у транскрипції");
        const rs = selToRanges(sel);
        upd({ ranges: sc.ranges.concat(rs) });
      }
      if (a === "vo-add") upd({ voiceover: (sc.voiceover || []).concat([{ at: sc.ranges[0] ? sc.ranges[0].start : 0, text: "" }]) });
    }));
    // drag & drop reorder
    const handle = $(".handle", el);
    handle.ondragstart = (e) => { e.dataTransfer.setData("text/scene", sc.id); el.classList.add("dragging"); };
    handle.ondragend = () => el.classList.remove("dragging");
    el.ondragover = (e) => {
      if (!e.dataTransfer.types.includes("text/scene")) return;
      e.preventDefault();
      const r = el.getBoundingClientRect();
      el.classList.toggle("drop-before", e.clientY < r.top + r.height / 2);
      el.classList.toggle("drop-after", e.clientY >= r.top + r.height / 2);
    };
    el.ondragleave = () => el.classList.remove("drop-before", "drop-after");
    el.ondrop = (e) => {
      e.preventDefault();
      const id = e.dataTransfer.getData("text/scene");
      const after = el.classList.contains("drop-after");
      el.classList.remove("drop-before", "drop-after");
      if (!id || id === sc.id) return;
      const order = v.scenes.map((s) => s.id).filter((x) => x !== id);
      order.splice(order.indexOf(sc.id) + (after ? 1 : 0), 0, id);
      storyOp(an, v, "reorder", { order });
    };
  });
}
function selToRanges(sel) {
  const toks = T();
  return sel.map((s) => ({ start: toks[s.a].r0, end: toks[s.b].r1 }));
}
function addSceneFromSel(sel) {
  const v = currentVariant(), an = currentAnalysis();
  if (!v) return toast("Спочатку отримайте або оберіть сюжетний варіант");
  if (!sel) return toast("Виділіть текст у транскрипції");
  const title = selText(sel).split(/\s+/).slice(0, 6).join(" ");
  storyOp(an, v, "add_scene", { role: "other", title: title || "Нова сцена", ranges: selToRanges(sel) });
  switchTab("right", "stories");
}
function jumpRec(rec) {
  // nearest token to a record time on the original timeline
  const toks = T();
  let lo = 0, hi = toks.length - 1;
  while (lo < hi) { const m = (lo + hi + 1) >> 1; if (toks[m].r0 <= rec + 1e-3) lo = m; else hi = m - 1; }
  const tok = toks[lo];
  if (!tok) return;
  scrollToToken(tok.i);
  seekToToken(tok.i);
  if ($("#chk-clickjump").checked) jumpResolveRec(rec);
}
function playRanges(ranges) {
  // temporary playlist from ranges (preview without changing the edit)
  const segs = [];
  const toks = T();
  ranges.forEach((r) => {
    let cur = null;
    toks.forEach((t) => {
      const m = (t.r0 + t.r1) / 2;
      if (m < r.start || m >= r.end) return;
      if (cur && cur.c === t.c && Math.abs(cur.s1 - t.t0) < 1e-3) { cur.s1 = t.t1; cur.parts[0].b = t.i; }
      else { cur = { c: t.c, s0: t.t0, s1: t.t1, r0: 0, parts: [{ pid: null, a: t.i, b: t.i, s0: t.t0 }] }; segs.push(cur); }
    });
  });
  let pos = 0; segs.forEach((s) => { s.r0 = pos; s.dur = s.s1 - s.s0; pos += s.dur; });
  S._savedRender = S._savedRender || S.render;
  S.render = { segments: segs, duration: pos };
  S.playMode = true; $("#chk-playedit").checked = true;
  jumpSeg(0);
  video().play();
  const restore = () => { if (S._savedRender) { S.render = S._savedRender; S._savedRender = null; } video().removeEventListener("pause", restore); };
  video().addEventListener("pause", restore);
}
function playVariant(v) { playRanges(v.scenes.filter((s) => !s.disabled).flatMap((s) => s.ranges)); }

// ---- moments tab
function renderMoments() {
  const box = $("#moments");
  const an = currentAnalysis();
  if (!an || !an.moments.length) { box.innerHTML = `<div class="pad dim" style="padding:14px">Цікаві моменти з'являться після аналізу Claude.</div>`; return; }
  const ms = an.moments.slice().sort((a, b) => a.start - b.start);
  box.innerHTML = `<div class="sec-b">${ms.map((m) =>
    `<div class="item"><span class="tc" data-jump="${m.start}">${fmt(m.start).slice(0, 10)}</span><span class="mtype" style="background:${MOMENT_COLORS[m.type] || "#888"}">${esc(MOMENT_UA[m.type] || m.type)}</span><span style="flex:1">${esc(m.description)}</span><span class="stars">${"★".repeat(m.strength)}</span></div>`).join("")}</div>`;
  $$("[data-jump]", box).forEach((el) => (el.onclick = () => jumpRec(+el.dataset.jump)));
}

// =====================================================================
// chat
// =====================================================================
const QUICK = ["Знайди кращий початок", "Запропонуй іншу кульмінацію", "Скороти середину, вона затягнута", "Знайди ще цікаві моменти",
  "Покращ переходи між сценами", "Напиши закадровий текст", "Перебудуй сюжет з урахуванням моїх правок"];
function mdLite(text) {
  let h = esc(text);
  h = h.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/`([^`]+)`/g, "<code>$1</code>");
  h = h.replace(/\[?(\d{1,2}:\d{2}:\d{2}(?:[.,]\d)?)(?:\s*[–-]\s*(\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d)?))?\]?/g, (m, a, b) =>
    `<a class="ts" data-ts="${a}">${a}${b ? "–" + b : ""}</a>`);
  const lines = h.split("\n");
  let out = "", list = null;
  for (const line of lines) {
    const m = line.match(/^\s*(?:[-*•]|\d+[.)])\s+(.*)/);
    const hd = line.match(/^\s*#{1,4}\s+(.*)/);
    if (m) { if (!list) { out += "<ul>"; list = true; } out += `<li>${m[1]}</li>`; continue; }
    if (list) { out += "</ul>"; list = null; }
    if (hd) out += `<h4>${hd[1]}</h4>`;
    else if (line.trim()) out += `<p>${line}</p>`;
  }
  if (list) out += "</ul>";
  return out;
}
function renderChat() {
  const log = $("#chat-log");
  const p = P();
  $("#chat-quick").innerHTML = QUICK.map((q) => `<button class="qchip">${esc(q)}</button>`).join("");
  $$(".qchip").forEach((b) => (b.onclick = () => { $("#chat-text").value = b.textContent; $("#chat-text").focus(); }));
  if (!p || !p.tokens.length) { log.innerHTML = `<div class="dim">Чат стане доступним після транскрибування.</div>`; return; }
  const msgs = p.chat || [];
  log.innerHTML = msgs.length ? "" : `<div class="dim" style="line-height:1.6">Claude бачить усю транскрипцію, поточний монтаж і сюжетні варіанти. Просіть знайти кращий початок, іншу кульмінацію, скоротити частину чи написати закадровий текст. Нові структури з'являться у вкладці «Сюжети».</div>`;
  msgs.forEach((m) => {
    const el = document.createElement("div");
    el.className = "msg " + m.role;
    const who = m.role === "user" ? "Ви" : m.role === "error" ? "Помилка" : "Claude";
    let body = m.role === "assistant" ? mdLite(m.text) : esc(m.text);
    if (m.variants && m.variants.length) body += m.variants.map((id) => `<button class="btn sm blue vlink" data-vid="${id}">Відкрити новий варіант сюжету</button>`).join(" ");
    el.innerHTML = `<div class="who">${who}</div><div class="body">${body}</div>`;
    log.appendChild(el);
  });
  if (S.chatStream) { const el = document.createElement("div"); el.id = "chat-stream"; el.className = "msg assistant"; log.appendChild(el); renderChatStream(); }
  bindChatLinks(log);
  log.scrollTop = log.scrollHeight;
}
function renderChatStream() {
  let el = $("#chat-stream");
  if (!el) { renderChat(); el = $("#chat-stream"); if (!el) return; }
  const s = S.chatStream || { text: "", thinking: "" };
  el.innerHTML = `<div class="who">Claude</div>${s.thinking && !s.text ? `<div class="thinking">${esc(s.thinking.slice(-600))}</div>` : ""}<div class="body typing">${mdLite(s.text)}</div>`;
  bindChatLinks(el);
  const log = $("#chat-log"); log.scrollTop = log.scrollHeight;
}
function bindChatLinks(root) {
  $$("a.ts", root).forEach((a) => (a.onclick = () => { try { jumpRec(parseTs(a.dataset.ts)); } catch (e) { /* ignore */ } }));
  $$("[data-vid]", root).forEach((b) => (b.onclick = () => {
    for (const an of P().analyses) for (const v of an.variants) if (v.id === b.dataset.vid) { S.storyAn = an.id; S.storyVar = v.id; }
    switchTab("right", "stories"); renderStories();
  }));
}
async function sendChat() {
  const ta = $("#chat-text");
  const text = ta.value.trim();
  if (!text) return;
  if (!S.settings.has_api_key) { toast("Додайте ключ Anthropic API у налаштуваннях (⚙)", "error"); openSettings(); return; }
  const chat = P().chat;
  chat.push({ role: "user", text, local: true });
  S.chatStream = { text: "", thinking: "" };
  ta.value = "";
  renderChat();
  try {
    await api("/api/ai/chat", { text });
  } catch (e) {
    const n = chat.lastIndexOf(chat.find((m) => m.local && m.text === text));
    if (n >= 0) chat.splice(n, 1);
    S.chatStream = null;
    ta.value = text;
    renderChat();
    toast(e.message, "error");
  }
}

// =====================================================================
// dialogs
// =====================================================================
function openModal(title, body, footer) {
  $("#modal").innerHTML = `<div class="modal-h"><span>${esc(title)}</span><button data-close>✕</button></div><div class="modal-b">${body}</div>${footer ? `<div class="modal-f">${footer}</div>` : ""}`;
  $("#modal-back").classList.remove("hidden");
  $$("[data-close]", $("#modal")).forEach((b) => (b.onclick = closeModal));
}
function closeModal() { $("#modal-back").classList.add("hidden"); }

function openTranscribeDialog() {
  const p = P();
  if (!p) return toast("Немає підключеного таймлайну", "error");
  const s = S.settings;
  const running = Object.values(S.jobs).find((j) => j.kind === "transcribe" && j.status === "running");
  const models = ["large-v3", "large-v3-turbo", "medium", "small", "base", "tiny", "distil-large-v3"];
  const langs = [["", "Автовизначення"], ["uk", "Українська"], ["en", "English"], ["ru", "Русский"], ["pl", "Polski"], ["de", "Deutsch"], ["es", "Español"], ["fr", "Français"]];
  const total = p.clips.reduce((a, c) => a + (c.src_out - c.src_in) / c.media_fps, 0);
  openModal("Транскрибування", `
    <div class="note">Таймлайн <b>${esc(p.resolve.timeline)}</b>: ${p.clips.length} кліп(ів), ${fmt(total, true)} матеріалу. Аудіо витягується FFmpeg, мова розпізнається Whisper (faster-whisper) з таймкодами для кожного слова.</div>
    ${p.timeline_changed ? `<div class="warn" style="padding:0">⚠ Кліпи на таймлайні змінилися після транскрибування. <button class="btn sm" id="tr-refresh">Перечитати кліпи</button></div>` : ""}
    ${p.tokens.length ? `<div class="warn" style="padding:0">Повторне транскрибування замінить поточний текст і скине монтаж (сюжети збережуться).</div>` : ""}
    <div class="frow"><label>Модель Whisper</label><select class="inp" id="tr-model">${models.map((m) => `<option ${m === s.whisper_model ? "selected" : ""}>${m}</option>`).join("")}</select></div>
    <div class="frow"><label>Мова</label><select class="inp" id="tr-lang">${langs.map(([v, n]) => `<option value="${v}" ${v === (s.language || "") ? "selected" : ""}>${n}</option>`).join("")}</select></div>
    <div class="frow"><label>Визначення мовців</label><label class="mini"><input type="checkbox" id="tr-diar" ${s.diarize ? "checked" : ""}> pyannote.audio ${s.has_hf_token ? "" : '<span class="dim">(потрібен HF токен у налаштуваннях)</span>'}</label></div>
    <div class="frow"><label>Кількість мовців</label><input class="inp" id="tr-nsp" type="number" min="0" max="10" value="${s.num_speakers || 0}" title="0 — автоматично"></div>
    <div class="note">Порада: large-v3 — найточніша (GPU бажано), large-v3-turbo — швидша, small/medium — для CPU. Модель завантажується один раз.</div>
    <div class="pbar"><i id="tr-progress" style="width:${running ? running.progress * 100 : 0}%"></i></div><div id="tr-msg" class="small dim">${running ? esc(running.message) : ""}</div>
    <hr style="border:none;border-top:1px solid var(--line2);width:100%">
    <div class="frow"><label>Або імпорт</label><div><input type="file" id="tr-file" accept=".srt,.vtt,.json"> <div class="note">SRT/VTT (наприклад, субтитри з Resolve «Create Subtitles from Audio», з таймкодами таймлайну) або JSON Whisper/WhisperX.</div></div></div>`,
    `<button class="btn" data-close>Закрити</button><button class="btn primary" id="tr-start" ${running ? "disabled" : ""}>Почати</button>`);
  const r = $("#tr-refresh");
  if (r) r.onclick = () => act(() => api("/api/refresh_clips", {}), "Кліпи оновлено").then(() => { closeModal(); refreshState(); });
  $("#tr-start").onclick = async () => {
    const body = { model: $("#tr-model").value, language: $("#tr-lang").value, diarize: $("#tr-diar").checked, num_speakers: +$("#tr-nsp").value };
    await act(() => api("/api/settings", { whisper_model: body.model, language: body.language, diarize: body.diarize, num_speakers: body.num_speakers }));
    await act(() => api("/api/transcribe", body));
    $("#tr-start").disabled = true;
  };
  $("#tr-file").onchange = async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    const text = await f.text();
    const fmtType = f.name.toLowerCase().endsWith(".json") ? "json" : "srt";
    await act(() => api("/api/import_transcript", { text, format: fmtType }), "Транскрипцію імпортовано");
    closeModal(); refreshState();
  };
}

function openAnalyzeDialog() {
  if (!S.settings.has_api_key) { toast("Додайте ключ Anthropic API у налаштуваннях", "error"); return openSettings(); }
  const running = Object.values(S.jobs).find((j) => j.kind === "analyze" && j.status === "running");
  openModal("✦ Пошук сюжетів з Claude", `
    <div class="note">Claude (${esc(S.settings.claude_model)}) отримає всю транскрипцію з таймкодами, паузами (геймплей без мови) та сплесками гучності й запропонує концепції відео: hook, структуру сцен, кульмінацію, завершення, слабкі місця, закадровий текст і монтажні прийоми. Лише реальні події з запису.</div>
    <div class="frow"><label>Кількість варіантів</label><select class="inp" id="an-n"><option>1</option><option>2</option><option selected>3</option><option>4</option><option>5</option></select></div>
    <div><div class="flabel" style="margin-bottom:4px">Побажання (необов'язково)</div><textarea class="inp" id="an-instr" rows="4" style="width:100%" placeholder="Напр.: смішний ролик на 10–15 хв про провальну спробу вбити боса; акцент на реакціях; без спойлерів у hook."></textarea></div>
    <div class="pbar"><i id="analyze-progress" style="width:${running ? running.progress * 100 : 0}%"></i></div>
    <div id="analyze-msg" class="small dim">${running ? esc(running.message) : ""}</div>
    <div id="analyze-thinking" class="thinking-live${running ? "" : " hidden"}"></div>`,
    `<button class="btn" data-close>${running ? "Згорнути" : "Скасувати"}</button><button class="btn primary" id="an-go" ${running ? "disabled" : ""}>Аналізувати</button>`);
  $("#an-go").onclick = async () => {
    await act(() => api("/api/ai/analyze", { variants: +$("#an-n").value, instructions: $("#an-instr").value }));
    $("#an-go").disabled = true;
    $("#analyze-thinking").classList.remove("hidden");
  };
}

function openSettings() {
  const s = S.settings;
  const models = [["claude-opus-5-5", "Claude Opus 5.5 (рекомендовано)"], ["claude-fable-5-2", "Claude Fable 5.2 (найпотужніша)"], ["claude-sonnet-5-5", "Claude Sonnet 5.5 (швидша)"], ["claude-haiku-5-5", "Claude Haiku 5.5 (найдешевша)"]];
  openModal("Налаштування", `
    <div class="frow"><label>Anthropic API key</label><input class="inp" id="st-key" type="password" placeholder="${s.has_api_key ? "•••••••• (збережено)" : "sk-ant-…"}"></div>
    <div class="frow"><label>Модель Claude</label><select class="inp" id="st-model">${models.map(([v, n]) => `<option value="${v}" ${v === s.claude_model ? "selected" : ""}>${n}</option>`).join("")}</select></div>
    <div class="frow"><label>Глибина аналізу (effort)</label><select class="inp" id="st-effort">${["low", "medium", "high", "xhigh", "max"].map((v) => `<option ${v === s.claude_effort ? "selected" : ""}>${v}</option>`).join("")}</select></div>
    <div class="frow"><label>Hugging Face токен</label><input class="inp" id="st-hf" type="password" placeholder="${s.has_hf_token ? "•••••••• (збережено)" : "hf_… (для pyannote)"}"></div>
    <div class="frow"><label>Whisper: пристрій</label><select class="inp" id="st-dev">${["auto", "cuda", "cpu"].map((v) => `<option ${v === s.whisper_device ? "selected" : ""}>${v}</option>`).join("")}</select></div>
    <div class="frow"><label>Whisper: точність</label><select class="inp" id="st-ct">${["auto", "float16", "int8_float16", "int8", "float32"].map((v) => `<option ${v === s.whisper_compute_type ? "selected" : ""}>${v}</option>`).join("")}</select></div>
    <div class="frow"><label>Суфікс монтажного таймлайну</label><input class="inp" id="st-suffix" value="${esc(s.edit_timeline_suffix)}"></div>
    <div class="frow"><label>Розмір тексту</label><input class="inp" id="st-fs" type="number" min="11" max="20" value="${s.ui_font_size || 13}"></div>
    <div class="note">Ключі зберігаються локально у ~/.davinci_director/settings.json (права 600). Також підтримуються змінні середовища ANTHROPIC_API_KEY і HF_TOKEN.</div>`,
    `<button class="btn" data-close>Скасувати</button><button class="btn primary" id="st-save">Зберегти</button>`);
  $("#st-save").onclick = async () => {
    const body = { claude_model: $("#st-model").value, claude_effort: $("#st-effort").value, whisper_device: $("#st-dev").value,
      whisper_compute_type: $("#st-ct").value, edit_timeline_suffix: $("#st-suffix").value, ui_font_size: +$("#st-fs").value };
    if ($("#st-key").value.trim()) body.anthropic_api_key = $("#st-key").value.trim();
    if ($("#st-hf").value.trim()) body.hf_token = $("#st-hf").value.trim();
    const r = await act(() => api("/api/settings", body), "Налаштування збережено");
    S.settings = r.settings;
    document.documentElement.style.setProperty("--fs", (S.settings.ui_font_size || 13) + "px");
    closeModal();
  };
}

async function syncTimeline() {
  status("Перенесення на таймлайн Resolve…");
  try { await api("/api/sync", {}); status(""); }
  catch (e) { toast(e.message, "error"); status(""); }
}

// =====================================================================
// UI bindings
// =====================================================================
function switchTab(group, tab) {
  const tabs = $(`[data-tabs="${group}"]`);
  $$(".tab", tabs).forEach((t) => t.classList.toggle("active", t.dataset.tab === tab));
  const panel = tabs.parentElement;
  $$(".tab-body", panel).forEach((b) => b.classList.toggle("active", b.dataset.body === tab));
  if (tab === "chat") $("#chat-badge").classList.add("hidden");
}

function bindUI() {
  $$(".tabs").forEach((tabs) => $$(".tab", tabs).forEach((t) => (t.onclick = () => switchTab(tabs.dataset.tabs, t.dataset.tab))));
  $("#btn-connect").onclick = () => connect(false);
  $("#btn-transcribe").onclick = openTranscribeDialog;
  $("#btn-empty-transcribe").onclick = openTranscribeDialog;
  $("#btn-undo").onclick = undo;
  $("#btn-redo").onclick = redo;
  $("#btn-sync").onclick = syncTimeline;
  $("#btn-analyze").onclick = openAnalyzeDialog;
  $("#btn-settings").onclick = openSettings;
  $("#chk-autosync").onchange = (e) => act(() => api("/api/settings", { auto_sync: e.target.checked }));
  $("#btn-add-speaker").onclick = () => { const n = prompt("Ім'я мовця"); if (n) act(() => api("/api/speakers", { action: "add", name: n })); };

  // view options
  const opt = (id, cls, invert) => { const el = $(id); const f = () => $("#editor").classList.toggle(cls, invert ? !el.checked : el.checked); el.onchange = f; f(); };
  opt("#opt-showcut", "hide-cut", true); opt("#opt-gaps", "hide-gaps", true); opt("#opt-times", "hide-times", true); opt("#opt-conf", "hide-conf", true);
  $$(".dropdown [data-dd]").forEach((b) => (b.onclick = (e) => { e.stopPropagation(); const d = b.parentElement; const open = d.classList.contains("open"); $$(".dropdown.open").forEach((x) => x.classList.remove("open")); d.classList.toggle("open", !open); }));
  $$(".dd-menu").forEach((m) => (m.onclick = (e) => { if (e.target.tagName === "BUTTON") m.parentElement.classList.remove("open"); e.stopPropagation(); }));
  $$("[data-cmd]").forEach((b) => (b.onclick = () => runCommand(b.dataset.cmd)));

  // editor
  const ed = $("#editor");
  ed.addEventListener("mouseup", (e) => {
    if (e.button !== 0) return;
    const tokEl = e.target.closest("[data-i]");
    const pieceEl = e.target.closest(".piece");
    if (pieceEl) setFocusPiece(pieceEl.dataset.pid);
    const sel = window.getSelection();
    if (tokEl && sel.isCollapsed) {
      const i = +tokEl.dataset.i, pid = pieceEl.dataset.pid;
      S.caret = { pid, i };
      markToken(i, "caret");
      seekToToken(i, pid);
      if ($("#chk-clickjump").checked) jumpResolve(i, "auto", pid);
    }
  });
  ed.addEventListener("dblclick", (e) => { const t = e.target.closest(".w"); if (t) { clearSel(); editWord(t); } });
  ed.addEventListener("contextmenu", editorMenu);
  ed.addEventListener("click", (e) => {
    const pt = e.target.closest("[data-pt]");
    if (pt) {
      const pid = pt.closest(".piece").dataset.pid;
      const a = pt.dataset.pt;
      if (a === "dup") editOp("duplicate", { pids: [pid] });
      if (a === "up") editOp("move_step", { pids: [pid], delta: -1 });
      if (a === "down") editOp("move_step", { pids: [pid], delta: 1 });
      if (a === "del") editOp("delete_pieces", { pids: [pid] });
      if (a === "restore") editOp("restore_pieces", { pids: [pid] });
    }
    const spk = e.target.closest(".spk");
    if (spk) editorMenu(Object.assign(e, { preventDefault() {} }));
  });
  // drag & drop pieces
  let dragPid = null;
  ed.addEventListener("dragstart", (e) => {
    const h = e.target.closest(".handle");
    if (!h) return;
    dragPid = h.closest(".piece").dataset.pid;
    e.dataTransfer.setData("text/piece", dragPid);
    e.dataTransfer.effectAllowed = "move";
  });
  ed.addEventListener("dragover", (e) => {
    if (!dragPid) return;
    const pe = e.target.closest(".piece");
    if (!pe) return;
    e.preventDefault();
    $$(".dragover-top,.dragover-bottom", ed).forEach((x) => x.classList.remove("dragover-top", "dragover-bottom"));
    const r = pe.getBoundingClientRect();
    pe.classList.add(e.clientY < r.top + r.height / 2 ? "dragover-top" : "dragover-bottom");
    const er = ed.getBoundingClientRect();
    if (e.clientY < er.top + 40) ed.scrollTop -= 18; else if (e.clientY > er.bottom - 40) ed.scrollTop += 18;
  });
  ed.addEventListener("drop", (e) => {
    const pe = e.target.closest(".piece");
    if (!dragPid || !pe) return;
    e.preventDefault();
    const bottom = pe.classList.contains("dragover-bottom");
    $$(".dragover-top,.dragover-bottom", ed).forEach((x) => x.classList.remove("dragover-top", "dragover-bottom"));
    const pieces = P().edit.pieces;
    let before = pe.dataset.pid;
    if (bottom) { const n = pieces.findIndex((x) => x.id === before); before = pieces[n + 1] ? pieces[n + 1].id : null; }
    if (before !== dragPid) editOp("move", { pids: [dragPid], before }, "Фрагмент переставлено");
    dragPid = null;
  });
  ed.addEventListener("dragend", () => { dragPid = null; $$(".dragover-top,.dragover-bottom", ed).forEach((x) => x.classList.remove("dragover-top", "dragover-bottom")); });

  document.addEventListener("click", (e) => { if (!e.target.closest(".ctxmenu")) hideMenu(); if (!e.target.closest(".dropdown")) $$(".dropdown.open").forEach((x) => x.classList.remove("open")); });
  $("#ctxmenu").addEventListener("click", (e) => {
    const mi = e.target.closest(".mi[data-k]");
    if (!mi) return;
    hideMenu();
    const fn = S._menuFns[+mi.dataset.k];
    if (fn) fn();
  });

  // search
  let st = null;
  const onSearch = (val) => { clearTimeout(st); st = setTimeout(() => { runSearch(val); if (S.search.hits.length) gotoHit(0); }, 180); };
  $("#search-input").oninput = (e) => { $("#search-input2").value = e.target.value; onSearch(e.target.value); };
  $("#search-input2").oninput = (e) => { $("#search-input").value = e.target.value; onSearch(e.target.value); };
  $("#search-input").onkeydown = (e) => { if (e.key === "Enter") gotoHit(S.search.idx + (e.shiftKey ? -1 : 1)); if (e.key === "Escape") { e.target.value = ""; runSearch(""); ed.focus(); } e.stopPropagation(); };
  $("#search-prev").onclick = () => gotoHit(S.search.idx - 1);
  $("#search-next").onclick = () => gotoHit(S.search.idx + 1);
  $("#search-results").onclick = (e) => { const r = e.target.closest(".sr"); if (r) gotoHit(+r.dataset.n); };
  $("#btn-cut-matches").onclick = () => editOp("cut_tokens", { indices: S.search.hits.flat() }, "Вирізано збігів: " + S.search.hits.length);
  $("#btn-restore-matches").onclick = () => {
    const sel = [];
    const hitSet = new Set(S.search.hits.flat());
    for (const pc of P().edit.pieces) for (const i of pc.cut) if (hitSet.has(i)) sel.push({ pid: pc.id, a: i, b: i });
    if (sel.length) editOp("restore", { sel }, "Відновлено");
  };
  $("#outline").onclick = (e) => {
    const r = e.target.closest(".ol");
    if (!r) return;
    const pc = pieceById(r.dataset.pid);
    setFocusPiece(pc.id);
    const rec = pieceEls.get(pc.id);
    if (rec) rec.el.scrollIntoView({ block: "start" });
    seekToToken(pc.a, pc.id);
  };

  // viewer
  const v = video();
  v.addEventListener("timeupdate", onVideoTime);
  v.addEventListener("play", () => { $("#v-play").textContent = "❚❚"; tick(); });
  v.addEventListener("pause", () => ($("#v-play").textContent = "▶"));
  const tick = () => { if (!v.paused) { onVideoTime(); requestAnimationFrame(tick); } };
  $("#v-play").onclick = togglePlay;
  $("#v-next").onclick = () => jumpSeg(Math.min(S.render.segments.length - 1, S.segIdx + 1));
  $("#v-prev").onclick = () => jumpSeg(Math.max(0, S.segIdx - 1));
  $("#chk-playedit").onchange = (e) => (S.playMode = e.target.checked);

  // overview
  $("#ov-canvas").onclick = (e) => {
    if (!S._ovTotal) return;
    const r = e.target.getBoundingClientRect();
    jumpRec(((e.clientX - r.left) / r.width) * S._ovTotal);
  };
  window.addEventListener("resize", () => renderOverview());

  // chat
  $("#chat-send").onclick = sendChat;
  $("#chat-text").onkeydown = (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); sendChat(); } e.stopPropagation(); };
  $("#chat-clear").onclick = () => { if (confirm("Очистити розмову з Claude?")) act(() => api("/api/ai/chat_clear", {})); };

  // splitters
  $$(".splitter").forEach((sp) => {
    sp.onmousedown = (e) => {
      e.preventDefault();
      sp.classList.add("drag");
      const side = sp.dataset.split;
      const move = (ev) => {
        if (side === "left") document.documentElement.style.setProperty("--left-w", Math.max(240, Math.min(640, ev.clientX)) + "px");
        else document.documentElement.style.setProperty("--right-w", Math.max(320, Math.min(800, innerWidth - ev.clientX)) + "px");
        renderOverview();
      };
      const up = () => { sp.classList.remove("drag"); removeEventListener("mousemove", move); removeEventListener("mouseup", up); };
      addEventListener("mousemove", move); addEventListener("mouseup", up);
    };
  });

  // modal background closes
  $("#modal-back").addEventListener("mousedown", (e) => { if (e.target.id === "modal-back") closeModal(); });

  // keyboard
  document.addEventListener("keydown", (e) => {
    const tag = (e.target.tagName || "").toLowerCase();
    const typing = tag === "input" || tag === "textarea" || tag === "select";
    const mod = e.ctrlKey || e.metaKey;
    if (e.key === "Escape") { hideMenu(); closeModal(); }
    if (mod && e.key.toLowerCase() === "f") { e.preventDefault(); $("#search-input").focus(); $("#search-input").select(); return; }
    if (typing) return;
    if (mod && e.key.toLowerCase() === "z") { e.preventDefault(); e.shiftKey ? redo() : undo(); return; }
    if (mod && e.key.toLowerCase() === "y") { e.preventDefault(); redo(); return; }
    if (!P() || !P().tokens.length) return;
    if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); cmdDelete(); return; }
    if (mod && e.key.toLowerCase() === "c") { e.preventDefault(); cmdCopy(false); return; }
    if (mod && e.key.toLowerCase() === "x") { e.preventDefault(); cmdCopy(true); return; }
    if (mod && e.key.toLowerCase() === "v") { e.preventDefault(); cmdPaste("at"); return; }
    if (mod && e.key.toLowerCase() === "d") { e.preventDefault(); cmdDuplicate(); return; }
    if (e.altKey && e.key === "ArrowUp") { e.preventDefault(); cmdMove(-1); return; }
    if (e.altKey && e.key === "ArrowDown") { e.preventDefault(); cmdMove(1); return; }
    if (e.key === " ") { e.preventDefault(); togglePlay(); return; }
    if (e.key.toLowerCase() === "s" && !mod) { cmdSplit(); return; }
    if (mod && e.key === "Enter") { e.preventDefault(); syncTimeline(); }
  });
}

async function runCommand(cmd) {
  const dl = (path) => { const a = document.createElement("a"); a.href = path; a.download = ""; document.body.appendChild(a); a.click(); a.remove(); };
  switch (cmd) {
    case "remove-pauses": return editOp("remove_pauses", { min_len: 2.0 }, "Паузи скорочено");
    case "remove-pauses-5": return editOp("remove_pauses", { min_len: 5.0 }, "Паузи скорочено");
    case "cut-fillers": {
      const list = prompt("Слова-паразити через кому", "ееее, еее, ммм, ну, типу, короче, um, uh");
      if (!list) return;
      const set = new Set(list.split(",").map((x) => normWord(x.trim())).filter(Boolean));
      const idx = T().filter((t) => t.k === "w" && set.has(normWord(t.w))).map((t) => t.i);
      if (!idx.length) return toast("Нічого не знайдено");
      if (confirm(`Вирізати ${idx.length} слів(а)? (Ctrl+Z — скасувати)`)) editOp("cut_tokens", { indices: idx }, "Вирізано " + idx.length + " слів");
      return;
    }
    case "export-srt": return dl("/api/export/srt?scope=edit");
    case "export-txt": return dl("/api/export/txt?scope=edit");
    case "export-edl": return dl("/api/export/edl");
    case "export-src-srt": return dl("/api/export/srt?scope=source");
    case "reset-edit": if (confirm("Скинути всі правки монтажу до оригіналу? (можна скасувати Ctrl+Z)")) editOp("reset", {}, "Монтаж скинуто"); return;
  }
}

boot();
