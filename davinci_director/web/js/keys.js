// Keyboard shortcuts of the panel.

import { $ } from "./util.js";
import { hideMenu, closeModal, modalOpen } from "./ui.js";

// the physical key, so Ctrl+Z works on the Ukrainian layout too (ev.key would be "я")
const keyOf = (ev) => (/^Key[A-Z]$/.test(ev.code || "") ? ev.code.slice(3).toLowerCase() : (ev.key || "").toLowerCase());
const typingIn = (el) => ["input", "textarea", "select"].includes((el.tagName || "").toLowerCase());

export function setupKeys(editor, actions, dialogs) {
  const plain = {  // without Ctrl
    "Delete": () => actions.cutOrRestore(),
    "Backspace": () => actions.cutOrRestore(),
    " ": () => actions.play(),
    "F2": () => editor.correctSelection(),
    "F1": () => dialogs.help(),
    "?": () => dialogs.help(),
    "ArrowRight": (ev) => editor.step(1, ev.shiftKey),
    "ArrowLeft": (ev) => editor.step(-1, ev.shiftKey),
  };
  const withCtrl = {
    a: () => editor.selectAll(),
    z: (ev) => (ev.shiftKey ? actions.redo() : actions.undo()),
    y: () => actions.redo(),
    c: () => actions.copy(false),
    x: () => actions.copy(true),
    v: () => actions.paste(),
    d: () => actions.duplicate(),
  };

  document.addEventListener("keydown", (ev) => {
    const ctrl = ev.ctrlKey || ev.metaKey, k = keyOf(ev);
    if (ctrl && k === "f") { ev.preventDefault(); $("#searchInput").focus(); $("#searchInput").select(); return; }
    if (ev.key === "Escape") { hideMenu(); closeModal(); editor.endDrag(); editor.clear(); return; }
    if (typingIn(ev.target) || modalOpen()) return;
    if (!ctrl && !ev.altKey && /^[1-9]$/.test(ev.key) && editor.wordRanges().length) {
      ev.preventDefault();
      actions.speakerByNumber(+ev.key);  // these words were said by speaker N
      return;
    }
    const fn = ctrl ? withCtrl[k] : plain[ev.key];
    // without a selection, Ctrl+C and the arrows keep their usual meaning (copy text, scroll)
    const needsSelection = (ctrl && k === "c") || ev.key.startsWith("Arrow");
    if (!fn || (needsSelection && !editor.payload())) return;
    ev.preventDefault();
    fn(ev);
  });
}
