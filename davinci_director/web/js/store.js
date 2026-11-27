// What the panel knows about the app, and who to tell when it changes.
//
// The server is the source of truth (the timeline in Resolve, through it): the store only keeps its last
// answer.  Events: "view", "transcripts", "link", "undo", "stories", "topics", "playhead", "job",
// "selection" (from the editor), "busy".

import { remember } from "./util.js";

export const state = {
  view: { blocks: [], fps: 25, start: 0, v: 0 },
  tr: {},          // media key -> transcript {path, tag, speakers, frags, words}
  fragOf: {},      // media key -> Int32Array: word index -> fragment index
  tv: -1,          // version of the transcripts
  link: {},
  undo: {},
  stories: [],
  topics: [],
  settings: {},
  comp: {},        // components and models (checked in the background)
  playhead: null,
  busy: false,
  showCut: remember("dd.showCut", true),
  follow: true,
};

const bus = new EventTarget();
export const on = (name, fn) => bus.addEventListener(name, (e) => fn(e.detail));
export const emit = (name, detail) => bus.dispatchEvent(new CustomEvent(name, { detail }));

export function setTranscripts(tr, tv) {
  state.tr = tr || {};
  state.tv = tv;
  state.fragOf = {};
  for (const [k, t] of Object.entries(state.tr)) {
    const arr = new Int32Array(t.words.length);
    t.frags.forEach(([a, z], i) => arr.fill(i, a, z));
    state.fragOf[k] = arr;
  }
  emit("transcripts");
}

export const NO_SPEAKER = { id: "S1", name: "Мовець", color: "#cfcfcf" };
export function speaker(key, idx) {
  const t = state.tr[key];
  return (t && (t.speakers[idx] || t.speakers[0])) || NO_SPEAKER;
}

// timeline frame of a source time inside block b
export function wordFrame(b, t) {
  return Math.min(Math.max(b.s + Math.round((t - b.src0) * state.view.fps), b.s), b.e);
}
