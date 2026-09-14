// Live log tail via SSE: ANSI colors, find-in-log highlighting, timestamps toggle.
// SSE events are JSON: {"t": epoch_seconds, "s": "raw line (may contain ANSI)"}
(function () {
  const view = document.getElementById("log-view");
  if (!view) return;
  const toolId = view.dataset.tool;
  const sourceSel = document.getElementById("log-source");
  const autoscroll = document.getElementById("log-autoscroll");
  const clearBtn = document.getElementById("log-clear");
  const downloadBtn = document.getElementById("log-download");
  const searchBox = document.getElementById("log-search");
  const tsToggle = document.getElementById("log-timestamps");
  const MAX_LINES = 5000;
  let es = null;

  // ANSI SGR fg colors tuned for the always-dark log surface
  const FG = {
    30: "#8b949e", 31: "#ff7b72", 32: "#3fb950", 33: "#d29922",
    34: "#58a6ff", 35: "#bc8cff", 36: "#39c5cf", 37: "#d3dae2",
    90: "#6e7681", 91: "#ffa198", 92: "#56d364", 93: "#e3b341",
    94: "#79c0ff", 95: "#d2a8ff", 96: "#56d4dd", 97: "#f0f6fc",
  };

  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

  // Convert a raw line to safe HTML with ANSI colors as spans.
  function ansiToHtml(raw) {
    let html = "", open = false;
    for (const part of raw.split(/(\x1b\[[0-9;]*m)/)) {
      const m = part.match(/^\x1b\[([0-9;]*)m$/);
      if (!m) {
        if (part) html += esc(part);
        continue;
      }
      const codes = (m[1] || "0").split(";").map(Number);
      let style = "";
      for (let i = 0; i < codes.length; i++) {
        const c = codes[i];
        if (FG[c]) style += `color:${FG[c]};`;
        else if (c === 1) style += "font-weight:600;";
        else if (c === 3) style += "font-style:italic;";
        else if (c === 38 && codes[i + 1] === 5) i += 2; // 256-color: skip, keep default
      }
      if (open) { html += "</span>"; open = false; }
      if (style) { html += `<span style="${style}">`; open = true; }
    }
    if (open) html += "</span>";
    // strip any non-SGR escape sequences that slipped through
    return html.replace(/\x1b\[[0-9;?]*[a-zA-Z]/g, "");
  }

  function fmtTs(t) {
    return new Date(t * 1000).toLocaleTimeString([], { hour12: false });
  }

  function makeRow(t, raw) {
    const div = document.createElement("div");
    div.className = "log-line";
    div.dataset.text = raw.toLowerCase();
    div.innerHTML =
      `<span class="log-ts"${tsToggle.checked ? "" : " hidden"}>${fmtTs(t)}</span>` +
      ansiToHtml(raw);
    applyFilterTo(div);
    return div;
  }

  function applyFilterTo(row) {
    const q = searchBox.value.trim().toLowerCase();
    if (!q) { row.hidden = false; row.classList.remove("log-hit"); return; }
    const hit = row.dataset.text.includes(q);
    row.hidden = !hit;
    row.classList.toggle("log-hit", hit);
  }

  function applyFilterAll() {
    view.querySelectorAll(".log-line").forEach(applyFilterTo);
    if (!autoscroll || autoscroll.checked) view.scrollTop = view.scrollHeight;
  }

  function updateDownloadLink(source) {
    if (!downloadBtn) return;
    downloadBtn.href = `/api/logs/${toolId}/download?source=${source}`;
    downloadBtn.download = `${toolId}-${source}.log`;
  }

  function connect() {
    if (es) es.close();
    view.textContent = "";
    const source = sourceSel ? sourceSel.value : "run";
    updateDownloadLink(source);
    es = new EventSource(`/api/logs/${toolId}/stream?source=${source}`);
    // the server replays the ring buffer on every (re)connect — start clean
    es.onopen = () => (view.textContent = "");
    es.onmessage = (ev) => {
      let t = 0, s = ev.data;
      try { const d = JSON.parse(ev.data); t = d.t; s = d.s; } catch {}
      view.appendChild(makeRow(t, s));
      while (view.childNodes.length > MAX_LINES) view.removeChild(view.firstChild);
      if (!autoscroll || autoscroll.checked) view.scrollTop = view.scrollHeight;
    };
  }

  if (sourceSel) sourceSel.addEventListener("change", connect);
  if (clearBtn) clearBtn.addEventListener("click", () => (view.textContent = ""));
  if (searchBox) searchBox.addEventListener("input", applyFilterAll);
  if (tsToggle) tsToggle.addEventListener("change", () => {
    view.querySelectorAll(".log-ts").forEach((el) => (el.hidden = !tsToggle.checked));
  });
  // Browsers cap connections per origin (6 on HTTP/1.1); a lingering SSE stream
  // from a backgrounded or navigated-away page starves every other request.
  // Close aggressively on hide/navigation, reconnect when visible again.
  window.addEventListener("pagehide", () => es && es.close());
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { if (es) es.close(); }
    else connect();
  });
  connect();
})();
