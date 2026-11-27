// Everything around the text: connection and track on top, speaker legend, structure (outline), the
// timeline strip with the playhead, the speakers tab, the status bar, panel switches.

import { $, $$, html, tc, dur, nWords, nClips, keep, remember } from "./util.js";
import { state, speaker } from "./store.js";

const GREY_CLIP = "#6b6b70";
const OUTLINE_WORDS = 9;
const SEL_HINT = "Клік — слово · перетягніть — шматок · Shift+клік — до цього слова · 2×клік — речення · 3×клік — абзац · ? — довідка";

// ------------------------------------------------------------ top: connection, track, undo, job

export function renderLink() {
  const L = state.link || {}, info = L.info || {}, check = L.check || [];
  $("#connDot").className = "dot" + (L.connected ? (check.length ? " warn" : " on") : "");
  $("#connText").textContent = L.connected
    ? [info.project, info.tl].filter(Boolean).join(" / ") || "Resolve підключено"
    : "Resolve не підключено";
  $("#conn").title = L.connected
    ? `${info.product || "DaVinci Resolve"} ${info.version || ""}` + (check.length ? "\nПеревірка стану: " + check.join("; ") : "")
    : "Запустіть у Resolve: Workspace > Scripts > DaVinci Director";
  const tracks = L.tracks && L.tracks.length ? L.tracks : [L.track || "video1"];
  const label = (k) => (k.startsWith("video") ? "V" : "A") + k.replace(/\D/g, "");
  $("#trackSel").innerHTML = String(html`${tracks.map((k) => html`<option value="${k}" ${k === L.track ? "selected" : ""}>${label(k)}</option>`)}`);
  renderTrackInfo();
}

export function linkLost() {
  $("#connDot").className = "dot";
  $("#connText").textContent = "Немає зв'язку із застосунком";
}

// under the big timecode: «Стрім 14.11» / «12 кліпів · 1:42:10»
export function renderTrackInfo() {
  const blocks = state.view.blocks || [];
  $("#tlName").textContent = state.view.tl || "Таймлайн";
  $("#trkSub").textContent = blocks.length
    ? `${nClips(blocks.length)} · ${dur((blocks[blocks.length - 1].e - blocks[0].s) / state.view.fps)}`
    : "немає кліпів";
}

export function renderUndo() {
  const u = state.undo || {};
  $("#btnUndo").disabled = !u.undo;
  $("#btnRedo").disabled = !u.redo;
  $("#btnUndo").title = u.undo ? `Скасувати: ${u.undo} (Ctrl+Z)` : "Скасувати (Ctrl+Z)";
  $("#btnRedo").title = u.redo ? `Повторити: ${u.redo} (Ctrl+Shift+Z)` : "Повторити (Ctrl+Shift+Z)";
}

// a running job in the status bar; its end as a notification (returned for the caller to show)
export function renderJob(job) {
  const box = $("#statusJob");
  const running = job.status === "running";
  if (job.kind === "transcribe") $("#btnTranscribe").disabled = running;
  if (running) {
    const pct = job.progress * 100;
    box.innerHTML = String(html`${job.label}: ${job.message || ""} <span class="progress"><i style="width:${pct.toFixed(1)}%"></i></span> ${pct.toFixed(0)}% <button class="mini" data-cancel="${job.id}" title="Скасувати">&times;</button>`);
    return [];
  }
  box.textContent = "";
  const done = job.status === "done" ? [[job.label + ": готово", "ok"]] : job.status === "error" ? [[job.label + ": " + job.message, "err"]] : [];
  return done.concat((job.log || []).map((m) => [m, ""]));
}

// ------------------------------------------------------------ speaker legend above the text

export function renderLegend() {
  const box = $("#legend");
  const keys = Object.keys(state.tr).filter((k) => state.view.blocks.some((b) => b.m === k));
  if (!keys.length) { box.replaceChildren(); return; }
  box.innerHTML = String(html`<div class="lg-list">${keys.map((k) => state.tr[k].speakers.map((s) =>
    html`<button class="lg" data-m="${k}" data-sid="${s.id}" title="${s.name} — клік: перейменувати"><i style="background:${s.color}"></i>${s.name}</button>`))}</div>
    <label class="chk lg-cut" title="Показувати вирізані слова на їхніх місцях"><input type="checkbox" id="showCut" ${state.showCut ? "checked" : ""}> вирізане</label>
    <button class="lg lg-help" id="btnHelp" title="Як монтувати текстом (?)">?</button>`);
}

