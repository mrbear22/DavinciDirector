// Generic pieces of the interface: notifications, context menu, dialogs, the status bar.

import { $, html } from "./util.js";
import { state, emit } from "./store.js";

const LONG_ERROR = 120;  // characters: such errors are instructions to follow, they stay until clicked

export function toast(msg, kind = "") {
  const t = document.createElement("div");
  t.className = "toast " + kind;
  t.textContent = msg;
  t.onclick = () => t.remove();
  $("#toasts").appendChild(t);
  if (!(kind === "err" && msg.length > LONG_ERROR)) setTimeout(() => t.remove(), kind === "err" ? 9000 : 3500);
}

export const status = (text) => { $("#statusEdit").textContent = text; };

// one action at a time: label in the status bar, errors as notifications; returns fn's result or undefined
export async function act(label, fn) {
  if (state.busy) return undefined;
  state.busy = true;
  status(label + "…");
  document.body.style.cursor = "progress";
  try {
    const res = await fn();
    status(label + ": готово");
    return res;
  } catch (e) {
    status("");
    toast(e.message, "err");
    return undefined;
  } finally {
    state.busy = false;
    document.body.style.cursor = "";
    emit("busy");
  }
}

// items: [label, shortcut, fn, disabled] | null (separator) | {label} (heading)
export function showMenu(x, y, items) {
  const m = $("#ctxMenu");
  m.replaceChildren();
  for (const it of items) {
    if (it === null) { m.appendChild(document.createElement("hr")); continue; }
    if (it.label) {
      const d = document.createElement("div");
      d.className = "menu-label";
      d.textContent = it.label;
      m.appendChild(d);
      continue;
    }
    const [label, key, fn, disabled] = it;
    const b = document.createElement("button");
    b.innerHTML = String(html`<span>${label}</span><span class="k">${key || ""}</span>`);
    b.disabled = !!disabled;
    b.onclick = () => { hideMenu(); fn(); };
    m.appendChild(b);
  }
  m.classList.remove("hidden");
  const r = m.getBoundingClientRect();
  m.style.left = Math.min(x, innerWidth - r.width - 6) + "px";
  m.style.top = Math.min(y, innerHeight - r.height - 6) + "px";
}
export const hideMenu = () => $("#ctxMenu").classList.add("hidden");

// buttons: [label, kind, fn]; kind "primary" (Enter), "ghost", "left" (secondary actions at the left edge).
// fn returning false keeps the dialog open.
export function modal(title, body, buttons, size = "") {
  const m = $("#modal");
  m.className = "modal " + size;
  m.innerHTML = String(html`<div class="modal-head">${title}<button class="mini" data-close title="Закрити (Esc)"><svg><use href="#i-close"/></svg></button></div>
    <div class="modal-body">${body}</div><div class="modal-foot"><div class="foot-left"></div></div>`);
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
  return m;
}
export const closeModal = () => $("#modalBack").classList.add("hidden");
export const modalOpen = () => !$("#modalBack").classList.contains("hidden");

export const note = (body, cls = "") => html`<div class="note ${cls}"><svg><use href="#i-info"/></svg><div>${body}</div></div>`;

export function setupWidgets() {
  document.addEventListener("mousedown", (e) => { if (!e.target.closest("#ctxMenu")) hideMenu(); });
  $("#modalBack").addEventListener("mousedown", (e) => { if (e.target.id === "modalBack") closeModal(); });
  // an interface error must never leave the text silently out of date
  window.addEventListener("error", (e) => toast("Помилка інтерфейсу: " + e.message, "err"));
  window.addEventListener("unhandledrejection", (e) => toast("Помилка: " + ((e.reason && e.reason.message) || e.reason), "err"));
}
