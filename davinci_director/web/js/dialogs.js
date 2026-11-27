// Dialogs: transcription, the prompt for the language model, importing its answer, components and
// models, choosing a timeline, renaming a speaker, help.

import { $, $$, html } from "./util.js";
import { state, emit } from "./store.js";
import { api, fetchText } from "./api.js";
import { act, toast, modal, closeModal, note } from "./ui.js";

const TIMELINES_SETTLE_MS = 400;  // the script needs a moment to report the project's timelines

async function copyExport(url, label) {
  try {
    await navigator.clipboard.writeText(await fetchText(url));
    toast(label + " — скопійовано. Вставте в чат Claude (Ctrl+V).", "ok");
  } catch (e) {
    toast(e.message, "err");
  }
}

const value = (sel) => $(sel).value.trim();
const checked = (name) => ($(`input[name="${name}"]:checked`) || {}).value;

export class Dialogs {
  constructor(callbacks) {
    this.cb = callbacks;  // {showStory(uid), rerender()}
  }

  // ------------------------------------------------------------ transcription

  // what the first run downloads; the dialog opens at once with the last known state and refreshes it
  downloadsNote(comp) {
    const need = ["audio", "speakers"].filter((k) => comp[k] && !comp[k].installed).map((k) => comp[k].size);
    const asr = (comp.models || [])[0];
    if (asr && !asr.available) need.push(asr.size);
    return need.length ? note(`Перший запуск: програма один раз завантажить модель і компоненти (${need.join(" + ")}). Далі все працює без інтернету.`) : "";
  }

  transcribe() {
    const s = state.settings || {};
    const asr = (state.comp.models || [])[0] || { label: "Parakeet TDT 0.6B v3 UK" };
    api("components").then((c) => {
      state.comp = c;
      const box = $("#tComp");
      if (box) box.innerHTML = String(this.downloadsNote(c));
    }).catch(() => { /* the note is only a hint */ });
    const token = s.has_hf_token
      ? html`<span class="val">збережено</span><button class="btn sm ghost" id="tHfChange">Змінити</button><input type="text" id="tHf" class="hidden" placeholder="hf_…" style="flex:1">`
      : html`<input type="text" id="tHf" placeholder="hf_… — потрібен один раз для моделі мовців">`;
    modal("Транскрибування", html`
      <p class="lead">Розпізнає мову в кліпах доріжки <b>${state.view.track || ""}</b>. Кожне слово прив'язується до свого місця у відео.</p>
      <div class="fsec"><div class="fsec-h">Розпізнавання</div>
        <div class="frow"><span class="fl">Модель</span><span class="fc"><span class="val" title="${asr.credit || ""}">${asr.label}</span><span class="sub">українська, англійські назви латиницею</span></span></div>
        <div class="frow"><span class="fl">Що розпізнати</span><span class="fc"><span class="seg">
          <label><input type="radio" name="tScope" value="used" checked><span>Кліпи на таймлайні</span></label>
          <label><input type="radio" name="tScope" value="whole"><span>Файли повністю</span></label></span></span></div>
        <div class="frow"><span class="fl"></span><span class="fc"><label class="chk"><input type="checkbox" id="tForce"> Розпізнати заново, замінивши наявний текст</label></span></div>
      </div>
      <div class="fsec"><div class="fsec-h">Мовці</div>
        <div class="frow"><span class="fl">Визначати</span><span class="fc"><label class="chk"><input type="checkbox" id="tDiar" ${s.diarize !== false ? "checked" : ""}> Розфарбувати текст за мовцями</label></span></div>
        <div class="frow" id="tSpkRow"><span class="fl">Кількість</span><span class="fc"><input type="number" class="num" id="tSpk" min="0" max="12" value="${s.num_speakers || ""}" placeholder="авто"><span class="sub">порожньо — визначити автоматично</span></span></div>
        <div class="frow" id="tHfRow"><span class="fl">Токен Hugging Face</span><span class="fc">${token}</span></div>
      </div>
      <div id="tComp">${this.downloadsNote(state.comp)}</div>`,
      [["Лише мовці", "left", () => this.startJob("diarize", { diarize: true })],
        ["Скасувати"],
        ["Почати", "primary", () => this.startJob("transcribe", {
          diarize: $("#tDiar").checked, whole: checked("tScope") === "whole", force: $("#tForce").checked })]]);
    const sync = () => ["#tSpkRow", "#tHfRow"].forEach((r) => $(r).classList.toggle("hidden", !$("#tDiar").checked));
    $("#tDiar").onchange = sync;
    sync();
    const change = $("#tHfChange");
    if (change) change.onclick = () => { change.remove(); $("#tHf").classList.remove("hidden"); $("#tHf").focus(); };
  }

