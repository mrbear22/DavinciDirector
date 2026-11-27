// Small helpers without state: DOM lookup, safe HTML, time and word formatting.

export const $ = (s, el = document) => el.querySelector(s);
export const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));

const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const escape = (s) => String(s).replace(/[&<>"']/g, (c) => ESC[c]);

// html`<b>${name}</b>`: every interpolated value is escaped unless it is html`` itself (or an array of them)
class Html {
  constructor(s) { this.s = s; }
  toString() { return this.s; }
}
const part = (v) => (v == null || v === false ? "" : v instanceof Html ? v.s : Array.isArray(v) ? v.map(part).join("") : escape(v));
export const html = (strings, ...values) => new Html(strings.reduce((out, s, i) => out + part(values[i - 1]) + s));

export function tc(frame, fps) {
  const n = Math.round(fps) || 25;
  frame = Math.max(0, Math.round(frame));
  const f = frame % n, s = Math.floor(frame / n);
  const p = (x) => String(x).padStart(2, "0");
  return `${p(Math.floor(s / 3600))}:${p(Math.floor(s / 60) % 60)}:${p(s % 60)}:${p(f)}`;
}

export function dur(sec) {
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec / 3600), m = Math.floor(sec / 60) % 60, s = sec % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}

export function plural(n, one, few, many) {
  const m10 = n % 10, m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return one;
  if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
  return many;
}
export const nWords = (n) => `${n} ${plural(n, "слово", "слова", "слів")}`;
export const nClips = (n) => `${n} ${plural(n, "кліп", "кліпи", "кліпів")}`;

// per-viewer conveniences (a private window or blocked storage just means defaults)
export function remember(key, fallback) {
  try {
    const v = localStorage.getItem(key);
    return v == null ? fallback : JSON.parse(v);
  } catch (e) {
    return fallback;
  }
}
export function keep(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* not kept: defaults next time */ }
}
