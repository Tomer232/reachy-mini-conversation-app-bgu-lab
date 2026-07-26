// Operator board for show mode. Vanilla JS, same conventions as app.js.
//
// The interaction model is a show-caller's, not a soundboard's: the script has
// an order, Space fires the next cue and advances the pointer, and the letter
// keys stay available for out-of-order reactions. Under stage pressure you
// want one key you can hit without reading.

(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const el = {
    board: $("board"),
    sysState: $("sys-state"),
    convWarning: $("conv-warning"),
    stopBtn: $("stop-btn"),
    reloadBtn: $("reload-btn"),
    npLabel: $("np-label"),
    npText: $("np-text"),
    npBar: $("np-bar"),
    npCountdown: $("np-countdown"),
  };

  let cues = {};        // id -> cue
  let order = [];       // cue ids, script order
  let byHotkey = {};    // key -> cue id
  let pointer = 0;      // index into order
  let playTimer = null;
  let systemState = "…";

  // ----- rendering -----

  function cueCard(cue) {
    const node = document.createElement("button");
    const audio = cue.has_audio;
    node.className =
      "cue text-left rounded-lg px-3 py-2 border w-full " +
      (audio
        ? "bg-slate-800 border-slate-600 hover:bg-slate-700"
        : "bg-slate-800/50 border-slate-700 hover:bg-slate-700/60");
    node.dataset.cueId = cue.id;
    node.onclick = () => fire(cue.id);

    const dur = cue.duration_s ? `${cue.duration_s.toFixed(1)}s` : "";
    const motion = cue.motion
      ? (cue.motion.name || cue.motion.direction || cue.motion.type)
      : "";
    const missing = audio && !cue.audio_ready;

    node.innerHTML = `
      <div class="flex items-center gap-2">
        <span class="key">${cue.hotkey || "·"}</span>
        <span class="font-medium text-sm">${cue.label}</span>
        <span class="ml-auto text-[11px] font-mono text-slate-400">${dur}</span>
      </div>
      ${cue.text ? `<div class="rtl mt-1.5 text-sm text-slate-300 leading-snug">${cue.text}</div>` : ""}
      <div class="mt-1.5 flex items-center gap-2 text-[11px] text-slate-400">
        ${motion ? `<span class="px-1.5 py-0.5 rounded bg-slate-700">${motion}</span>` : ""}
        ${audio ? "" : `<span class="px-1.5 py-0.5 rounded bg-slate-700">motion only</span>`}
        ${missing ? `<span class="px-1.5 py-0.5 rounded bg-red-700 text-white">no audio — run build_show.py</span>` : ""}
      </div>`;
    return node;
  }

  function render(catalog) {
    cues = {};
    order = catalog.order || [];
    byHotkey = {};
    el.board.innerHTML = "";

    (catalog.sections || []).forEach((section) => {
      const card = document.createElement("section");
      card.className = "rounded-xl bg-slate-900 border border-slate-700 p-3";
      const h = document.createElement("h2");
      h.className = "rtl text-sm font-semibold text-slate-300 mb-2";
      h.textContent = section.title || section.id;
      card.appendChild(h);

      const list = document.createElement("div");
      list.className = "space-y-2";
      section.cues.forEach((cue) => {
        cues[cue.id] = cue;
        if (cue.hotkey) byHotkey[cue.hotkey.toLowerCase()] = cue.id;
        list.appendChild(cueCard(cue));
      });
      card.appendChild(list);
      el.board.appendChild(card);
    });
    markPointer();
  }

  function markPointer() {
    document.querySelectorAll(".cue.next").forEach((n) => n.classList.remove("next"));
    const id = order[pointer];
    if (!id) return;
    const node = document.querySelector(`[data-cue-id="${id}"]`);
    if (node) {
      node.classList.add("next");
      node.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }

  function movePointer(delta) {
    if (!order.length) return;
    pointer = Math.max(0, Math.min(order.length - 1, pointer + delta));
    markPointer();
  }

  // ----- firing -----

  async function fire(cueId, advance) {
    const cue = cues[cueId];
    if (!cue) return;
    const node = document.querySelector(`[data-cue-id="${cueId}"]`);
    if (node) {
      node.classList.add("firing");
      setTimeout(() => node.classList.remove("firing"), 250);
    }
    try {
      const r = await fetch(`/api/show/fire/${encodeURIComponent(cueId)}`, { method: "POST" });
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        showError(body.error || `HTTP ${r.status}`);
        return;
      }
    } catch (e) {
      showError(String(e));
      return;
    }
    if (advance) {
      const idx = order.indexOf(cueId);
      if (idx >= 0 && idx + 1 < order.length) {
        pointer = idx + 1;
        markPointer();
      }
    }
  }

  async function stopAll() {
    try {
      await fetch("/api/show/stop", { method: "POST" });
    } catch (e) {
      showError(String(e));
    }
  }

  function showError(msg) {
    el.npLabel.textContent = `error: ${msg}`;
    el.npLabel.classList.add("text-red-400");
    setTimeout(() => el.npLabel.classList.remove("text-red-400"), 3000);
  }

  // ----- now-playing bar -----

  function startPlaying(payload) {
    clearInterval(playTimer);
    const cue = cues[payload.cue_id] || {};
    el.npLabel.textContent = payload.label || payload.cue_id;
    el.npText.textContent = cue.text || "";
    const total = payload.duration_s || 0;
    if (!total) {
      el.npBar.style.width = "0%";
      el.npCountdown.textContent = "";
      return;
    }
    const t0 = performance.now();
    playTimer = setInterval(() => {
      const elapsed = (performance.now() - t0) / 1000;
      const pct = Math.min(100, (elapsed / total) * 100);
      el.npBar.style.width = `${pct}%`;
      const left = Math.max(0, total - elapsed);
      el.npCountdown.textContent = `${left.toFixed(1)}s`;
      if (left <= 0) clearInterval(playTimer);
    }, 50);
  }

  function endPlaying() {
    clearInterval(playTimer);
    el.npBar.style.width = "0%";
    el.npCountdown.textContent = "";
  }

  function setSystemState(state) {
    systemState = state;
    el.sysState.textContent = state;
    const inConv = state === "CONVERSATION_RUNNING";
    el.convWarning.classList.toggle("hidden", !inConv);
    const usable = inConv || state === "IDLE_BREATHING";
    el.board.style.opacity = usable ? "1" : "0.45";
  }

  // ----- keyboard -----

  document.addEventListener("keydown", (ev) => {
    if (ev.target && ["INPUT", "TEXTAREA"].includes(ev.target.tagName)) return;
    if (ev.key === "Escape") {
      ev.preventDefault();
      stopAll();
      return;
    }
    if (ev.key === " ") {
      ev.preventDefault();
      const id = order[pointer];
      if (id) fire(id, true);
      return;
    }
    if (ev.key === "ArrowDown" || ev.key === "ArrowRight") {
      ev.preventDefault(); movePointer(1); return;
    }
    if (ev.key === "ArrowUp" || ev.key === "ArrowLeft") {
      ev.preventDefault(); movePointer(-1); return;
    }
    if (ev.ctrlKey || ev.altKey || ev.metaKey) return;
    const id = byHotkey[ev.key.toLowerCase()];
    if (id) {
      ev.preventDefault();
      fire(id, false);
    }
  });

  el.stopBtn.onclick = stopAll;
  el.reloadBtn.onclick = async () => {
    const r = await fetch("/api/show/reload", { method: "POST" });
    render(await r.json());
  };

  // ----- websocket (shared with the dashboard) -----

  function connect() {
    const ws = new WebSocket(`ws://${location.host}/ws`);
    ws.onmessage = (msg) => {
      let data;
      try { data = JSON.parse(msg.data); } catch { return; }
      switch (data.event) {
        case "state.snapshot":
        case "state.change":
          setSystemState(data.state);
          break;
        case "show.cue.started":
          if (data.has_audio) startPlaying(data);
          else { el.npLabel.textContent = data.label; el.npText.textContent = ""; }
          break;
        case "show.cue.ended":
          if (data.has_audio) endPlaying();
          break;
        case "show.stopped":
          endPlaying();
          el.npLabel.textContent = "stopped";
          el.npText.textContent = "";
          break;
      }
    };
    ws.onclose = () => setTimeout(connect, 1500);
  }

  (async function init() {
    const r = await fetch("/api/show/cues");
    const catalog = await r.json();
    render(catalog);
    setSystemState(catalog.state || "…");
    connect();
  })();
})();
