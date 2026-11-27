// DaVinci Director panel.  The text is always rendered from the current Resolve timeline (the server's
// "view"); every edit goes to the server, changes the real timeline, and the new text comes back.

import { $, $$, keep } from "./util.js";
import { state, on, emit, setTranscripts } from "./store.js";
import { api, listen } from "./api.js";
import { act, toast, showMenu, setupWidgets } from "./ui.js";
import { Editor } from "./editor.js";
import { Actions } from "./actions.js";
import { Dialogs } from "./dialogs.js";
import { StoriesPanel } from "./stories.js";
import { setupKeys } from "./keys.js";
import * as P from "./panels.js";

const COMPONENTS_CHECK_DELAY_MS = 1500;

const editor = new Editor($("#editor"));
const actions = new Actions(editor);
editor.actions = actions;
const dialogs = new Dialogs({ showStory: (uid) => { stories.active = uid; P.showTab("stories"); stories.render(); } });
const stories = new StoriesPanel($("#tab-stories"), actions, dialogs);
const clock = new P.PlayheadClock((frame, scroll) => showPlayhead(frame, scroll));

function showPlayhead(frame, scroll = false) {
  state.playhead = frame;
  P.markPlayhead(frame, scroll);
  editor.markPlayhead(frame, scroll);
}

function runSearch(jump) {
  const inCut = $("#searchCut").checked;
  if (inCut && !state.showCut) setShowCut(true);
  editor.search($("#searchInput").value, inCut, jump);
  showSearchCount();
}

function showSearchCount() {
  const n = editor.hits.length;
  $("#searchCount").textContent = !$("#searchInput").value.trim() ? "" : n ? `${editor.hitIdx + 1} / ${n}` : "0";
}

function stepHit(d) {
  editor.stepHit(d);
  showSearchCount();
}

function setShowCut(on) {
  state.showCut = on;
  keep("dd.showCut", on);
  renderText();
}

// ------------------------------------------------------------ what follows from which change

function renderText() {
  P.renderLegend();
  P.renderOutline();
  P.renderStrip();
  P.renderTrackInfo();
  editor.render();
  showPlayhead(state.playhead);
  if ($("#searchInput").value) runSearch(false);
}

on("selection", (p) => {
  const frames = p && p.kind === "live" ? editor.frames() : null;
  P.markStripSelection(frames);
  P.renderSelectionStatus(p, frames);
});
on("transcripts", () => P.renderSpeakers());
on("topics", renderText);
on("undo", P.renderUndo);
on("link", P.renderLink);

async function loadState() {
  const st = await api("state");
  Object.assign(state, { view: st.view, link: st.link, undo: st.undo, stories: st.stories || [], topics: st.topics || [],
    settings: st.settings, playhead: st.link.playhead });
  setTranscripts(st.transcripts, st.view.tv);
  P.renderLink();
  P.renderUndo();
  renderText();
  stories.render();
  (st.jobs || []).forEach(showJob);
}

function showJob(job) {
  for (const [msg, kind] of P.renderJob(job)) toast(msg, kind);
}

async function onMessage(m) {
  if (m.type === "view") {
    if (m.view.tv !== state.tv) {
      const t = await api("transcripts");
      setTranscripts(t.transcripts, t.tv);
    }
    const changed = m.view.v !== state.view.v;
    state.view = m.view;
    if (m.undo) { state.undo = m.undo; emit("undo"); }
    if (changed) { renderText(); stories.render(); }
  } else if (m.type === "playhead") {
    clock.reported(m.frame);
  } else if (m.type === "link") {
    state.link = m.link;
    emit("link");
  } else if (m.type === "job") {
    showJob(m.job);
  }
}

// ------------------------------------------------------------ controls around the text

