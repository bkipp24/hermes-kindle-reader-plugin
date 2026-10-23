import assert from "node:assert/strict";
import fs from "node:fs";

const scenario = process.argv[2];
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const settle = async () => {
  for (let i = 0; i < 12; i += 1) await new Promise(resolve => setImmediate(resolve));
};

class MemoryStorage {
  constructor() { this.values = new Map(); }
  getItem(key) { return this.values.has(key) ? this.values.get(key) : null; }
  setItem(key, value) { this.values.set(key, String(value)); }
  removeItem(key) { this.values.delete(key); }
}

class AudioFake {
  constructor() {
    this.src = "";
    this.currentTime = 0;
    this.duration = 60;
    this.paused = true;
    this.playCalls = 0;
    this.pauseCalls = 0;
    this.loadCalls = 0;
  }
  async play() { this.playCalls += 1; this.paused = false; }
  pause() { this.pauseCalls += 1; this.paused = true; }
  load() { this.loadCalls += 1; }
  removeAttribute(name) { if (name === "src") this.src = ""; }
}

let currentAudio = null;
const jsx = (type, props = {}) => {
  if (type === "audio" && props.ref) props.ref.current = currentAudio;
  return { type, props };
};
const jsxs = jsx;
const hostRequests = [];
let focusedSessionId = "session-1";
let focusedStoredSessionId = "stored-1";
const host = {
  state: {
    focusedSessionId: { get: () => focusedSessionId },
    focusedStoredSessionId: { get: () => focusedStoredSessionId },
  },
  request: async (name, payload) => { hostRequests.push({ name, payload }); return {}; },
};

let activeRunner = null;
function useRef(initial) { return activeRunner.useRef(initial); }
function useState(initial) { return activeRunner.useState(initial); }
function useEffect(effect, deps) { return activeRunner.useEffect(effect, deps); }
function useValue(value) { return value.get(); }

class Runner {
  constructor(ctx, audio) {
    this.ctx = ctx;
    this.audio = audio;
    this.slots = [];
    this.pendingEffects = [];
    this.tree = null;
  }
  useRef(initial) {
    const index = this.index++;
    if (!(index in this.slots)) this.slots[index] = { current: initial };
    return this.slots[index];
  }
  useState(initial) {
    const index = this.index++;
    if (!(index in this.slots)) this.slots[index] = { value: typeof initial === "function" ? initial() : initial };
    const slot = this.slots[index];
    return [slot.value, update => { slot.value = typeof update === "function" ? update(slot.value) : update; }];
  }
  useEffect(effect) {
    const index = this.index++;
    if (!(index in this.slots)) {
      this.slots[index] = { mounted: true };
      this.pendingEffects.push(effect);
    }
  }
  render() {
    this.index = 0;
    currentAudio = this.audio;
    activeRunner = this;
    this.tree = globalThis.ReaderPane({ ctx: this.ctx });
    activeRunner = null;
    currentAudio = null;
    return this.tree;
  }
  runEffects() {
    for (const effect of this.pendingEffects.splice(0)) effect();
  }
}

function visit(node, predicate) {
  if (!node || typeof node !== "object") return null;
  if (predicate(node)) return node;
  const children = node.props?.children;
  for (const child of Array.isArray(children) ? children : [children]) {
    const found = visit(child, predicate);
    if (found) return found;
  }
  return null;
}
function button(tree, label) {
  const result = visit(tree, node => node.type === "button" &&
    (node.props?.["aria-label"] === label || node.props?.children === label));
  assert.ok(result, `button ${label} not found`);
  return result;
}
function audioNode(tree) {
  const result = visit(tree, node => node.type === "audio");
  assert.ok(result, "audio node not found");
  return result;
}

const originalSetInterval = globalThis.setInterval;
const originalClearInterval = globalThis.clearInterval;
const intervalCallbacks = new Set();
globalThis.setInterval = callback => { intervalCallbacks.add(callback); return callback; };
globalThis.clearInterval = callback => intervalCallbacks.delete(callback);
const poll = async () => {
  for (const callback of [...intervalCallbacks]) callback();
  await settle();
};
globalThis.localStorage = new MemoryStorage();
globalThis.__mocks = { jsx, jsxs, useEffect, useRef, useState, ConfirmDialog: () => null, host, useValue };
let source = fs.readFileSync(new URL("../desktop/plugin.js", import.meta.url), "utf8");
source = source
  .replace('import { jsx, jsxs } from "react/jsx-runtime";', 'const { jsx, jsxs } = globalThis.__mocks;')
  .replace('import { useEffect, useRef, useState } from "react";', 'const { useEffect, useRef, useState } = globalThis.__mocks;')
  .replace('import { ConfirmDialog, host, useValue } from "@hermes/plugin-sdk";', 'const { ConfirmDialog, host, useValue } = globalThis.__mocks;')
  .replace("export default {", "const pluginDefault = {")
  .concat("\nglobalThis.ReaderPane = ReaderPane;\n");