  // settings of the dialog are saved, then the job starts (false keeps the dialog open on an error)
  async startJob(path, opts) {
    const speakers = +$("#tSpk").value || 0;
    const settings = { diarize: opts.diarize, num_speakers: speakers };
    if (value("#tHf")) settings.hf_token = value("#tHf");
    try {
      state.settings = await api("settings", settings);
      await api(path, Object.assign({ speakers }, opts));
    } catch (e) {
      toast(e.message, "err");
      return false;
    }
    return true;
  }

  // ------------------------------------------------------------ the prompt for the language model

  async exportPrompt() {
    let est;
    try { est = await api("export/estimate"); } catch (e) { toast(e.message, "err"); return; }
    const single = est.mode === "single";
    const only = () => (checked("exScope") === "tl" ? "1" : "");
    const copyPrompt = () => this.saveBrief().then(() => copyExport(`/api/export/claude?raw=1&only=${only()}`, "Запит"));
    const download = () => this.saveBrief().then(() => { location.href = `/api/export/claude?only=${only()}`; });
    const stage2 = () => {
      if (!est.beats) { toast("Спершу імпортуйте відповіді на частини етапу 1.", "err"); return; }
      this.saveBrief().then(() => copyExport("/api/export/stage2?raw=1", "Етап 2"));
    };
    modal("Завдання для мовної моделі", this.exportBody(est), [
      [single ? "Завантажити файл" : "Завантажити частини (.zip)", "left ghost", download],
      ["Закрити", "", () => { this.saveBrief(); }],
      single ? ["Копіювати запит", "primary", () => { copyPrompt(); return false; }]
        : ["Етап 2: відео з моментів", "primary", () => { stage2(); return false; }]], "wide");
    $("#brDur").onchange = () => $("#brCustom").classList.toggle("hidden", $("#brDur").value !== "custom");
    const parts = est.topics_parts || 1;
    $$("[data-topics]").forEach((b) => (b.onclick = () => copyExport(`/api/export/topics?raw=1&part=${b.dataset.topics}&only=${only()}`,
      parts > 1 ? `Теми, частина ${b.dataset.topics}` : "Запит на теми")));
    $$("[data-copypart]").forEach((b) => (b.onclick = () => this.saveBrief().then(() =>
      copyExport(`/api/export/claude?raw=1&part=${b.dataset.copypart}&only=${only()}`, "Частину " + b.dataset.copypart))));
    const clear = $("#topicsClear");
    if (clear) clear.onclick = () => api("topics/clear", {}).then(() => {
      state.topics = [];
      emit("topics");
      closeModal();
      toast("Теми прибрано.", "ok");
    }).catch((e) => toast(e.message, "err"));
  }