// ------------------------------------------------------------ structure: clips in timeline order

function firstWords(b) {
  if (!b.m || !state.tr[b.m]) return html`<i>${b.name}</i>`;
  const words = state.tr[b.m].words.slice(b.i0, Math.min(b.i1, b.i0 + OUTLINE_WORDS));
  if (!words.length) return html`<i>без мови</i>`;
  const runs = [];
  for (const w of words) {
    if (!runs.length || runs[runs.length - 1].sp !== w[3]) runs.push({ sp: w[3], text: [] });
    runs[runs.length - 1].text.push(w[2]);
  }
  return html`${runs.map((r) => html`<span style="color:${speaker(b.m, r.sp).color}">${r.text.join(" ")}</span> `)}${b.i1 - b.i0 > OUTLINE_WORDS ? "…" : ""}`;
}

export function renderOutline() {
  const box = $("#outline"), blocks = state.view.blocks || [], fps = state.view.fps;
  if (!blocks.length) {
    box.innerHTML = `<div class="hint ol-empty">Кліпи таймлайна з'являться тут.</div>`;
    $("#outlineSum").textContent = "";
    return;
  }
  const longest = Math.max(...blocks.map((b) => b.e - b.s));
  $("#outlineSum").textContent = `${nClips(blocks.length)} · ${dur((blocks[blocks.length - 1].e - blocks[0].s) / fps)}`;
  box.innerHTML = String(html`${blocks.map((b, k) => html`<div class="ol-row" data-k="${k}">
      <div class="ol-top"><span class="ol-n">${k + 1}</span><span class="ol-tc">${dur((b.s - state.view.start) / fps)}</span><span class="ol-d">${dur((b.e - b.s) / fps)}</span></div>
      <div class="ol-bar"><i style="width:${Math.max(2, (b.e - b.s) / longest * 100)}%"></i></div>
      <div class="ol-txt">${firstWords(b)}</div></div>`)}`);
}

// ------------------------------------------------------------ timeline strip at the bottom

function stripSpan() {
  const blocks = state.view.blocks || [];
  if (!blocks.length) return null;
  return { t0: blocks[0].s, span: Math.max(1, blocks[blocks.length - 1].e - blocks[0].s) };
}
const pct = (f, s) => Math.min(100, Math.max(0, (f - s.t0) / s.span * 100));

export function renderStrip() {
  const box = $("#overview"), s = stripSpan();
  if (!s) { box.replaceChildren(); return; }
  box.innerHTML = String(html`${state.view.blocks.map((b, k) => {
    const color = b.m && state.tr[b.m] && b.i1 > b.i0 ? speaker(b.m, state.tr[b.m].words[b.i0][3]).color : GREY_CLIP;
    return html`<i class="ov-clip" data-k="${k}" style="left:${pct(b.s, s)}%;width:${(b.e - b.s) / s.span * 100}%;--c:${color}" title="${k + 1} · ${tc(b.s, state.view.fps)}"></i>`;
  })}<i class="ov-sel hidden" id="ovSel"></i><i class="ov-ph" id="ovPh"></i>`);
}

export function stripFrameAt(clientX) {
  const box = $("#overview"), s = stripSpan(), r = box.getBoundingClientRect();
  return s ? Math.round(s.t0 + (clientX - r.left) / r.width * s.span) : null;
}

export function markStripSelection(frames) {
  const el = $("#ovSel"), s = stripSpan();
  if (!el || !s) return;
  el.classList.toggle("hidden", !frames);
  if (!frames) return;
  el.style.left = pct(frames[0], s) + "%";
  el.style.width = Math.max(0.2, (frames[1] - frames[0]) / s.span * 100) + "%";
}