function moreMenu(ev) {
  const r = ev.currentTarget.getBoundingClientRect();
  showMenu(r.left - 180, r.bottom + 4, [
    ["Субтитри монтажу (.srt)", "", () => { location.href = "/api/export/srt"; }],
    ["Текст монтажу (.txt)", "", () => { location.href = "/api/export/txt"; }],
    ["Схема відповіді Claude (.json)", "", () => { location.href = "/api/export/schema"; }],
    null,
    ["Прибрати розриви на доріжці", "", () => actions.edit("Розриви", { op: "close_gaps" })],
    ["Компоненти й моделі…", "", () => dialogs.components()],
    ["Перемкнути таймлайн…", "", () => dialogs.timelines()],
  ]);
}

function setupControls() {
  $("#btnUndo").onclick = () => actions.undo();
  $("#btnRedo").onclick = () => actions.redo();
  $("#btnTranscribe").onclick = () => dialogs.transcribe();
  $("#btnExport").onclick = () => dialogs.exportPrompt();
  $("#btnImport").onclick = () => dialogs.importAnswer();
  $("#btnRefresh").onclick = () => act("Оновлення", () => api("refresh", {}));
  $("#btnMore").onclick = moreMenu;
  $("#btnFollow").classList.toggle("on", state.follow);
  $("#btnFollow").onclick = () => {
    state.follow = !state.follow;
    $("#btnFollow").classList.toggle("on", state.follow);
    if (state.follow) showPlayhead(state.playhead, true);
  };
  $("#trackSel").onchange = (e) => act("Доріжка", () => api("track", { track: e.target.value }));
  $("#statusJob").onclick = (e) => { const c = e.target.closest("[data-cancel]"); if (c) api("cancel", { id: c.dataset.cancel }); };

  $$(".tab").forEach((t) => (t.onclick = () => P.showTab(t.dataset.tab)));
  $("#tglOutline").onclick = () => { P.panels.outline = !P.panels.outline; P.applyPanels(); };
  $$("[data-side]").forEach((b) => (b.onclick = () => {
    P.panels.side = P.panels.side === b.dataset.side ? null : b.dataset.side;  // the active switch hides the panel
    P.applyPanels();
  }));
  P.applyPanels();

  $("#searchInput").oninput = () => { editor.hitIdx = 0; runSearch(true); };
  $("#searchInput").onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); stepHit(e.shiftKey ? -1 : 1); } };
  $("#searchCut").onchange = () => runSearch(true);
  $("#searchPrev").onclick = () => stepHit(-1);
  $("#searchNext").onclick = () => stepHit(1);

  $("#legend").onclick = (ev) => {
    if (ev.target.closest("#btnHelp")) { dialogs.help(); return; }
    const b = ev.target.closest(".lg[data-sid]");
    if (b) dialogs.renameSpeaker(b.dataset.m, b.dataset.sid);
  };
  $("#legend").onchange = (ev) => { if (ev.target.id === "showCut") setShowCut(ev.target.checked); };
  $("#outline").onclick = (ev) => {
    const row = ev.target.closest(".ol-row");
    if (!row) return;
    editor.selectBlock(+row.dataset.k);
    actions.goto({ frame: state.view.blocks[+row.dataset.k].s });
  };
  $("#overview").onclick = (ev) => { const f = P.stripFrameAt(ev.clientX); if (f != null) actions.goto({ frame: f }); };
  $("#tab-speakers").onchange = (ev) => {
    const i = ev.target.closest("input[data-sid]");
    if (i) act("Мовець", () => api("speaker/rename", { m: i.dataset.m, sid: i.dataset.sid, name: i.value }));
  };
}

setupWidgets();
setupControls();
setupKeys(editor, actions, dialogs);
listen({ open: () => loadState().catch((e) => toast(e.message, "err")), lost: P.linkLost, message: onMessage });
// checked once in the background, so «Транскрибувати» opens at once
setTimeout(() => api("components").then((c) => { state.comp = c; }).catch(() => {}), COMPONENTS_CHECK_DELAY_MS);