  exportBody(est) {
    const thousands = (n) => (n / 1000).toFixed(n < 10000 ? 1 : 0) + " тис.";
    const mins = (s) => (s < 120 ? `${s} с` : `${Math.round(s / 60)} хв`);
    const br = est.brief || {};
    const durations = Object.entries(est.durations || {}).map(([v, [label, lo, hi]]) =>
      html`<option value="${v}" ${br.duration === v ? "selected" : ""}>${label} · ${mins(lo)}–${mins(hi)}</option>`);
    const how = est.mode === "single"
      ? note(html`Обсяг ≈ <b>${thousands(est.tokens)}</b> токенів — один запит. Скопіюйте його в чат мовної моделі (Claude, ChatGPT…), а відповідь вставте через «Імпорт».`)
      : note(html`Обсяг ≈ <b>${thousands(est.tokens)}</b> токенів — забагато для одного чату, тому два етапи.<br>
         <b>Етап 1:</b> ${est.parts} частин — кожну окремим чатом, кожну відповідь імпортуйте (це ключові моменти).<br>
         <b>Етап 2</b> (моментів зараз: ${est.beats}): з моментів модель складе відео за вашим завданням.`);
    const parts = est.mode === "single" ? "" : html`<div class="frow"><span class="fl">Етап 1</span><span class="fc">${Array.from({ length: est.parts }, (_, i) =>
      html`<button class="btn sm" data-copypart="${i + 1}">Копіювати частину ${i + 1}</button>`)}</span></div>`;
    const tn = est.topics_parts || 1;
    return html`
      <p class="lead">Опишіть, яке відео потрібне. Програма складе запит із транскрипцією, а модель запропонує варіанти сюжету з таймкодами.</p>
      <div class="fsec"><div class="fsec-h">Відео</div>
        <div class="frow"><span class="fl">Тривалість</span><span class="fc"><select id="brDur">${durations}<option value="custom" ${br.duration === "custom" ? "selected" : ""}>своя тривалість…</option></select>
          <span id="brCustom" class="${br.duration === "custom" ? "" : "hidden"}">від <input type="number" class="num" id="brMin" min="5" value="${br.min_sec || ""}"> до
          <input type="number" class="num" id="brMax" min="5" value="${br.max_sec || ""}"> с</span></span></div>
        <div class="frow"><span class="fl">Варіантів</span><span class="fc"><input type="number" class="num" id="brVar" min="1" max="5" value="${br.variants || 3}"></span></div>
        <div class="frow top"><span class="fl">Що потрібно</span><span class="fc"><textarea id="brNotes" rows="4" placeholder="Своїми словами: про що відео, для кого, настрій і темп, що обов'язково лишити, що викинути, як почати й закінчити…">${br.notes || ""}</textarea></span></div>
        <div class="frow"><span class="fl">Матеріал</span><span class="fc"><span class="seg">
          <label><input type="radio" name="exScope" value="all" checked><span>Уся транскрипція</span></label>
          <label><input type="radio" name="exScope" value="tl"><span>Лише таймлайн</span></label></span></span></div>
      </div>
      <div class="fsec"><div class="fsec-h">Запит</div>${how}${parts}</div>
      <div class="fsec"><div class="fsec-h">Теми</div>
        <p class="lead">Окремий короткий запит: модель поділить розмову на теми, і після імпорту вони стануть заголовками в тексті.${est.topics ? html` Зараз тем: <b>${est.topics}</b>.` : ""}</p>
        <div class="frow"><span class="fl"></span><span class="fc">${Array.from({ length: tn }, (_, i) =>
          html`<button class="btn sm" data-topics="${i + 1}">Копіювати запит на теми${tn > 1 ? ` (${i + 1})` : ""}</button>`)}
          ${est.topics ? html`<button class="btn sm ghost" id="topicsClear">Прибрати теми</button>` : ""}</span></div></div>`;
  }

  // what the user wants is saved with the project before every copy / download
  saveBrief() {
    if (!$("#brDur")) return Promise.resolve();
    return api("brief", { duration: $("#brDur").value, min_sec: $("#brMin").value, max_sec: $("#brMax").value,
      variants: +$("#brVar").value || 1, notes: $("#brNotes").value }).catch((e) => toast(e.message, "err"));
  }

  // ------------------------------------------------------------ the answer of the language model