// timecode, playhead on the strip, current clip in the structure
export function markPlayhead(frame, scroll) {
  $("#tcBig").textContent = frame == null || !state.view.fps ? "--:--:--:--" : tc(frame, state.view.fps);
  const ph = $("#ovPh"), s = stripSpan();
  if (ph && s && frame != null) ph.style.left = pct(frame, s) + "%";
  const k = frame == null ? -1 : (state.view.blocks || []).findIndex((b) => b.s <= frame && frame < b.e);
  const row = k < 0 ? null : $(`.ol-row[data-k="${k}"]`);
  if (row && row.classList.contains("cur")) return;
  $$(".ol-row.cur").forEach((e) => e.classList.remove("cur"));
  if (!row) return;
  row.classList.add("cur");
  if (scroll && state.follow) row.scrollIntoView({ block: "nearest" });
}

// ------------------------------------------------------------ status bar

export function renderSelectionStatus(p, frames) {
  $("#statusSel").textContent = !p ? SEL_HINT
    : p.kind === "cut" ? `Вирізане: ${nWords(p.n)} — Del повертає у відео`
    : `Виділено ${nWords(p.n)} · ${dur((frames[1] - frames[0]) / state.view.fps)} — Space: програти в Resolve, Del: вирізати`;
}

// ------------------------------------------------------------ speakers tab

export function renderSpeakers() {
  const box = $("#tab-speakers");
  const keys = Object.keys(state.tr);
  if (!keys.length) {
    box.innerHTML = `<div class="sec hint">Мовці з'являться після транскрибування. Імена можна змінити тут або через ПКМ на імені в тексті.</div>`;
    return;
  }
  box.innerHTML = String(html`${keys.map((k) => {
    const t = state.tr[k];
    const counts = new Array(t.speakers.length).fill(0);
    for (const w of t.words) counts[w[3]] = (counts[w[3]] || 0) + 1;
    return html`<div class="speakers-media"><div class="file" title="${t.path}">${t.tag} · ${t.path.split(/[\\/]/).pop()}${t.lang ? " · " + t.lang : ""}</div>
      ${t.speakers.map((s, i) => html`<div class="spk-row"><span class="swatch" style="background:${s.color}"></span>
        <input type="text" value="${s.name}" data-m="${k}" data-sid="${s.id}"><span class="n">${counts[i] || 0} сл.</span></div>`)}</div>`;
  })}`);
}

// ------------------------------------------------------------ panel switches (remembered)

export const panels = remember("dd.panels", { outline: true, side: "stories" });

export function applyPanels() {
  $("#pnlOutline").classList.toggle("hidden", !panels.outline);
  $("#tglOutline").classList.toggle("on", panels.outline);
  $("#pnlSide").classList.toggle("hidden", !panels.side);
  $$("[data-side]").forEach((b) => b.classList.toggle("on", panels.side === b.dataset.side));
  if (panels.side) showTab(panels.side);
  $("#tglText").classList.add("on");
  keep("dd.panels", panels);
}

export function showTab(name) {
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  $$(".tab-body").forEach((b) => b.classList.toggle("hidden", b.id !== "tab-" + name));
  if (panels.side !== name) { panels.side = name; applyPanels(); }
}

// ------------------------------------------------------------ playhead between Resolve's reports

// Resolve reports the playhead a few times a second; while it plays, the panel advances it on its own
// (fps x elapsed time) between reports, so the timecode and the sounding word keep up.
export class PlayheadClock {
  constructor(onFrame) {
    this.onFrame = onFrame;
    this.report = null;
    this.playing = false;
    this.timer = 0;
  }

  reported(frame) {
    const now = performance.now(), fps = state.view.fps || 25, prev = this.report;
    this.report = { f: frame, t: now };
    // playing = it moved forward at about real-time speed since the previous report
    this.playing = !!prev && frame > prev.f && now - prev.t < 1500 &&
      Math.abs((frame - prev.f) / fps - (now - prev.t) / 1000) < 0.35 + (now - prev.t) / 2000;
    this.onFrame(frame, true);
    if (this.playing && !this.timer) this.timer = setTimeout(() => this.tick(), 40);
  }

  tick() {
    this.timer = 0;
    const r = this.report;
    if (!this.playing || !r) return;
    const dt = (performance.now() - r.t) / 1000;
    if (dt > 0.9) { this.playing = false; this.onFrame(r.f, true); return; }  // stopped: no more reports
    this.onFrame(r.f + Math.floor(dt * (state.view.fps || 25)), true);
    this.timer = setTimeout(() => this.tick(), 40);
  }
}
