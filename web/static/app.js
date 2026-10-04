// Phase B dashboard client. Vanilla JS, single IIFE. Renders the two-column
// dashboard: System Status (+substate badge), Robot Connection (+heartbeat),
// System Controls, Session Monitor (+live turn timer), Event Log (color +
// copy + autoscroll), and the Live Conversation Stream (bubbles, RTL, avatar,
// aborted-turn lines). Drives Start/Stop System and Start/End Conversation
// against the existing REST endpoints.

(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const el = {
    stateDot: $("state-dot"),
    stateLabel: $("state-label"),
    substateBadge: $("substate-badge"),
    robotDot: $("robot-dot"),
    robotConnLabel: $("robot-conn-label"),
    robotIp: $("robot-ip"),
    robotDaemon: $("robot-daemon"),
    startSystemBtn: $("start-system-btn"),
    stopSystemBtn: $("stop-system-btn"),
    actionBtn: $("action-btn"),
    sessionBody: $("session-body"),
    convId: $("conv-id"),
    convTurns: $("conv-turns"),
    turnDuration: $("turn-duration"),
    log: $("log"),
    copyLogsBtn: $("copy-logs-btn"),
    copyLogsStatus: $("copy-logs-status"),
    chat: $("chat"),
    chatConvId: $("chat-conv-id"),
    chatConvIdSpan: $("chat-conv-id").querySelector("span"),
    robotName: $("robot-name"),
    robotBadges: $("robot-badges"),
    personaToggle: $("persona-toggle"),
    personaPanel: $("persona-panel"),
    personaSummary: $("persona-summary"),
    personaPreset: $("persona-preset"),
    personaText: $("persona-text"),
    personaVoice: $("persona-voice"),
    personaSave: $("persona-save"),
    personaReset: $("persona-reset"),
    personaMsg: $("persona-msg"),
    personaHint: $("persona-hint"),
    personaCount: $("persona-count"),
  };

  let currentState = "STARTING";

  // ---- state labels + dot colors -------------------------------------
  const STATE_LABEL = {
    STARTING: "Starting…", IDLE_BREATHING: "Idle",
    CONVERSATION_RUNNING: "In Conversation", STOPPED: "Stopped",
    SHUTTING_DOWN: "Shutting Down", ERROR: "Error",
  };
  const STATE_COLOR = {
    STARTING: "#f59e0b", IDLE_BREATHING: "#16a34a",
    CONVERSATION_RUNNING: "#2563eb", STOPPED: "#9ca3af",
    SHUTTING_DOWN: "#f59e0b", ERROR: "#dc2626",
  };

  function setState(state) {
    currentState = state;
    el.stateLabel.textContent = STATE_LABEL[state] || state;
    el.stateDot.style.background = STATE_COLOR[state] || "#9ca3af";
    updateButtons(state);
    // Substate badge only meaningful during a conversation.
    if (state !== "CONVERSATION_RUNNING") el.substateBadge.classList.add("hidden");
    // Session body muted unless in a conversation.
    el.sessionBody.classList.toggle("opacity-50", state !== "CONVERSATION_RUNNING");
    // Freeze the turn timer once the conversation is over.
    if (state === "IDLE_BREATHING" || state === "STOPPED" ||
        state === "ERROR" || state === "SHUTTING_DOWN") {
      freezeTimer();
    }
  }

  function updateButtons(state) {
    // Primary conversation button (in Session Monitor).
    const b = el.actionBtn;
    if (state === "IDLE_BREATHING") {
      b.textContent = "Start Conversation";
      b.className = "btn btn-primary mt-3 w-full";
      b.disabled = false;
      b.onclick = startConversation;
    } else if (state === "CONVERSATION_RUNNING") {
      b.textContent = "End Conversation";
      b.className = "btn btn-danger mt-3 w-full";
      b.disabled = false;
      b.onclick = endConversation;
    } else {
      b.textContent = "Start Conversation";
      b.className = "btn btn-primary mt-3 w-full";
      b.disabled = true;
      b.onclick = null;
    }
    // System controls.
    el.startSystemBtn.disabled = (state !== "STOPPED");
    el.stopSystemBtn.disabled = !(state === "IDLE_BREATHING" ||
                                  state === "CONVERSATION_RUNNING");
  }

  function setSubstate(ui, turnId) {
    if (currentState === "CONVERSATION_RUNNING") {
      el.substateBadge.textContent = ui;
      el.substateBadge.classList.remove("hidden");
    }
    el.convTurns.textContent = turnId;
    // New turn begins at its first LISTENING substate -> reset the timer.
    if (ui === "LISTENING" && turnId !== timerTurn) {
      timerTurn = turnId;
      resetTimer();
    }
  }

  // ---- turn-duration timer -------------------------------------------
  let timerTurn = null, timerStart = null, timerInterval = null;

  function fmtDuration(ms) {
    const total = Math.floor(ms / 1000);
    const m = String(Math.floor(total / 60)).padStart(2, "0");
    const s = String(total % 60).padStart(2, "0");
    return m + ":" + s;
  }
  function tickTimer() {
    if (timerStart != null) el.turnDuration.textContent = fmtDuration(Date.now() - timerStart);
  }
  function resetTimer() {
    timerStart = Date.now();
    el.turnDuration.textContent = "00:00";
    if (timerInterval == null) timerInterval = setInterval(tickTimer, 1000);
  }
  function freezeTimer() {
    if (timerInterval != null) { clearInterval(timerInterval); timerInterval = null; }
  }
  function clearTimer() {
    freezeTimer();
    timerTurn = null; timerStart = null;
    el.turnDuration.textContent = "00:00";
  }

  // ---- robot connection ----------------------------------------------
  function setRobot(robot) {
    if (!robot) return;
    const connected = !!robot.connected;
    el.robotDot.style.background = connected ? "#16a34a" : "#dc2626";
    el.robotConnLabel.textContent = connected ? "Connected" : "Disconnected";
    if (robot.ip) el.robotIp.textContent = robot.ip;
    if (robot.daemon_status) el.robotDaemon.textContent = robot.daemon_status;
  }

  // ---- session / conversation header ---------------------------------
  function setConversationHeader(id) {
    if (id) {
      el.convId.textContent = id;
      el.chatConvIdSpan.textContent = id;
      el.chatConvId.classList.remove("hidden");
    } else {
      el.convId.textContent = "—";
      el.chatConvId.classList.add("hidden");
    }
  }

  // ---- chat box -------------------------------------------------------
  const ROBOT_SVG =
    '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" ' +
    'class="shrink-0 mt-0.5" stroke="#c2410c" stroke-width="1.6" ' +
    'stroke-linecap="round" stroke-linejoin="round">' +
    '<rect x="5" y="8" width="14" height="10" rx="2"/>' +
    '<path d="M12 8V5"/><circle cx="12" cy="4" r="1"/>' +
    '<circle cx="9.5" cy="13" r="1"/><circle cx="14.5" cy="13" r="1"/>' +
    '<path d="M3 12v3M21 12v3"/></svg>';

  function stick(elm) { return elm.scrollHeight - elm.scrollTop - elm.clientHeight < 40; }
  function maybeScroll(elm, wasBottom) { if (wasBottom) elm.scrollTop = elm.scrollHeight; }

  function clearChat() { el.chat.innerHTML = ""; }

  function meta(turnId) {
    const m = document.createElement("div");
    m.className = "text-xs text-slate-400 mt-1";
    m.textContent = "Turn: " + turnId;
    return m;
  }
  function bubbleText(text) {
    const b = document.createElement("div");
    b.setAttribute("dir", "auto");
    b.className = "rounded-lg px-3 py-2 max-w-[85%] text-sm whitespace-pre-wrap break-words";
    b.textContent = text || "(no transcript)";
    return b;
  }

  function appendUser(turnId, text) {
    const wasBottom = stick(el.chat);
    const wrap = document.createElement("div");
    wrap.className = "flex flex-col items-end bubble-in";
    const b = bubbleText(text);
    b.classList.add("bg-slate-200", "text-slate-800");
    wrap.appendChild(b);
    wrap.appendChild(meta(turnId));
    el.chat.appendChild(wrap);
    maybeScroll(el.chat, wasBottom);
  }

  function appendRobot(turnId, text) {
    const wasBottom = stick(el.chat);
    const wrap = document.createElement("div");
    wrap.className = "flex flex-col items-start bubble-in";
    const row = document.createElement("div");
    row.className = "flex items-start gap-2 max-w-[90%]";
    row.innerHTML = ROBOT_SVG;
    const b = bubbleText(text);
    b.classList.add("bg-orange-100", "text-slate-800");
    row.appendChild(b);
    wrap.appendChild(row);
    wrap.appendChild(meta(turnId));
    el.chat.appendChild(wrap);
    maybeScroll(el.chat, wasBottom);
  }

  function appendAborted(turnId, reason) {
    const wasBottom = stick(el.chat);
    const line = document.createElement("div");
    line.className = "text-center text-xs text-slate-400 italic bubble-in py-1";
    line.textContent = (reason === "gemini_silent")
      ? ("Turn " + turnId + " aborted — Gemini silent")
      : ("Turn " + turnId + " aborted");
    el.chat.appendChild(line);
    maybeScroll(el.chat, wasBottom);
  }

  function renderTranscriptEntry(t) {
    if (t.role === "user") appendUser(t.turn_id, t.text);
    else if (t.role === "robot") appendRobot(t.turn_id, t.text);
    else if (t.role === "aborted") appendAborted(t.turn_id, t.reason);
  }

  // ---- event log ------------------------------------------------------
  let logLines = [];
  const LEVEL_RE = /^\d{2}:\d{2}:\d{2}\.\d{3}\s+(\w+)\s/;

  function nowTs() {
    const d = new Date(), p = (n, w) => String(n).padStart(w, "0");
    return p(d.getHours(), 2) + ":" + p(d.getMinutes(), 2) + ":" +
           p(d.getSeconds(), 2) + "." + p(d.getMilliseconds(), 3);
  }
  function fmtLog(ts, level, logger, msg) {
    return (ts || nowTs()) + " " + String(level).padEnd(5) + " " +
           String(logger).padEnd(28) + " " + msg;
  }
  function pushLogString(text, level) {
    logLines.push(text);
    const wasBottom = stick(el.log);
    const div = document.createElement("div");
    if (level) div.className = "log-" + level;
    div.textContent = text;
    el.log.appendChild(div);
    maybeScroll(el.log, wasBottom);
  }
  function appendLogLine(level, logger, msg, ts) {
    pushLogString(fmtLog(ts, level, logger, msg), level);
  }
  function levelOf(line) {
    const m = LEVEL_RE.exec(line);
    return m ? m[1] : "INFO";
  }

  // ---- controls -------------------------------------------------------
  async function post(path, label) {
    try {
      const r = await fetch(path, { method: "POST" });
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        appendLogLine("ERROR", "client", label + " failed: " + JSON.stringify(body));
      }
    } catch (e) {
      appendLogLine("ERROR", "client", label + " request error: " + e);
    }
  }
  async function startConversation() { el.actionBtn.disabled = true; await post("/api/conversation/start", "start"); }
  async function endConversation() { el.actionBtn.disabled = true; await post("/api/conversation/end", "end"); }
  async function startSystem() {
    el.startSystemBtn.disabled = true; el.stopSystemBtn.disabled = true;
    await post("/api/system/start", "start system");
  }
  async function stopSystem() {
    el.startSystemBtn.disabled = true; el.stopSystemBtn.disabled = true;
    await post("/api/system/stop", "stop system");
  }
  // Fallback for when navigator.clipboard is absent. That API is restricted to
  // secure contexts: HTTPS, or http on localhost. Laptop mode serves the
  // dashboard from 127.0.0.1 so it qualifies, but robot mode serves it from
  // the robot's LAN address over plain http, where navigator.clipboard is
  // simply undefined — which is why Copy logs worked for months and then
  // stopped the first time the dashboard was opened from the robot.
  // execCommand("copy") is deprecated but is not restricted this way, and it
  // remains the only option available on a plain-http origin.
  function copyViaTextarea(text) {
    const ta = document.createElement("textarea");
    ta.value = text;
    // Keep it off-screen and non-disruptive: no scroll jump, no visible flash,
    // but still focusable/selectable, which execCommand requires.
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "-1000px";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    try {
      ta.select();
      ta.setSelectionRange(0, ta.value.length);   // iOS/Safari need this
      return document.execCommand("copy");
    } catch (e) {
      return false;
    } finally {
      document.body.removeChild(ta);
    }
  }

  async function copyLogs() {
    const text = logLines.join("\n");
    const feedback = (m) => {
      el.copyLogsStatus.textContent = m;
      setTimeout(() => { el.copyLogsStatus.textContent = ""; }, 2500);
    };
    if (!text) { feedback("Nothing to copy"); return; }
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
        feedback("Copied");
        return;
      }
      throw new Error("clipboard API unavailable (insecure origin)");
    } catch (e) {
      // Either no API, or the write was refused (permissions, focus).
      if (copyViaTextarea(text)) feedback("Copied");
      else feedback("Copy failed — select the log and use Ctrl-C");
    }
  }

  // ---- WS dispatch ----------------------------------------------------
  function handle(m) {
    switch (m.event) {
      case "state.snapshot":
        setState(m.state);
        setRobot(m.robot);
        setIdentity(m.identity);
        setPersona(m.persona);
        if (m.conversation) {
          setConversationHeader(m.conversation.id);
          el.convTurns.textContent = m.conversation.turn_count != null ? m.conversation.turn_count : 0;
        } else {
          setConversationHeader(null);
          el.convTurns.textContent = "0";
        }
        // Event log restore (parse level out of pre-formatted ring-buffer lines).
        el.log.innerHTML = ""; logLines = [];
        (m.recent_log || []).forEach((l) => pushLogString(l, levelOf(l)));
        el.log.scrollTop = el.log.scrollHeight;
        // Chat restore.
        clearChat();
        (m.transcript || []).forEach(renderTranscriptEntry);
        break;
      case "state.change":
        setState(m.state);
        break;
      case "robot.status":
        setRobot(m);
        break;
      case "robot.heartbeat":
        if (m.ok && m.daemon_status) el.robotDaemon.textContent = m.daemon_status;
        break;
      case "conversation.started":
        clearChat();
        clearTimer();
        setConversationHeader(m.id);
        el.convTurns.textContent = "0";
        el.sessionBody.classList.remove("opacity-50");
        break;
      case "conversation.ended":
        appendLogLine("INFO", "client", "conversation ended (" + m.reason + ")");
        freezeTimer();
        break;
      case "transcript.user":
        appendUser(m.turn_id, m.text);
        break;
      case "transcript.robot":
        appendRobot(m.turn_id, m.text);
        break;
      case "turn.substate":
        setSubstate(m.state, m.turn_id);
        break;
      case "turn.aborted":
        appendAborted(m.turn_id, m.reason);
        break;
      case "persona.change":
        // Another tab (or another phone) changed this robot's persona. Every
        // open dashboard follows, so two people cannot believe different
        // things about what the robot in front of them is doing.
        setPersona(m);
        break;
      case "log":
        appendLogLine(m.level, m.logger, m.msg, m.ts);
        break;
      case "error":
        appendLogLine("ERROR", m.where || "system", m.message || "");
        break;
      default:
        break;
    }
  }

  // ---- identity + persona switch -------------------------------------

  function setIdentity(info) {
    if (!info) return;
    const robot = info.robot || {};
    const name = robot.display_name || "Reachy Mini";
    el.robotName.textContent = name;
    // The tab title too: ten tabs all reading "Reachy Mini Handler Dashboard"
    // is ten tabs you have to click through to find the right robot.
    document.title = name + " — Reachy";

    const badges = [];
    const provider = info.provider || {};
    if (provider.display_name) {
      badges.push({ text: provider.display_name, tone: provider.implemented ? "slate" : "amber" });
    }
    const key = info.key || {};
    if (key.error) {
      badges.push({ text: "no API key", tone: "red", title: key.error });
    } else if (key.label) {
      // Label and last four only. The key itself never reaches this page.
      badges.push({ text: key.label + " ··" + (key.key_tail || ""), tone: "slate",
                    title: "key source: " + (key.source || "") });
    }
    el.robotBadges.innerHTML = "";
    badges.forEach((b) => {
      const span = document.createElement("span");
      const tones = {
        slate: "bg-slate-100 text-slate-600 border-slate-200",
        amber: "bg-amber-50 text-amber-700 border-amber-200",
        red: "bg-red-50 text-red-700 border-red-200",
      };
      span.className = "px-2 py-0.5 rounded border " + (tones[b.tone] || tones.slate);
      span.textContent = b.text;
      if (b.title) span.title = b.title;
      el.robotBadges.appendChild(span);
    });
  }

  // True while the user is mid-edit, so a broadcast from another tab does not
  // overwrite what someone is typing. Live-updating a textarea under someone's
  // hands is the fastest way to lose a persona they were halfway through.
  let personaDirty = false;
  let personaLoaded = false;

  function fillOptions(select, values, selected, blankLabel) {
    select.innerHTML = "";
    if (blankLabel != null) {
      const o = document.createElement("option");
      o.value = ""; o.textContent = blankLabel;
      select.appendChild(o);
    }
    values.forEach((v) => {
      const o = document.createElement("option");
      o.value = v.value != null ? v.value : v;
      o.textContent = v.label != null ? v.label : v;
      select.appendChild(o);
    });
    select.value = selected || "";
  }

  function setPersona(p) {
    if (!p) return;

    el.personaToggle.checked = !!p.enabled;
    el.personaPanel.classList.toggle("hidden", !p.enabled);

    if (!personaDirty) {
      el.personaText.value = p.overlay || "";
      fillOptions(el.personaPreset,
                  (p.presets || []).map((x) => ({ value: x.id, label: x.label || x.id })),
                  p.preset_id, "Write my own");
      fillOptions(el.personaVoice, p.voices || [],
                  p.voice || "", "Default voice (" + (p.default_voice || "—") + ")");
    }

    // The summary is the honest line: what this robot is *actually* running.
    if (p.active) {
      const what = p.preset_id
        ? ((p.presets || []).find((x) => x.id === p.preset_id) || {}).label || p.preset_id
        : "Custom";
      el.personaSummary.textContent = "In character — " + what;
      el.personaSummary.className = "text-sm text-green-700 font-medium mt-1";
    } else {
      el.personaSummary.textContent = p.enabled
        ? "Switched on, but nothing written yet"
        : "Base persona";
      el.personaSummary.className = "text-sm text-slate-500 mt-1";
    }

    el.personaHint.textContent = p.applies_next
      ? "Applies to the next conversation"
      : "";
    updatePersonaCount(p.max_chars);
    personaLoaded = true;
  }

  function updatePersonaCount(max) {
    const limit = max || 1500;
    const n = el.personaText.value.length;
    el.personaCount.textContent = n ? n + " / " + limit : "";
    el.personaCount.className = n > limit
      ? "text-xs text-red-600" : "text-xs text-slate-400";
  }

  function personaMessage(text, ok) {
    el.personaMsg.textContent = text || "";
    el.personaMsg.className = "text-xs " + (ok ? "text-green-700" : "text-red-600");
    if (text) setTimeout(() => {
      if (el.personaMsg.textContent === text) el.personaMsg.textContent = "";
    }, 4000);
  }

  async function sendPersona(body) {
    try {
      const r = await fetch("/api/persona", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await r.json();
      if (!r.ok) {
        personaMessage(data.detail || "Could not save that.", false);
        return null;
      }
      personaDirty = false;
      setPersona(data);
      return data;
    } catch (e) {
      personaMessage("Could not reach the robot.", false);
      return null;
    }
  }

  async function loadPersona() {
    try {
      const [idRes, pRes] = await Promise.all([
        fetch("/api/identity"), fetch("/api/persona"),
      ]);
      setIdentity(await idRes.json());
      setPersona(await pRes.json());
    } catch (e) { /* the WS snapshot carries both as well */ }
  }

  el.personaToggle.onchange = async () => {
    const on = el.personaToggle.checked;
    el.personaPanel.classList.toggle("hidden", !on);
    // Switching on saves nothing by itself; switching off is the reset, and
    // it takes effect immediately without needing Save.
    const body = { enabled: on };
    if (on && el.personaText.value.trim()) body.overlay = el.personaText.value;
    const data = await sendPersona(body);
    if (data) personaMessage(on ? "" : "Back to the base persona.", true);
  };

  el.personaPreset.onchange = async () => {
    const id = el.personaPreset.value;
    if (!id) return;
    personaDirty = false;
    await sendPersona({ preset_id: id, enabled: true });
  };

  el.personaText.oninput = () => { personaDirty = true; updatePersonaCount(); };

  el.personaSave.onclick = async () => {
    const data = await sendPersona({
      overlay: el.personaText.value,
      voice: el.personaVoice.value,
      enabled: true,
    });
    if (data) personaMessage("Saved.", true);
  };

  el.personaVoice.onchange = () => { personaDirty = true; };

  el.personaReset.onclick = async () => {
    try {
      const r = await fetch("/api/persona/reset", { method: "POST" });
      personaDirty = false;
      setPersona(await r.json());
      personaMessage("Cleared — back to the base persona.", true);
    } catch (e) {
      personaMessage("Could not reach the robot.", false);
    }
  };

  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(proto + "://" + location.host + "/ws");
    ws.onmessage = (ev) => { try { handle(JSON.parse(ev.data)); } catch (e) {} };
    ws.onclose = () => {
      el.stateLabel.textContent = (STATE_LABEL[currentState] || currentState) + " (reconnecting…)";
      setTimeout(connect, 1500);
    };
    ws.onerror = () => { try { ws.close(); } catch (e) {} };
  }

  el.startSystemBtn.onclick = startSystem;
  el.stopSystemBtn.onclick = stopSystem;
  el.copyLogsBtn.onclick = copyLogs;

  loadPersona();
  connect();
})();
