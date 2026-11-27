// Edits of the montage.  Each one goes to the server, which changes the real timeline in Resolve; the
// new text then arrives as a "view" event.  Actions read the editor's selection, nothing else of it.

import { api } from "./api.js";
import { state, emit } from "./store.js";
import { act, toast, status } from "./ui.js";
import { nWords, dur } from "./util.js";

const STOP_WAIT_MS = 150, STOP_TRIES = 14;  // waiting for Resolve to stop before playing a piece
const OVERRUN_FRAMES_S = 3;                 // after a piece: still "ours" this many seconds past its end

export class Actions {
  constructor(editor) {
    this.editor = editor;
    this.previewTimer = 0;
  }

  edit(label, body) {
    return act(label, () => api("edit", Object.assign({ v: state.view.v }, body))).then((r) => {
      if (r && r.undo) { state.undo = r.undo; emit("undo"); }
      return r;
    });
  }

  goto(body) {
    return api("goto", body).catch((e) => toast(e.message, "err"));
  }

  live(message) {
    const p = this.editor.payload();
    if (!p || p.kind !== "live") { toast(message); return null; }
    return p;
  }

  // Del: cut what is on the timeline, or bring back what was cut
  cutOrRestore() {
    const p = this.editor.payload();
    if (!p) return toast("Виділіть слова: клік, перетягування або Shift+клік.");
    const body = p.kind === "cut" ? { op: "restore", k: p.k, i0: p.i0, i1: p.i1 } : { op: "delete", sel: p.sel };
    return this.edit(p.kind === "cut" ? "Відновлення" : "Вирізання", body).then(() => this.editor.clear());
  }

  async copy(cutToo) {
    const p = this.live("Виділіть слова, що є на таймлайні.");
    if (!p) return;
    const r = await act("Копіювання", () => api("edit", { op: "copy", sel: p.sel }));
    if (!r) return;
    state.undo.clipboard = r.res.clips;
    if (cutToo) {
      await this.edit("Вирізання", { op: "delete", sel: p.sel });
      this.editor.clear();
      return;
    }
    toast(`Скопійовано ${nWords(p.n)}. Ctrl+V вставляє перед виділеним словом.`);
    this.editor.showTools();
  }

  // Ctrl + drag: a copy straight to that place
  async copyTo(sel, pos) {
    const r = await act("Копіювання", () => api("edit", { op: "copy", sel }));
    if (!r) return;
    state.undo.clipboard = r.res.clips;
    await this.edit("Копія", { op: "paste", pos });
    this.editor.clear();
  }

  paste(pos = this.editor.caretPos() || { b: null, end: true }) {
    return this.edit("Вставлення", { op: "paste", pos });
  }

  duplicate() {
    const p = this.live("Виділіть слова для дублювання.");
    return p && this.edit("Дублювання", { op: "duplicate", sel: p.sel });
  }

  move(sel, pos) {
    return this.edit("Переміщення", { op: "move", sel, pos }).then(() => this.editor.clear());
  }

  undo() { return this.edit("Скасування", { op: "undo" }); }
  redo() { return this.edit("Повтор", { op: "redo" }); }

  correct(key, i0, i1, text) {
    const single = i1 - i0 === 1 && !text.includes(" ");
    return act("Виправлення", () => (single ? api("word", { m: key, i: i0, text }) : api("words", { m: key, i0, i1, text })));
  }

  // ---- who said the selected words

  speakers() {
    const r = this.editor.wordRanges();
    return r.length ? state.tr[r[0].m].speakers : null;  // of the file the selection starts in
  }

  async setSpeaker(sid) {
    const ranges = this.editor.wordRanges();
    if (!ranges.length) return toast("Виділіть слова, які сказав інший мовець.");
    const list = this.speakers();
    const n = ranges.reduce((a, r) => a + r.i1 - r.i0, 0);
    const name = (list.find((x) => x.id === sid) || { name: "новий мовець" }).name;
    if (await act("Мовець", () => api("words/speaker", { ranges, sid }))) status(`${nWords(n)} → ${name}`);
  }

  speakerByNumber(n) {
    const list = this.speakers();
    if (!list) return toast("Виділіть слова, які сказав інший мовець.");
    if (n > list.length) return toast(`Мовця ${n} немає. Новий мовець — кнопка «+» на панелі виділення.`);
    return this.setSpeaker(list[n - 1].id);
  }

  // ---- playing the selection in Resolve

  // Space in Resolve toggles playback, so it is never pressed blindly: whether Resolve plays is known from
  // the playhead the script reports.  Playing already -> stop first; at the end -> stop our piece only.
  async play() {
    const p = this.live("Виділіть шматок, який хочете переглянути.");
    if (!p) return;
    const py = window.pywebview && window.pywebview.api;
    clearTimeout(this.previewTimer);
    try {
      if (py) await this.playInResolve(py, p);
      else await api("preview/start", { seconds: (await api("preview", { sel: p.sel })).seconds });  // a browser
    } catch (e) {
      toast(e.message, "err");
    }
  }

  async playInResolve(py, p) {
    const playing = async () => (await api("playback", {})).playing;
    if (await playing()) {
      await py.space(true);  // stop what is playing (focus comes back here)
      for (let i = 0; i < STOP_TRIES && (await playing()); i++) await new Promise((ok) => setTimeout(ok, STOP_WAIT_MS));
    }
    const r = await api("preview", { sel: p.sel });  // In/Out + playhead at the start of the selection
    await py.space(false);  // play; Resolve stays in front
    const fps = state.view.fps, ms = (r.f2 - r.f1) / fps * 1000;
    status(`Відтворення в Resolve: ${dur((r.f2 - r.f1) / fps)}`);
    this.previewTimer = setTimeout(() => this.stopAfter(py, r, ms), ms);
  }

  // still running through our piece -> stop it; somewhere else entirely -> the editor took over
  async stopAfter(py, r, ms) {
    const st = await api("playback", {}).catch(() => null);
    if (!st || st.frame == null) return;
    const ours = st.frame > r.f1 && st.frame <= r.f2 + state.view.fps * OVERRUN_FRAMES_S;
    if (ours && (st.playing || ms < 1500)) await py.space(true);
  }

  // ---- the context menu of the text

  menu(p) {
    if (p && p.kind === "cut") return [["Повернути у відео", "Del", () => this.cutOrRestore()]];
    if (!p) return [["Вставити в кінець", "", () => this.paste({ b: null, end: true }), !state.undo.clipboard]];
    const items = [["Програти в Resolve", "Space", () => this.play()], null,
      ["Вирізати з відео", "Del", () => this.cutOrRestore()],
      ["Копіювати", "Ctrl+C", () => this.copy(false)],
      ["Вирізати в буфер", "Ctrl+X", () => this.copy(true)],
      ["Дублювати поруч", "Ctrl+D", () => this.duplicate()],
      ["Вставити скопійоване перед цим словом", "Ctrl+V", () => this.paste(), !state.undo.clipboard]];
    const list = this.speakers();
    if (list) {
      items.push(null, [p.n > 1 ? "Виправити текст" : "Виправити слово", "F2", () => this.editor.correctSelection()],
        { label: "Виділені слова сказав:" },
        ...list.map((s, i) => [s.name, i < 9 ? String(i + 1) : "", () => this.setSpeaker(s.id)]),
        ["Новий мовець", "", () => this.setSpeaker("new")]);
    }
    return items;
  }
}
