// The stories tab: variants from the language model, their scenes, applying them to the timeline.

import { $$, html, dur } from "./util.js";
import { state } from "./store.js";
import { api } from "./api.js";
import { act, toast, modal, note, showMenu } from "./ui.js";

const PAUSE_BETWEEN_FRAGMENTS = 0.3;  // s, added per fragment when estimating a scene's length
const QUOTE_CHARS = 220;
const MAX_WARNINGS = 6;

export class StoriesPanel {
  constructor(box, actions, dialogs) {
    this.box = box;
    this.actions = actions;
    this.dialogs = dialogs;
    this.active = null;
    this.dragged = null;
    this.listen();
  }

  story(uid = this.active) { return (state.stories || []).find((s) => s.uid === uid); }

  // a fragment id (A12) -> where it is and whether a clip uses it now
  fragment(fid) {
    const m = /^([A-Z]+)0*(\d+)$/.exec(fid || "");
    const key = m && Object.keys(state.tr).find((k) => state.tr[k].tag === m[1]);
    const t = key && state.tr[key], n = m ? +m[2] - 1 : -1;
    if (!t || !t.frags[n]) return null;
    const [a, z] = t.frags[n];
    return { t0: t.words[a][0], t1: t.words[z - 1][1], text: t.words.slice(a, z).map((w) => w[2]).join(" "),
      onTimeline: state.view.blocks.some((b) => b.m === key && b.i0 < z && b.i1 > a) };
  }

  sceneSeconds(sc) {
    return sc.fragments.map((f) => this.fragment(f)).filter(Boolean)
      .reduce((s, f) => s + f.t1 - f.t0 + PAUSE_BETWEEN_FRAGMENTS, 0);
  }

  render() {
    const list = state.stories || [];
    if (!list.length) { this.box.innerHTML = String(this.howTo()); return; }
    if (!this.story()) this.active = list[0].uid;
    this.box.innerHTML = String(html`<div class="variants">${list.map((s) => this.variantRow(s))}</div>${this.detail(this.story())}`);
  }

  howTo() {
    return html`<div class="sec hint">
      <div class="sec-title">Пошук сюжетів через Claude</div>
      <p>1. Транскрибуйте таймлайн.</p>
      <p>2. «Експорт для Claude» — опишіть, що потрібно (тривалість, кількість варіантів, побажання), і скопіюйте готовий запит у чат мовної моделі. Довгий запис автоматично ділиться на частини й проходить у два етапи.</p>
      <p>3. «Імпорт сюжетів» — завантажте відповідь. Тут з'являться варіанти зі сценами, рекомендаціями та закадровим текстом.</p>
      <p>4. Застосуйте варіант до таймлайна Resolve повністю або сцена за сценою.</p>
      <div class="actions"><button class="btn" data-a="export">Експорт для Claude</button><button class="btn" data-a="import">Імпорт сюжетів</button></div></div>`;
  }

  variantRow(s) {
    const total = s.scenes.filter((x) => x.enabled).reduce((a, x) => a + this.sceneSeconds(x), 0);
    return html`<div class="variant ${s.uid === this.active ? "active" : ""}" data-story="${s.uid}">
      <span class="t">${s.title}</span><span class="meta">${s.scenes.length} сц. · ${dur(total)}</span>
      ${s.logline ? html`<span class="log">${s.logline}</span>` : ""}</div>`;
  }

