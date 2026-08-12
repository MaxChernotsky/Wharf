// Detail-page activity charts: two single-series line charts (CPU %, RSS MB)
// and an event timeline. Plain SVG, no libraries. Refreshes every 10s.
(function () {
  const panel = document.getElementById("activity-panel");
  if (!panel) return;
  const toolId = panel.dataset.tool;
  const tooltip = document.getElementById("chart-tooltip");

  const css = getComputedStyle(document.documentElement);
  const C = (name) => css.getPropertyValue(name).trim();

  const EVENT_COLORS = {
    "started": "--green", "stopped": "--gray", "crashed": "--red",
    "gave-up": "--red", "mem-cap": "--orange", "idle-stop": "--blue",
  };

  const H = 120, PAD_L = 44, PAD_R = 14, PAD_T = 8, PAD_B = 18;

  function niceMax(v) {
    if (v <= 0) return 1;
    const exp = Math.pow(10, Math.floor(Math.log10(v)));
    const n = v / exp;
    const step = n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10;
    return step * exp;
  }

  const fmtTime = (t) =>
    new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });

  function renderChart(el, series, unit) {
    const W = el.clientWidth || 600;
    if (series.length < 2) {
      el.innerHTML = '<div class="hint chart-empty">No samples yet — data appears while the tool runs.</div>';
      return;
    }
    const t0 = series[0][0], t1 = series[series.length - 1][0];
    const vmax = niceMax(Math.max(...series.map((p) => p[1])));
    const x = (t) => PAD_L + ((t - t0) / Math.max(t1 - t0, 1)) * (W - PAD_L - PAD_R);
    const y = (v) => PAD_T + (1 - v / vmax) * (H - PAD_T - PAD_B);

    const pts = series.map((p) => `${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join(" ");
    const area = `${PAD_L},${y(0)} ${pts} ${x(t1).toFixed(1)},${y(0)}`;
    const last = series[series.length - 1];
    const grid = [0, vmax / 2, vmax];

    el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}">
      ${grid.map((g) => `
        <line x1="${PAD_L}" y1="${y(g)}" x2="${W - PAD_R}" y2="${y(g)}" class="gridline"/>
        <text x="${PAD_L - 6}" y="${y(g) + 3.5}" class="tick" text-anchor="end">${g >= 1000 ? (g / 1000) + "k" : g}</text>`).join("")}
      ${W > 280 ? `<text x="${PAD_L}" y="${H - 4}" class="tick">${fmtTime(t0)}</text>` : ""}
      <text x="${W - PAD_R}" y="${H - 4}" class="tick" text-anchor="end">${fmtTime(t1)}</text>
      <polygon points="${area}" class="chart-area"/>
      <polyline points="${pts}" class="chart-line"/>
      <circle cx="${x(last[0])}" cy="${y(last[1])}" r="4" class="chart-dot"/>
      <line class="crosshair" y1="${PAD_T}" y2="${H - PAD_B}" hidden/>
    </svg>`;

    const svg = el.querySelector("svg");
    const cross = svg.querySelector(".crosshair");
    svg.addEventListener("mousemove", (ev) => {
      const rect = svg.getBoundingClientRect();
      const mx = ((ev.clientX - rect.left) / rect.width) * W;
      let best = 0, bd = Infinity;
      series.forEach((p, i) => {
        const d = Math.abs(x(p[0]) - mx);
        if (d < bd) { bd = d; best = i; }
      });
      const p = series[best];
      cross.hidden = false;
      cross.setAttribute("x1", x(p[0]));
      cross.setAttribute("x2", x(p[0]));
      tooltip.hidden = false;
      tooltip.textContent = `${fmtTime(p[0])} — ${p[1]}${unit}`;
      tooltip.style.left = ev.pageX + 12 + "px";
      tooltip.style.top = ev.pageY - 28 + "px";
    });
    svg.addEventListener("mouseleave", () => { cross.hidden = true; tooltip.hidden = true; });
  }

  function renderTimeline(el, events) {
    if (!events.length) {
      el.innerHTML = '<div class="hint chart-empty">No events yet this session.</div>';
      return;
    }
    const W = el.clientWidth || 600;
    const t0 = events[0][0], t1 = Math.max(events[events.length - 1][0], t0 + 1);
    const x = (t) => PAD_L + ((t - t0) / (t1 - t0)) * (W - PAD_L - PAD_R);
    el.innerHTML = `<svg viewBox="0 0 ${W} 34" width="${W}" height="34">
      <line x1="${PAD_L}" y1="14" x2="${W - PAD_R}" y2="14" class="gridline"/>
      ${events.map(([t, kind, detail]) => `
        <circle cx="${x(t).toFixed(1)}" cy="14" r="5" class="event-dot"
                fill="var(${EVENT_COLORS[kind] || "--gray"})">
          <title>${fmtTime(t)} — ${kind}${detail ? ": " + detail.replaceAll("<", "&lt;") : ""}</title>
        </circle>`).join("")}
      ${W > 280 ? `<text x="${PAD_L}" y="31" class="tick">${fmtTime(t0)}</text>` : ""}
      <text x="${W - PAD_R}" y="31" class="tick" text-anchor="end">${fmtTime(t1)}</text>
    </svg>`;
  }

  async function refresh() {
    try {
      const d = await (await fetch(`/api/tools/${toolId}/stats`)).json();
      renderChart(document.getElementById("chart-cpu"), d.history.map((p) => [p[0], p[1]]), "%");
      renderChart(document.getElementById("chart-rss"), d.history.map((p) => [p[0], p[2]]), " MB");
      renderTimeline(document.getElementById("timeline"), d.events);
    } catch (e) { console.error("activity charts:", e); }
  }
  refresh();
  setInterval(refresh, 10000);
})();