new Function(source)();

const book = { id: "kindle:B001", title: "Book" };
const item1 = { id: "item-1", label: "One", state: "ready", duration: 60 };
const item2 = { id: "item-2", label: "Two", state: "ready", duration: 60 };
const statePayload = server => ({
  active_book: book,
  pending_switch: null,
  books: [book],
  cursor: { screen_hash: "screen-0" },
  playback: { current_item_id: server.playbackId || null, offset: 0 },
  queue: { control_generation: server.generation, stopped: server.stopped },
  requests: [],
});

async function mount(ctx, audio = new AudioFake()) {
  const runner = new Runner(ctx, audio);
  runner.render();
  runner.runEffects();
  await settle();
  runner.render();
  return { runner, audio };
}

async function staleResumeAfterStop() {
  const server = { generation: 0, stopped: false, items: [item1, item2] };
  const resumeGate = deferred();
  const calls = [];
  const ctx = { rest: async (path, options = {}) => {
    calls.push({ path, options });
    if (path === "/state") return statePayload(server);
    if (path.endsWith("/queue")) return { items: server.items, control_generation: server.generation, stopped: server.stopped };
    if (path.includes("/audio/")) return { audio: path.endsWith("item-1") ? "AUDIO1" : "AUDIO2" };
    if (path.endsWith("/resume")) return resumeGate.promise;
    if (path === "/requests" && options.body.action === "stop") {
      server.generation += 1; server.stopped = true;
      return { id: "stop-1", status: "completed" };
    }
    if (path.endsWith("/playback")) {
      if (options.body?.item_id) server.playbackId = options.body.item_id;
      return {};
    }
    throw new Error(`unexpected ${path}`);
  }};
  const { runner, audio } = await mount(ctx);
  button(runner.tree, "Resume").props.onClick();
  await settle();
  const resumeCall = calls.find(call => call.path.endsWith("/resume"));
  assert.equal(resumeCall.options.body.control_generation, 0, "resume must carry its expected generation");
  button(runner.tree, "Stop").props.onClick();
  await settle();
  assert.equal(server.stopped, true);
  resumeGate.resolve({ items: server.items, control_generation: 0, stopped: false });
  await settle();
  assert.equal(audio.playCalls, 0, "a resume response made stale by Stop must not play");
  assert.equal(server.stopped, true, "stale frontend continuation must not clear durable stop");
}

async function stopDuringCompletionThenResume() {
  const server = { generation: 0, stopped: false, items: [{ ...item1 }, { ...item2 }] };
  const completionGate = deferred();
  const audio = new AudioFake();
  const calls = [];
  const ctx = { rest: async (path, options = {}) => {
    calls.push({ path, options });
    if (path === "/state") return statePayload(server);
    if (path.endsWith("/queue")) return { items: server.items.map(item => ({ ...item })), control_generation: server.generation, stopped: server.stopped };
    if (path.includes("/audio/")) return { audio: path.endsWith("item-1") ? "AUDIO1" : "AUDIO2" };
    if (path.endsWith("/resume")) {
      assert.equal(options.body.control_generation, server.generation);
      if (server.stopped) { server.generation += 1; server.stopped = false; }
      return { items: server.items, control_generation: server.generation, stopped: false };
    }
    if (path.endsWith("/completed/item-1")) return completionGate.promise;
    if (path === "/requests" && options.body.action === "stop") {
      server.generation += 1; server.stopped = true;
      return { id: "stop-1", status: "completed" };
    }
    if (path.endsWith("/playback")) {
      if (options.body?.item_id) server.playbackId = options.body.item_id;
      return {};
    }
    throw new Error(`unexpected ${path}`);
  }};
  const { runner } = await mount(ctx, audio);
  button(runner.tree, "Resume").props.onClick();
  await settle();
  runner.render();
  audioNode(runner.tree).props.onEnded();
  await settle();
  button(runner.tree, "Stop").props.onClick();
  await settle();
  assert.equal(audio.src, "", "Stop must invalidate media that already ended");
  server.items[0].state = "completed";
  completionGate.resolve({ items: server.items.map(item => ({ ...item })) });
  await settle();
  runner.render();
  button(runner.tree, "Resume").props.onClick();
  await settle();
  assert.match(audio.src, /AUDIO2$/, "Resume must load the next ready item");
  assert.equal(audio.playCalls, 2, "completed audio must not be replayed");
}

