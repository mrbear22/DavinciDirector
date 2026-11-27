// The app's HTTP API and its event stream.

export async function api(path, body) {
  const opt = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const r = await fetch("/api/" + path, opt);
  const data = await r.json().catch(() => ({}));
  if (!r.ok || data.error) throw new Error(data.error || "HTTP " + r.status);
  return data;
}

// plain text of an export (a prompt for the language model), for the clipboard
export async function fetchText(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || "HTTP " + r.status);
  return r.text();
}

// handlers: {open(), lost(), message(m)}
export function listen(handlers) {
  const es = new EventSource("/api/events");
  es.onopen = handlers.open;
  es.onerror = handlers.lost;
  es.onmessage = (ev) => handlers.message(JSON.parse(ev.data));
  return es;
}
