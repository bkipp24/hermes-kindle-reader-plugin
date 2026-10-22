import { jsx, jsxs } from "react/jsx-runtime";
import { useEffect, useRef, useState } from "react";
import { host, useValue } from "@hermes/plugin-sdk";

const ID = "kindle-reader";
const LOW_BUFFER_SECONDS = 115;

function parseCliJson(output) {
  const lines = String(output || "").split("\n").map(x => x.trim()).filter(Boolean);
  for (let i = lines.length - 1; i >= 0; i--) {
    if (!lines[i].startsWith("{")) continue;
    try { return JSON.parse(lines[i]); } catch { /* keep looking */ }
  }
  throw new Error(lines.slice(-3).join(" ") || "Empty Kindle queue response");
}

async function queueCli() {
  const routes = await host.profileRoutes();
  const route = routes.find(r => r.primary) || routes.find(r => r.mode === "local") || routes[0];
  if (!route) throw new Error("No Hermes profile route available");
  const res = await host.requestProfile(
    route,
    "cli.exec",
    { argv: ["--profile", route.targetProfile, "kindle-reader", "queue", "--json"], timeout: 30 },
    35_000,
  );
  if (res.blocked) throw new Error(res.hint || "cli.exec blocked");
  const payload = parseCliJson(res.output);
  if (!payload.ok) throw new Error(payload.error || "Kindle queue unavailable");
  return payload;
}