async function completionFailureSurvivesReload() {
  const server = { generation: 0, stopped: false, items: [{ ...item1 }, { ...item2 }], pending: new Set() };
  let completionAttempts = 0;
  const audioRequests = [];
  const reconcile = () => {
    for (const id of server.pending) {
      const item = server.items.find(value => value.id === id);
      if (item) item.state = "completed";
    }
    server.pending.clear();
  };
  const ctx = { rest: async (path, options = {}) => {
    if (path === "/state") { reconcile(); return statePayload(server); }
    if (path.endsWith("/queue")) return { items: server.items.map(item => ({ ...item })), control_generation: server.generation, stopped: server.stopped };
    if (path.includes("/audio/")) { audioRequests.push(path); return { audio: path.endsWith("item-1") ? "AUDIO1" : "AUDIO2" }; }
    if (path.endsWith("/resume")) return { items: server.items, control_generation: server.generation, stopped: false };
    if (path.endsWith("/completed/item-1")) {
      completionAttempts += 1;
      server.pending.add("item-1");
      throw new Error("response lost after durable completion intent");
    }
    if (path.endsWith("/playback")) {
      if (options.body?.item_id) server.playbackId = options.body.item_id;
      return {};
    }
    throw new Error(`unexpected ${path}`);
  }};
  const first = await mount(ctx, new AudioFake());
  button(first.runner.tree, "Resume").props.onClick();
  await settle();
  first.runner.render();
  audioNode(first.runner.tree).props.onEnded();
  await settle();
  assert.equal(completionAttempts, 1);
  assert.deepEqual([...server.pending], ["item-1"], "completion intent must be backend-durable");
  assert.equal(first.audio.playCalls, 1, "failed response must not advance playback locally");
  assert.equal(globalThis.localStorage.getItem("kindle-reader:pending-completions"), null,
    "renderer storage must not own completion state");

  audioRequests.length = 0;
  const reloaded = await mount(ctx, new AudioFake());
  assert.equal(completionAttempts, 1, "backend reconciliation must not need a renderer replay");
  assert.equal(server.pending.size, 0);
  assert.ok(audioRequests.some(path => path.endsWith("item-2")), "reload must prepare the next ready item");
  assert.ok(!audioRequests.some(path => path.endsWith("item-1")), "reload must not fetch heard audio again");
  assert.match(reloaded.audio.src, /AUDIO2$/);
}

async function concurrentPaneCompletionsUseBackend() {
  const item3 = { id: "item-3", label: "Three", state: "ready", duration: 60 };
  const server = { generation: 0, stopped: false, items: [{ ...item1 }, { ...item2 }, item3], pending: new Set() };
  const contextFor = playbackId => ({ rest: async (path) => {
    if (path === "/state") {
      for (const id of server.pending) server.items.find(item => item.id === id).state = "completed";
      server.pending.clear();
      return { ...statePayload(server), playback: { current_item_id: playbackId, offset: 0 } };
    }
    if (path.endsWith("/queue") || path.endsWith("/resume")) {
      return { items: server.items.map(item => ({ ...item })), control_generation: 0, stopped: false };
    }
    if (path.includes("/audio/")) return { audio: path.endsWith("item-1") ? "AUDIO1" : path.endsWith("item-2") ? "AUDIO2" : "AUDIO3" };
    const match = path.match(/\/completed\/(item-[12])$/);
    if (match) {
      server.pending.add(match[1]);
      throw new Error("response lost after durable intent");
    }
    if (path.endsWith("/playback")) return {};
    throw new Error(`unexpected ${path}`);
  }});

  const first = await mount(contextFor("item-1"), new AudioFake());
  const second = await mount(contextFor("item-2"), new AudioFake());
  button(first.runner.tree, "Resume").props.onClick();
  button(second.runner.tree, "Resume").props.onClick();
  await settle();
  first.runner.render();
  second.runner.render();
  audioNode(first.runner.tree).props.onEnded();
  audioNode(second.runner.tree).props.onEnded();
  await settle();
  assert.deepEqual(server.pending, new Set(["item-1", "item-2"]),
    "two panes must persist independent intents without lost updates");

  const reloaded = await mount(contextFor(null), new AudioFake());
  assert.equal(server.pending.size, 0);
  assert.deepEqual(server.items.map(item => item.state), ["completed", "completed", "ready"]);
  assert.match(reloaded.audio.src, /AUDIO3$/, "reload must observe reconciled backend completions");
}