  detail(s) {
    const warns = s.warnings || [];
    const notes = [].concat(s.editing_notes || []);
    const line = (label, text) => (text ? html`<p><span class="lbl">${label}: </span>${text}</p>` : "");
    return html`<div class="story-head"><h3>${s.title}</h3>
      ${s.concept ? html`<div class="concept">${s.concept}</div>` : ""}
      ${line("Чому це дивитимуться", s.why_watch)}${line("Структура", s.structure)}${line("Ризики", s.risks)}
      ${s.verdict ? html`<div class="hint">Оцінка матеріалу: ${s.verdict}</div>` : ""}
      ${s.target_duration_sec ? html`<div class="hint">Цільова тривалість: ${dur(s.target_duration_sec)}</div>` : ""}
      ${warns.length ? html`<div class="warns">${warns.slice(0, MAX_WARNINGS).map((w, i) => html`${i ? html`<br>` : ""}${w}`)}${warns.length > MAX_WARNINGS ? html`<br>…` : ""}</div>` : ""}
      <div class="actions" style="margin-top:10px">
        <button class="btn primary" data-a="apply-new" title="Створює новий таймлайн у проєкті Resolve з кліпами в порядку сцен">Застосувати: новий таймлайн</button>
        <button class="btn" data-a="apply-replace" title="Перебудовує доріжку поточного таймлайна">Замінити монтаж</button>
        <button class="btn ghost" data-a="story-more">Ще</button></div></div>
      ${s.scenes.map((sc, i) => this.scene(sc, i))}
      ${notes.length || s.music_and_sound || s.ending ? html`<div class="sec hint"><div class="sec-title">Рекомендації</div>
        ${notes.map((n) => html`<p>${n}</p>`)}
        ${s.music_and_sound ? html`<p><span class="lbl">Музика і звук: </span>${s.music_and_sound}</p>` : ""}
        ${s.ending ? html`<p><span class="lbl">Фінал: </span>${s.ending}</p>` : ""}</div>` : ""}`;
  }

  scene(sc, i) {
    const first = this.fragment(sc.fragments[0]);
    const last = this.fragment(sc.fragments[sc.fragments.length - 1]) || first;
    const range = first ? `${dur(first.t0)}–${dur(last.t1)}` : "";
    const line = (label, text) => (text ? html`<p><span class="lbl">${label}: </span>${text}</p>` : "");
    const vo = sc.voiceover;
    return html`<div class="scene ${sc.enabled ? "" : "off"}" data-scene="${sc.uid}" draggable="true">
      <div class="grip" title="Перетягніть, щоб змінити порядок"><svg><use href="#i-grip"/></svg></div>
      <div class="body">
        <div class="scene-top"><span class="scene-n">${i + 1}</span><span class="scene-title">${sc.title}</span><span class="role" data-role="${sc.role}">${sc.role_label || sc.role}</span></div>
        <div class="hint">${range ? "джерело " + range + " · " : ""}≈ ${dur(this.sceneSeconds(sc))}</div>
        ${sc.summary ? html`<p>${sc.summary}</p>` : ""}
        ${line("Роль", sc.role_note)}${line("Монтаж", sc.edit_notes)}${line("На екрані має бути", sc.visual)}${line("Контекст", sc.context)}${line("Перехід", sc.transition)}
        <div class="frags">${sc.fragments.length ? sc.fragments.map((f) => this.fragmentChip(f)) : html`<span class="hint">без фрагментів</span>`}</div>
        ${first ? html`<div class="quote">${first.text.slice(0, QUOTE_CHARS)}</div>` : ""}
        <div class="vo"><span class="lbl">Закадровий текст${vo && vo.placement ? " (" + vo.placement + ")" : ""}</span>
          <textarea data-vo="${sc.uid}" placeholder="немає">${vo ? vo.text : ""}</textarea></div>
        <div class="scene-actions">
          <label class="chk"><input type="checkbox" data-en="${sc.uid}" ${sc.enabled ? "checked" : ""}> у варіанті</label>
          <button class="btn sm" data-a="scene-go" data-sc="${sc.uid}" title="Показати перший фрагмент сцени в Resolve"><svg><use href="#i-play"/></svg>Перейти</button>
          <button class="btn sm" data-a="scene-ph" data-sc="${sc.uid}" title="Вставити сцену на таймлайн у позицію плейхеда (на найближчу межу кліпа)">У плейхед</button>
          <button class="btn sm" data-a="scene-end" data-sc="${sc.uid}">У кінець</button>
        </div></div></div>`;
  }

  fragmentChip(fid) {
    const f = this.fragment(fid);
    const title = f ? f.text.slice(0, 200) + (f.onTimeline ? "" : "\n(зараз не на таймлайні)") : "не знайдено";
    return html`<span class="frag ${f && f.onTimeline ? "" : "off"}" data-fid="${fid}" title="${title}">${fid}</span>`;
  }

  patch(patch, rerender = true) {
    return api("story/update", { uid: this.active, patch })
      .then((r) => { state.stories = r.stories; if (rerender) this.render(); })
      .catch((e) => toast(e.message, "err"));
  }

  // ------------------------------------------------------------ events