function dataUrl(base64) { return `data:audio/mpeg;base64,${base64}`; }
function fmt(value) {
  const s = Math.max(0, Math.floor(Number(value) || 0));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function ReaderPane() {
  const audioRef = useRef(null);
  const seenRef = useRef(new Map());
  const requestedRef = useRef(false);
  const stoppedRef = useRef(false);
  const playingWantedRef = useRef(false);
  const indexRef = useRef(0);
  const sessionId = useValue(host.state.focusedSessionId);
  const [items, setItems] = useState([]);
  const [status, setStatus] = useState("Loading queue…");
  const [playing, setPlaying] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [duration, setDuration] = useState(0);

  async function refresh() {
    try {
      const payload = await queueCli();
      const merged = payload.items.map(raw => {
        const known = seenRef.current.get(raw.id);
        if (known) return known;
        const item = { id: raw.id, label: raw.label, duration: raw.duration, src: dataUrl(raw.audio) };
        seenRef.current.set(item.id, item);
        return item;
      });
      setItems(merged);
      const audio = audioRef.current;
      if (audio && !audio.src && merged[indexRef.current]) load(indexRef.current, merged);
    } catch (error) {
      setStatus(`Queue error: ${error instanceof Error ? error.message : String(error)}`);
    }
  }

  function load(index, source = items) {
    const item = source[index];
    const audio = audioRef.current;
    if (!item || !audio) return false;
    indexRef.current = index;
    audio.src = item.src;
    audio.load();
    requestedRef.current = false;
    setStatus(item.label);
    return true;
  }

  async function requestMore() {
    if (requestedRef.current || stoppedRef.current || !sessionId) return;
    requestedRef.current = true;
    setStatus("Buffering the next screens…");
    try {
      await host.request("prompt.submit", {
        session_id: sessionId,
        submission_id: crypto.randomUUID(),
        text: "Kindle continuous reader buffer is low. Generate and append the next five screens from Co-Existence, then update the existing player. Do not repeat text.",
      });
    } catch (error) {
      requestedRef.current = false;
      setStatus(`Could not request buffer: ${error instanceof Error ? error.message : String(error)}`);
    }
  }

  useEffect(() => {
    void refresh();
    const timer = setInterval(() => void refresh(), 5000);
    return () => clearInterval(timer);
  }, []);

  async function toggle() {
    const audio = audioRef.current;
    if (!audio) return;
    stoppedRef.current = false;
    playingWantedRef.current = true;
    if (!audio.src && !load(indexRef.current)) { setStatus("Waiting for audio buffer…"); await requestMore(); return; }
    if (audio.paused) {
      try { await audio.play(); setPlaying(true); setStatus("Reading…"); }
      catch (error) { setStatus(`Playback blocked: ${error instanceof Error ? error.message : String(error)}`); }
    } else {
      audio.pause(); playingWantedRef.current = false; setPlaying(false); setStatus("Paused");
    }
  }

  async function ended() {
    const audio = audioRef.current;
    indexRef.current += 1;
    if (audio) { audio.removeAttribute("src"); audio.load(); }
    if (load(indexRef.current)) {
      if (playingWantedRef.current && !stoppedRef.current) { await audio.play(); setPlaying(true); setStatus("Reading…"); }
    } else if (!stoppedRef.current) {
      setStatus("Waiting for next buffer…");
      await requestMore();
    }
  }

  function stop() {
    stoppedRef.current = true;
    playingWantedRef.current = false;
    const audio = audioRef.current;
    if (audio) audio.pause();
    setPlaying(false); setStatus("Stopped");
    if (sessionId) void host.request("prompt.submit", {
      session_id: sessionId,
      submission_id: crypto.randomUUID(),
      text: "Stop the Kindle continuous reader now. Do not capture, transcribe, or advance any more pages.",
    });
  }

  const buffered = Math.max(0, items.length - indexRef.current);
  return jsxs("div", { className: "flex h-full min-h-0 flex-col gap-3 p-4 text-foreground", children: [
    jsxs("div", { children: [jsx("div", { className: "text-[0.625rem] uppercase tracking-[0.1em] text-(--ui-text-tertiary)", children: "Continuous Kindle reader" }), jsx("div", { className: "font-semibold", children: "Co-Existence: The Next Phase of AI" }), jsx("div", { className: "text-xs text-(--ui-text-tertiary)", children: "Kokoro · af_heart · remote live queue" })] }),
    jsxs("div", { className: "flex items-center gap-3", children: [
      jsx("button", { type: "button", onClick: () => void toggle(), className: "inline-flex size-10 items-center justify-center rounded-full bg-(--ui-accent) text-white", "aria-label": playing ? "Pause" : "Play", children: playing ? "❚❚" : "▶" }),
      jsxs("div", { className: "min-w-0 flex-1", children: [jsx("input", { className: "w-full accent-(--ui-accent)", type: "range", min: 0, max: 1000, value: duration ? Math.round(elapsed / duration * 1000) : 0, onChange: e => { const audio = audioRef.current; if (audio && audio.duration) audio.currentTime = Number(e.target.value) / 1000 * audio.duration; } }), jsxs("div", { className: "flex justify-between text-[0.65rem] text-(--ui-text-tertiary)", children: [jsx("span", { children: fmt(elapsed) }), jsx("span", { children: `−${fmt(duration - elapsed)}` })] })] }),
      jsx("button", { type: "button", onClick: stop, className: "rounded border border-(--ui-stroke-secondary) px-2 py-1 text-xs", children: "Stop" })
    ] }),
    jsxs("div", { className: "flex items-center justify-between border-t border-(--ui-stroke-secondary) pt-2 text-xs", children: [jsx("span", { children: status }), jsx("span", { className: "text-(--ui-text-tertiary)", children: `${buffered} buffered` })] }),
    jsx("audio", { ref: audioRef, preload: "auto", onEnded: () => void ended(), onLoadedMetadata: e => setDuration(e.currentTarget.duration || 0), onTimeUpdate: e => { const audio = e.currentTarget; setElapsed(audio.currentTime); setDuration(audio.duration || 0); if (audio.duration > 0 && audio.duration - audio.currentTime < LOW_BUFFER_SECONDS && indexRef.current >= items.length - 1) void requestMore(); } })
  ] });
}

export default {
  id: ID,
  name: "Kindle Reader",
  defaultEnabled: true,
  register(ctx) {
    ctx.register({ id: "pane", area: "panes", order: 120, title: "Kindle Reader", data: { placement: "bottom", dock: { pane: "workspace", pos: "bottom" }, height: "210px" }, render: () => jsx(ReaderPane, {}) });
  },
};
