// "Add WiFi…" on the Robot Connection card (wifi_networks.py on the server).
// Saves a hotspot on the robot without switching to it: turn the current
// hotspot off and the new one on, and the robot moves over by itself.

(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const open = $("wifi-open");
  const panel = $("wifi-panel");
  const ssid = $("wifi-ssid");
  const pass = $("wifi-pass");
  const save = $("wifi-save");
  const msg = $("wifi-msg");
  const list = $("wifi-list");
  if (!open) return;

  function say(text, kind) {
    msg.textContent = text;
    msg.className = "text-xs " + (kind === "error" ? "text-red-600"
      : kind === "ok" ? "text-green-700" : "text-slate-500");
  }

  function render(networks) {
    list.textContent = "";
    if (!networks.length) {
      const li = document.createElement("li");
      li.textContent = "nothing yet";
      list.append(li);
    }
    networks.forEach((n) => {
      const li = document.createElement("li");
      li.textContent = n.name
        + (n.active ? " (connected now)" : "")
        + (n.setup ? " (its own setup network)" : "")
        + (!n.autoconnect && !n.setup ? " (won't join by itself)" : "");
      list.append(li);
    });
  }

  async function load() {
    try {
      const res = await fetch("/api/wifi");
      const body = await res.json();
      if (!body.available) {
        say(body.reason || "WiFi settings are not available here.", "error");
        save.disabled = true;
        render([]);
        return;
      }
      save.disabled = false;
      render(body.networks || []);
    } catch (err) {
      say("Could not ask the robot: " + err, "error");
    }
  }

  open.addEventListener("click", () => {
    const show = panel.classList.contains("hidden");
    panel.classList.toggle("hidden", !show);
    open.textContent = show ? "Close WiFi" : "Add WiFi…";
    if (show) { say(""); load(); ssid.focus(); }
  });

  save.addEventListener("click", async () => {
    const name = ssid.value.trim();
    if (!name) { say("Type the hotspot's name first.", "error"); return; }
    if (pass.value.length < 8) { say("A WiFi password has at least 8 characters.", "error"); return; }
    save.disabled = true;
    say("Saving on the robot…");
    try {
      const res = await fetch("/api/wifi", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ssid: name, password: pass.value }),
      });
      const body = await res.json().catch(() => ({}));
      if (res.ok && body.ok) {
        pass.value = "";             // the password leaves the page once saved
        render(body.networks || []);
        say("“" + body.ssid + "” " + body.action + ". The robot stays on this network for now; "
          + "turn this hotspot off and that one on, and it moves over within a minute.", "ok");
      } else {
        say(body.error || ("Failed (" + res.status + ")."), "error");
      }
    } catch (err) {
      say("Could not reach the robot: " + err, "error");
    } finally {
      save.disabled = false;
    }
  });
})();
