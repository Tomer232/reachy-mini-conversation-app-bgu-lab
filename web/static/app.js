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
  async function copyLogs() {
    const text = logLines.join("\n");
    const feedback = (m) => {
      el.copyLogsStatus.textContent = m;
      setTimeout(() => { el.copyLogsStatus.textContent = ""; }, 1500);
    };
    try {
      if (!navigator.clipboard || !navigator.clipboard.writeText)
        throw new Error("clipboard unavailable");
      await navigator.clipboard.writeText(text);
      feedback("Copied");
    } catch (e) { feedback("Copy failed"); }
  }

  // ---- WS dispatch ----------------------------------------------------
  function handle(m) {
    switch (m.event) {
      case "state.snapshot":
        setState(m.state);
        setRobot(m.robot);
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

  connect();
})();