  listen() {
    this.box.addEventListener("click", (ev) => this.onClick(ev));
    this.box.addEventListener("change", (ev) => this.onChange(ev));
    this.box.addEventListener("dragstart", (ev) => this.onDragStart(ev));
    this.box.addEventListener("dragover", (ev) => this.onDragOver(ev));
    this.box.addEventListener("drop", (ev) => this.onDrop(ev));
    this.box.addEventListener("dragend", () => this.endDrag());
  }

  onClick(ev) {
    const variant = ev.target.closest("[data-story]");
    if (variant) { this.active = variant.dataset.story; this.render(); return; }
    const frag = ev.target.closest(".frag");
    if (frag) { this.actions.goto({ fid: frag.dataset.fid }); return; }
    const b = ev.target.closest("[data-a]");
    const handler = b && this.commands()[b.dataset.a];
    if (handler) handler(b, ev);
  }

  commands() {
    const uid = this.active;
    const scene = (b) => this.story().scenes.find((x) => x.uid === b.dataset.sc);
    const placeScene = (b, where) => act("Вставлення сцени", () => api("scene/apply", { uid, scene: b.dataset.sc, where }));
    return {
      export: () => this.dialogs.exportPrompt(),
      import: () => this.dialogs.importAnswer(),
      "apply-new": () => act("Застосування сюжету", () => api("story/apply", { uid, mode: "new" }))
        .then((r) => r && toast("У Resolve створено новий таймлайн з монтажем варіанта", "ok")),
      "apply-replace": () => modal("Замінити монтаж?",
        html`<p class="lead">Усі кліпи доріжки <b>${state.view.track}</b> поточного таймлайна буде замінено сценами варіанта. Інші доріжки не змінюються.</p>${note("Скасувати можна через Ctrl+Z у Resolve.")}`,
        [["Скасувати"], ["Замінити", "primary", () => act("Застосування сюжету", () => api("story/apply", { uid, mode: "replace" }))]]),
      "story-more": (b, ev) => showMenu(ev.clientX, ev.clientY, [
        ["Завантажити варіант як JSON", "", () => { location.href = "/api/export/story?uid=" + uid; }],
        ["Увімкнути всі сцени", "", () => this.patch({ scenes: this.story().scenes.map((s) => ({ uid: s.uid, enabled: true })) })],
        null, ["Видалити варіант", "", () => this.patch({ delete: true })]]),
      "scene-go": (b) => { const sc = scene(b); if (sc && sc.fragments[0]) this.actions.goto({ fid: sc.fragments[0] }); },
      "scene-ph": (b) => placeScene(b, "playhead"),
      "scene-end": (b) => placeScene(b, "end"),
    };
  }

  onChange(ev) {
    const enabled = ev.target.closest("[data-en]");
    if (enabled) this.patch({ scenes: [{ uid: enabled.dataset.en, enabled: enabled.checked }] });
    const vo = ev.target.closest("[data-vo]");
    if (vo) this.patch({ scenes: [{ uid: vo.dataset.vo, voiceover: vo.value }] }, false);
  }

  // scenes are reordered by dragging
  onDragStart(ev) {
    const sc = ev.target.closest(".scene");
    if (!sc || ev.target.closest("textarea")) return;
    this.dragged = sc.dataset.scene;
    sc.classList.add("dragging");
    ev.dataTransfer.effectAllowed = "move";
    ev.dataTransfer.setData("text/plain", this.dragged);
  }

  onDragOver(ev) {
    if (!this.dragged) return;
    ev.preventDefault();
    $$(".scene.drop-above", this.box).forEach((e) => e.classList.remove("drop-above"));
    const sc = ev.target.closest(".scene");
    if (sc && sc.dataset.scene !== this.dragged) sc.classList.add("drop-above");
  }

  onDrop(ev) {
    ev.preventDefault();
    const s = this.story();
    if (!this.dragged || !s) return;
    const target = ev.target.closest(".scene");
    const order = s.scenes.map((x) => x.uid).filter((u) => u !== this.dragged);
    const at = target ? order.indexOf(target.dataset.scene) : -1;
    order.splice(at < 0 ? order.length : at, 0, this.dragged);
    this.endDrag();
    this.patch({ scene_order: order });
  }

  endDrag() {
    this.dragged = null;
    $$(".dragging,.drop-above", this.box).forEach((e) => e.classList.remove("dragging", "drop-above"));
  }
}