  importAnswer() {
    modal("Імпорт відповіді", html`
      <p class="lead">Вставте відповідь мовної моделі цілком — програма сама знайде в ній сюжети, теми чи моменти.</p>
      <textarea class="big" id="impText" placeholder="Вставте відповідь сюди (Ctrl+V)"></textarea>
      <div class="frow" style="grid-template-columns:auto 1fr"><button class="btn sm" id="impFile">Або вибрати файл…</button><span class="sub" id="impName"></span></div>`,
      [["Скасувати"], ["Імпортувати", "primary", () => this.importText(value("#impText"))]]);
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

  async importText(text) {
    if (!text) { toast("Вставте JSON або виберіть файл.", "err"); return false; }
    let r;
    try { r = await api("stories/import", { text }); } catch (e) { toast(e.message, "err"); return false; }
    state.stories = r.stories;
    state.topics = r.topics || state.topics;
    const warnings = r.warnings && r.warnings.length ? "\n" + r.warnings.join("\n") : "";
    if (r.kind === "topics") {
      emit("topics");
      toast(`Тем: ${r.count} (усього ${r.total}). Вони з'явилися в тексті заголовками.` + warnings, "ok");
    } else if (r.kind === "beats") {
      toast(`Імпортовано моментів: ${r.count} (усього ${r.total}). Коли всі частини готові — «Експорт для Claude» > «Етап 2».` + warnings, "ok");
    } else {
      this.cb.showStory(r.imported[0]);
      toast(`Імпортовано варіантів: ${r.imported.length}`, "ok");
    }
    return true;
  }

  // ------------------------------------------------------------ components and models

  async components() {
    let st;
    try { st = await api("components?disk=1"); } catch (e) { toast(e.message, "err"); return; }
    const cap = (t) => t.charAt(0).toUpperCase() + t.slice(1);
    const row = (k) => {
      const c = st[k];
      const button = c.installed
        ? (c.local ? html`<button class="btn sm ghost" data-comp-rm="${k}">Видалити</button>` : html`<span></span>`)
        : html`<button class="btn sm" data-comp-get="${k}">Завантажити</button>`;
      return html`<div class="drow"><span><div class="dn">${cap(c.label)}</div>${c.disk ? html`<div class="dd">на диску ${c.disk}</div>` : ""}</span>
        ${c.installed ? html`<span class="ds ok">встановлено</span>` : html`<span class="ds">${c.size}</span>`}${button}</div>`;
    };
    const models = st.models.map((m) => html`<div class="drow"><span><div class="dn">${m.label || m.name}</div><div class="dd">${m.size || ""}</div></span>
      ${m.available ? html`<span class="ds ok">завантажено</span><span></span>` : html`<span class="ds">не завантажено</span><button class="btn sm" data-model-get="${m.name}">Завантажити</button>`}</div>`);
    const credits = st.models.filter((m) => m.credit).map((m, i) =>
      html`${i ? html`<br>` : ""}${m.label}: ${m.credit}. Ліцензія <a href="${m.license}" target="_blank" rel="noopener">CC BY 4.0</a>, модель використовується без змін.`);
    modal("Компоненти й моделі", html`
      <p class="lead">Програма завантажує їх сама, коли вони вперше потрібні. Тут можна зробити це заздалегідь; перерване завантаження продовжиться з місця зупинки.</p>
      <div class="fsec"><div class="fsec-h">Компоненти</div><div class="dlist">${["audio", "speakers"].map(row)}</div></div>
      <div class="fsec"><div class="fsec-h">Модель розпізнавання</div><div class="dlist">${models}</div></div>
      ${credits.length ? note(credits) : ""}`, [["Закрити", "primary"]]);
    const job = (path, body) => api(path, body)
      .then(() => { closeModal(); toast("Завантаження у фоні — прогрес у рядку стану внизу."); })
      .catch((e) => toast(e.message, "err"));
    $$("[data-comp-get]").forEach((b) => (b.onclick = () => job("components/install", { name: b.dataset.compGet })));
    $$("[data-model-get]").forEach((b) => (b.onclick = () => job("models/download", { name: b.dataset.modelGet })));
    $$("[data-comp-rm]").forEach((b) => (b.onclick = () => api("components/remove", { name: b.dataset.compRm })
      .then((r) => { toast(r.removed === "later" ? "Буде видалено після перезапуску програми." : "Видалено.", "ok"); closeModal(); })
      .catch((e) => toast(e.message, "err"))));
  }

  // ------------------------------------------------------------ timelines of the project

  async timelines() {
    try {
      await api("timelines", {});
      await new Promise((r) => setTimeout(r, TIMELINES_SETTLE_MS));
      const info = (await api("state")).link.info || {};
      const list = info.timelines || [], cur = info.timeline;
      modal("Таймлайн", list.length
        ? html`<p class="lead">Виберіть таймлайн проєкту, з яким працювати.</p><div class="dlist">${list.map((t) =>
          html`<div class="drow click ${t.name === cur ? "cur" : ""}" data-tlid="${t.id}"><span class="dn">${t.name}</span><span class="ds">${t.name === cur ? "поточний" : ""}</span></div>`)}</div>`
        : note("У проєкті немає таймлайнів."), [["Закрити"]], "narrow");
      $$("[data-tlid]").forEach((e) => (e.onclick = () => { closeModal(); act("Перемикання", () => api("set_timeline", { id: e.dataset.tlid })); }));
    } catch (e) {
      toast(e.message, "err");
    }
  }

  // ------------------------------------------------------------ small ones

  renameSpeaker(key, sid) {
    const s = state.tr[key].speakers.find((x) => x.id === sid);
    modal("Перейменувати мовця", html`<div class="frow"><span class="fl">Ім'я</span><span class="fc"><input type="text" id="spName" value="${s.name}"></span></div>`,
      [["Скасувати"], ["Зберегти", "primary", () => act("Мовець", () => api("speaker/rename", { m: key, sid, name: $("#spName").value }))]], "narrow");
    setTimeout(() => $("#spName").select(), 30);
  }

  help() {
    modal("Як монтувати текстом", html`<div class="help">
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
}