async function stoppedResumeResponseDoesNotPlay() {
  const server = { generation: 0, stopped: true, items: [item1] };
  const ctx = { rest: async (path) => {
    if (path === "/state") return statePayload(server);
    if (path.endsWith("/queue")) return { items: server.items, control_generation: 0, stopped: true };
    if (path.endsWith("/resume")) return { items: server.items, control_generation: 0, stopped: true };
    if (path.includes("/audio/")) return { audio: "AUDIO1" };
    if (path.endsWith("/playback")) return {};
    throw new Error(`unexpected ${path}`);
  }};
  const { runner, audio } = await mount(ctx);
  button(runner.tree, "Resume").props.onClick();
  await settle();
  assert.equal(audio.playCalls, 0, "a resume response that remains stopped must not play");
}

async function stopWithoutFocusedSession() {
  const server = { generation: 0, stopped: false, items: [item1] };
  focusedSessionId = null;
  focusedStoredSessionId = null;
  const ctx = { rest: async (path, options = {}) => {
    if (path === "/state") return statePayload(server);
    if (path.endsWith("/queue")) return { items: server.items, control_generation: server.generation, stopped: server.stopped };
    if (path.includes("/audio/")) return { audio: "AUDIO1" };
    if (path === "/requests" && options.body.action === "stop") {
      assert.equal(options.body.controller_session_id, null);
      server.generation += 1; server.stopped = true;
      return { id: "stop-no-session", status: "completed" };
    }
    if (path.endsWith("/playback")) return {};
    throw new Error(`unexpected ${path}`);
  }};
  const { runner } = await mount(ctx);
  button(runner.tree, "Stop").props.onClick();
  await settle();
  assert.equal(server.stopped, true, "Stop must reach the backend without a focused session");
  assert.equal(hostRequests.length, 0, "session interruption is optional without a controller");
}

async function remoteStopPropagation() {
  const server = { generation: 0, stopped: false, items: [item1] };
  let pendingSwitch = { target_book_id: "kindle:B002" };
  const other = { id: "kindle:B002", title: "Other" };
  const ctx = { rest: async (path) => {
    if (path === "/state") return { ...statePayload(server), pending_switch: pendingSwitch, books: [book, other] };
    if (path.endsWith("/queue")) return { items: server.items, control_generation: server.generation, stopped: server.stopped };
    if (path.includes("/audio/")) return { audio: "AUDIO1" };
    if (path.endsWith("/resume")) return { items: server.items, control_generation: server.generation, stopped: false };
    if (path.endsWith("/playback")) return {};
    throw new Error(`unexpected ${path}`);
  }};
  const { runner, audio } = await mount(ctx);
  button(runner.tree, "Resume").props.onClick();
  await settle();
  assert.equal(audio.paused, false);
  assert.ok(visit(runner.render(), node => node.type === globalThis.__mocks.ConfirmDialog));

  server.stopped = true;
  server.generation += 1;
  pendingSwitch = null;
  await poll();
  const tree = runner.render();
  assert.equal(audio.paused, true, "a backend Stop from another pane must pause active audio");
  assert.equal(visit(tree, node => node.type === globalThis.__mocks.ConfirmDialog), null,
    "refresh must clear a stale switch target");
  audioNode(tree).props.onEnded();
  await settle();
  assert.equal(audio.playCalls, 1, "remote Stop must fence playback continuations");
}

try {
  if (scenario === "stale-resume") await staleResumeAfterStop();
  else if (scenario === "stop-during-completion") await stopDuringCompletionThenResume();
  else if (scenario === "completion-reload") await completionFailureSurvivesReload();
  else if (scenario === "concurrent-pane-completions") await concurrentPaneCompletionsUseBackend();
  else if (scenario === "stopped-resume-response") await stoppedResumeResponseDoesNotPlay();
  else if (scenario === "stop-no-session") await stopWithoutFocusedSession();
  else if (scenario === "remote-stop") await remoteStopPropagation();
  else throw new Error(`unknown scenario: ${scenario}`);
  console.log(`ok: ${scenario}`);
} finally {
  globalThis.setInterval = originalSetInterval;
  globalThis.clearInterval = originalClearInterval;
}
