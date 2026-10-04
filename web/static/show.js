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

  // Anything from cues.json lands in innerHTML, so it goes through here first.
  // The text is typed by a human in the editor, but "<" in a line would still
  // silently eat the rest of the card.
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // A div, not a button: the card carries its own Edit button, and a button
  // inside a button is invalid HTML that browsers "fix" unpredictably.
  // role/tabindex keep it keyboard-reachable the way the old element was.
  function cueCard(cue) {
    const node = document.createElement("div");
    const audio = cue.has_audio;
    node.className =
      "cue relative text-left rounded-lg border w-full cursor-pointer " +
      (audio
        ? "bg-slate-800 border-slate-600 hover:bg-slate-700"
        : "bg-slate-800/50 border-slate-700 hover:bg-slate-700/60");
    node.dataset.cueId = cue.id;
    node.setAttribute("role", "button");
    node.setAttribute("tabindex", "0");

    const dur = cue.duration_s ? `${cue.duration_s.toFixed(1)}s` : "";
    const motion = cue.motion
      ? (cue.motion.name || cue.motion.direction || cue.motion.type)
      : "";
    const missing = audio && !cue.audio_ready;

    node.innerHTML = `
      <div class="px-3 py-2 pr-12 fire-target">
        <div class="flex items-center gap-2">
          <span class="key">${esc(cue.hotkey || "·")}</span>
          <span class="font-medium text-sm">${esc(cue.label)}</span>
          <span class="ml-auto text-[11px] font-mono text-slate-400">${dur}</span>
        </div>
        ${cue.text ? `<div class="rtl mt-1.5 text-sm text-slate-300 leading-snug">${esc(cue.text)}</div>` : ""}
        <div class="mt-1.5 flex items-center gap-2 text-[11px] text-slate-400">
          ${motion ? `<span class="px-1.5 py-0.5 rounded bg-slate-700">${esc(motion)}</span>` : ""}
          ${audio ? "" : `<span class="px-1.5 py-0.5 rounded bg-slate-700">motion only</span>`}
          ${missing ? `<span class="px-1.5 py-0.5 rounded bg-red-700 text-white">no audio — press Rebuild</span>` : ""}
        </div>
      </div>
      <button class="edit-btn absolute top-1.5 right-1.5 px-2 py-0.5 rounded text-[11px]
                     bg-slate-700/80 hover:bg-sky-600 text-slate-200 border border-slate-600"
              title="Edit this cue">edit</button>`;

    node.querySelector(".fire-target").onclick = () => fire(cue.id);
    node.onkeydown = (ev) => {
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); fire(cue.id); }
    };
    node.querySelector(".edit-btn").onclick = (ev) => {
      // Without this the click also reaches the card and fires the cue —
      // Reachy announcing a line to the room because you meant to reword it.
      ev.stopPropagation();
      openEditor(cue);
    };
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

      const add = document.createElement("button");
      add.className = "mt-2 w-full rounded-lg border border-dashed border-slate-600 " +
                      "text-slate-400 hover:text-slate-100 hover:border-slate-400 " +
                      "text-sm py-1.5";
      add.textContent = "+ add cue";
      add.onclick = () => openEditor(null, section.id);
      card.appendChild(add);

      el.board.appendChild(card);
    });
    markPointer();
  }

  // ----- editing -------------------------------------------------------

  let editing = null;         // {cue, sectionId} or null when closed
  let motions = null;         // catalog, fetched lazily

  async function loadMotions() {
    if (motions) return motions;
    try {
      motions = await (await fetch("/api/show/motions")).json();
    } catch {
      motions = { emotions: [], dances: [], directions: [] };
    }
    return motions;
  }

  async function openEditor(cue, sectionId) {
    await loadMotions();
    editing = { cue: cue || null, sectionId: sectionId || (cue && cue.section) };
    const m = (cue && cue.motion) || {};
    $("ed-title").textContent = cue ? `Edit — ${cue.label}` : "New cue";
    $("ed-label").value = cue ? (cue.label || "") : "";
    $("ed-text").value = cue ? (cue.text || "") : "";
    $("ed-hotkey").value = cue ? (cue.hotkey || "") : "";
    $("ed-boss").value = cue ? (cue.boss_cue || "") : "";
    $("ed-motion-type").value = m.type || "";
    fillMotionNames(m.type || "", m.name || m.direction || "");
    $("ed-delete").classList.toggle("hidden", !cue);
    $("ed-error").textContent = "";
    $("ed-modal").classList.remove("hidden");
    setTimeout(() => $("ed-text").focus(), 30);
  }

  function fillMotionNames(type, selected) {
    const sel = $("ed-motion-name");
    let opts = [];
    if (type === "emotion") opts = motions.emotions;
    else if (type === "dance") opts = motions.dances;
    else if (type === "head") opts = motions.directions;
    sel.innerHTML = opts.map((n) =>
      `<option value="${esc(n)}"${n === selected ? " selected" : ""}>${esc(n)}</option>`
    ).join("");
    sel.disabled = opts.length === 0;
    sel.classList.toggle("opacity-40", opts.length === 0);
  }

  function closeEditor() {
    editing = null;
    $("ed-modal").classList.add("hidden");
  }

  function editorPayload() {
    const type = $("ed-motion-type").value;
    const name = $("ed-motion-name").value;
    let motion = null;
    if (type === "head") motion = { type: "head", direction: name };
    else if (type) motion = { type, name };
    return {
      label: $("ed-label").value,
      text: $("ed-text").value,
      hotkey: $("ed-hotkey").value,
      boss_cue: $("ed-boss").value,
      motion: motion,
    };
  }

  async function saveEditor() {
    if (!editing) return;
    const busy = (on) => {
      $("ed-save").disabled = on;
      $("ed-save").textContent = on ? "saving + re-recording…" : "Save";
    };
    $("ed-error").textContent = "";
    busy(true);
    try {
      const payload = editorPayload();
      let r;
      if (editing.cue) {
        r = await fetch(`/api/show/cue/${encodeURIComponent(editing.cue.id)}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
      } else {
        r = await fetch("/api/show/cue", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ...payload, section_id: editing.sectionId }),
        });
      }
      const body = await r.json().catch(() => ({}));
      if (!r.ok) { $("ed-error").textContent = body.error || `HTTP ${r.status}`; return; }
      render(body);
      closeEditor();
    } catch (e) {
      $("ed-error").textContent = String(e);
    } finally {
      busy(false);
    }
  }

  async function deleteEditing() {
    if (!editing || !editing.cue) return;
    const label = editing.cue.label || editing.cue.id;
    if (!confirm(`Delete "${label}"? Its recording is deleted too.`)) return;
    try {
      const r = await fetch(`/api/show/cue/${encodeURIComponent(editing.cue.id)}`,
                            { method: "DELETE" });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) { $("ed-error").textContent = body.error || `HTTP ${r.status}`; return; }
      render(body);
      closeEditor();
    } catch (e) {
      $("ed-error").textContent = String(e);
    }
  }

  async function rebuildAll() {
    const btn = $("rebuild-btn");
    btn.disabled = true;
    const was = btn.textContent;
    btn.textContent = "rebuilding…";
    try {
      const r = await fetch("/api/show/rebuild", { method: "POST" });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) { showError(body.error || `HTTP ${r.status}`); return; }
      render(body);
      const n = (body.built || []).length;
      const failed = (body.failed || []).length;
      el.npLabel.textContent = failed
        ? `rebuilt ${n}, ${failed} failed — see the log`
        : (n ? `rebuilt ${n} line${n === 1 ? "" : "s"}` : "everything already up to date");
    } catch (e) {
      showError(String(e));
    } finally {
      btn.disabled = false;
      btn.textContent = was;
    }
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
    // While the editor is open the keyboard belongs to it. Otherwise typing a
    // Hebrew line would fire cues on every keystroke that matches a hotkey,
    // and Escape would mean two different things at once.
    if (editing) {
      if (ev.key === "Escape") { ev.preventDefault(); closeEditor(); }
      if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) {
        ev.preventDefault(); saveEditor();
      }
      return;
    }
    if (ev.target && ["INPUT", "TEXTAREA", "SELECT"].includes(ev.target.tagName)) return;
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
  $("rebuild-btn").onclick = rebuildAll;
  $("ed-save").onclick = saveEditor;
  $("ed-cancel").onclick = closeEditor;
  $("ed-delete").onclick = deleteEditing;
  $("ed-motion-type").onchange = (ev) => fillMotionNames(ev.target.value, "");
  // Click the backdrop to dismiss, but not a click inside the panel.
  $("ed-modal").onclick = (ev) => { if (ev.target === $("ed-modal")) closeEditor(); };

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
