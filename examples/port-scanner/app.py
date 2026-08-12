"""Port Scanner — zero-dependency demo tool (stdlib only).

Scans every TCP port on localhost, reports which are open, and makes a
best-effort guess at what's running on each (banner grab + well-known
port table). HTTP-looking ports get a live mini-preview iframe.
"""

from __future__ import annotations

import json
import os
import signal
import socket
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import monotonic

PORT = int(os.environ.get("PORT", "8000"))
HOST = os.environ.get("HOST", "0.0.0.0")
OWN_PORT = PORT
MAX_WORKERS = 400
PROBE_TIMEOUT = 0.25

WELL_KNOWN = {
    20: "FTP (data)", 21: "FTP", 22: "SSH", 23: "Telnet", 25: "SMTP",
    53: "DNS", 80: "HTTP", 110: "POP3", 111: "RPCbind", 123: "NTP",
    135: "MS RPC", 139: "NetBIOS", 143: "IMAP", 389: "LDAP", 443: "HTTPS",
    445: "SMB", 465: "SMTPS", 514: "Syslog", 587: "SMTP (submission)",
    631: "IPP (printing)", 993: "IMAPS", 995: "POP3S", 1080: "SOCKS",
    1433: "MSSQL", 1521: "Oracle DB", 2049: "NFS", 2375: "Docker (unencrypted)",
    2376: "Docker (TLS)", 3000: "Dev server", 3001: "Dev server",
    3306: "MySQL / MariaDB", 3389: "RDP", 4000: "Dev server", 5000: "Dev server",
    5001: "Dev server", 5353: "mDNS", 5432: "PostgreSQL", 5601: "Kibana",
    5672: "RabbitMQ", 5900: "VNC", 5984: "CouchDB", 6379: "Redis",
    6443: "Kubernetes API", 7000: "Dev server", 7474: "Neo4j",
    8000: "Dev server", 8008: "HTTP (alt)", 8080: "HTTP (alt / proxy)",
    8081: "HTTP (alt)", 8086: "InfluxDB", 8443: "HTTPS (alt)", 8888: "Jupyter",
    9000: "Dev server", 9042: "Cassandra", 9092: "Kafka",
    9200: "Elasticsearch", 9300: "Elasticsearch (transport)",
    11211: "Memcached", 15672: "RabbitMQ (mgmt)", 27017: "MongoDB",
    27018: "MongoDB (shard)",
}


def guess_from_banner(banner: str) -> str | None:
    b = banner.lower()
    if b.startswith("ssh-"):
        return f"SSH ({banner.strip()})"
    if b.startswith("http/"):
        for line in banner.splitlines():
            if line.lower().startswith("server:"):
                return f"HTTP — {line.split(':', 1)[1].strip()}"
        return "HTTP"
    if b.startswith("+ok") or b.startswith("-err"):
        return "POP3"
    if "redis" in b or b.startswith("-noauth"):
        return "Redis"
    if b.startswith("220") and "smtp" in b:
        return "SMTP"
    if b.startswith("220") and "ftp" in b:
        return "FTP"
    return None


def looks_like_wharf(port: int) -> bool:
    """Fingerprint Wharf's own dashboard via its /healthz endpoint."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=PROBE_TIMEOUT) as s:
            s.settimeout(PROBE_TIMEOUT)
            s.sendall(b"GET /healthz HTTP/1.0\r\nHost: localhost\r\n\r\n")
            data = b""
            while len(data) < 4096:
                chunk = s.recv(512)
                if not chunk:
                    break
                data += chunk
            text = data.decode("utf-8", "replace")
            status_line, _, rest = text.partition("\r\n")
            body = rest.rsplit("\r\n\r\n", 1)[-1].lower()
            return " 200 " in f" {status_line} " and '"ok"' in body and "true" in body
    except OSError:
        return False


def probe(port: int) -> dict | None:
    banner = ""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=PROBE_TIMEOUT) as s:
            s.settimeout(PROBE_TIMEOUT)
            try:
                banner = s.recv(256).decode("utf-8", "replace").strip()
            except OSError:
                pass
            if not banner:
                try:
                    s.sendall(b"HEAD / HTTP/1.0\r\nHost: localhost\r\n\r\n")
                    banner = s.recv(512).decode("utf-8", "replace").strip()
                except OSError:
                    pass
    except OSError:
        return None

    is_http = banner.lower().startswith("http/")
    is_self = port == OWN_PORT

    if is_self:
        service = "Port Scanner — this tool"
    elif is_http and looks_like_wharf(port):
        service = "Wharf — dashboard"
    else:
        service = guess_from_banner(banner) or WELL_KNOWN.get(port, "Unknown")

    return {"port": port, "service": service, "banner": banner[:200], "http": is_http, "self": is_self}


def scan_all() -> dict:
    start = monotonic()
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for r in pool.map(probe, range(1, 65536)):
            if r:
                results.append(r)
    results.sort(key=lambda r: r["port"])
    return {"results": results, "elapsed_s": round(monotonic() - start, 2)}


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Port Scanner</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<script>
  // apply saved theme before first paint to avoid a flash — same mechanism as the
  // Wharf dashboard. localStorage is per-origin, so this can't read the dashboard's
  // own saved choice, but it follows the same OS light/dark preference by default.
  const savedTheme = localStorage.getItem("theme");
  if (savedTheme) document.documentElement.dataset.theme = savedTheme;
</script>
<style>
  :root {
    --bg: #0f1216; --panel: #171c23; --panel-2: #1e252e; --text: #e6e9ed;
    --muted: #93a0ae; --border: #2a333e; --accent: #4f9cf9; --green: #34c07c;
    --red: #e5534b;
  }
  @media (prefers-color-scheme: light) {
    :root:not([data-theme="dark"]) {
      --bg: #f5f7f9; --panel: #ffffff; --panel-2: #eef1f4; --text: #1c2733;
      --muted: #5b6b7b; --border: #d7dee5;
    }
  }
  :root[data-theme="light"] {
    --bg: #f5f7f9; --panel: #ffffff; --panel-2: #eef1f4; --text: #1c2733;
    --muted: #5b6b7b; --border: #d7dee5;
  }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, "Segoe UI", sans-serif; background: var(--bg); color: var(--text);
         margin: 0; }
  a { color: var(--accent); }
  .topbar {
    display: flex; align-items: center; justify-content: space-between;
    padding: 0.7rem 1.5rem; background: var(--panel); border-bottom: 1px solid var(--border);
    position: sticky; top: 0; z-index: 10;
  }
  .brand { font-weight: 700; font-size: 1.1rem; display: flex; align-items: center; gap: .5rem; }
  .icon-btn {
    display: inline-flex; align-items: center; justify-content: center;
    width: 34px; height: 34px; background: none; border: none; border-radius: 8px;
    color: var(--muted); cursor: pointer;
  }
  .icon-btn:hover { color: var(--text); background: var(--panel-2); }
  main { max-width: 960px; margin: 0 auto; padding: 1.5rem; }
  .sub { color: var(--muted); margin: 0 0 1.25rem; font-size: .9rem; }
  button.scan { background: var(--accent); color: white; border: none; border-radius: 8px;
                padding: .55rem 1.05rem; font-size: .9rem; cursor: pointer; }
  button.scan:hover { filter: brightness(1.08); }
  button.scan:disabled { opacity: .55; cursor: default; }
  .status { margin-left: .8rem; color: var(--muted); font-size: .85rem; }
  table { width: 100%; border-collapse: collapse; margin-top: 1.25rem; font-size: .88rem; }
  th, td { text-align: left; padding: .55rem .6rem; border-bottom: 1px solid var(--border); vertical-align: top; }
  th { color: var(--muted); font-weight: 600; text-transform: uppercase; font-size: .72rem; letter-spacing: .04em; }
  td.port { font-variant-numeric: tabular-nums; font-weight: 600; color: var(--accent); white-space: nowrap; }
  td.banner { color: var(--muted); font-family: ui-monospace, monospace; font-size: .78rem;
              max-width: 300px; overflow-wrap: anywhere; }
  td.preview { width: 176px; }
  .thumb {
    position: relative; display: block; width: 168px; height: 96px;
    border-radius: 8px; overflow: hidden; border: 1px solid var(--border);
    background: var(--panel-2);
  }
  .thumb:hover { border-color: var(--accent); }
  button.thumb-load {
    color: var(--muted); font-size: .78rem; cursor: pointer;
    font-family: inherit;
  }
  button.thumb-load:hover { color: var(--accent); border-color: var(--accent); }
  .thumb iframe {
    position: absolute; top: 0; left: 0; width: 400%; height: 400%;
    transform: scale(.25); transform-origin: 0 0; border: 0; pointer-events: none;
  }
  .thumb-empty { color: var(--muted); font-size: 1.3rem; }
  tr.self-row td { background: color-mix(in srgb, var(--accent) 8%, transparent); }
  .spinner { display: inline-block; width: 14px; height: 14px; border: 2px solid var(--border);
             border-top-color: var(--accent); border-radius: 50%; animation: spin .7s linear infinite;
             vertical-align: -2px; margin-right: .4rem; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .empty { color: var(--muted); padding: 2rem 0; text-align: center; }
</style>
</head>
<body>
  <header class="topbar">
    <span class="brand">🔍 Port Scanner</span>
    <button class="icon-btn" id="theme-toggle" title="Toggle theme" aria-label="Toggle theme"></button>
  </header>
  <main>
    <p class="sub">Probes every TCP port (1&ndash;65535) on <code>127.0.0.1</code> and identifies
      what answers. Ports that talk HTTP get a live preview &mdash; click one to open it.</p>
    <button class="scan" id="scan-btn" onclick="scan()">Scan again</button>
    <span class="status" id="status"></span>
    <div id="results"></div>
  </main>
  <script>
    const SUN = '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="5"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/></svg>';
    const MOON = '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';
    const themeBtn = document.getElementById("theme-toggle");
    function effectiveTheme() {
      return document.documentElement.dataset.theme ||
        (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
    }
    function renderThemeIcon() {
      themeBtn.innerHTML = effectiveTheme() === "dark" ? SUN : MOON;
    }
    themeBtn.addEventListener("click", () => {
      const next = effectiveTheme() === "dark" ? "light" : "dark";
      document.documentElement.dataset.theme = next;
      localStorage.setItem("theme", next);
      renderThemeIcon();
    });
    renderThemeIcon();

    function escapeHtml(s) {
      return s.replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
    }

    async function scan() {
      const btn = document.getElementById("scan-btn");
      const status = document.getElementById("status");
      const results = document.getElementById("results");
      btn.disabled = true;
      status.innerHTML = '<span class="spinner"></span>Scanning 65,535 ports…';
      results.innerHTML = "";
      try {
        const res = await fetch("/api/scan");
        const data = await res.json();
        status.textContent = `${data.results.length} open port(s) — scanned in ${data.elapsed_s}s`;
        if (!data.results.length) {
          results.innerHTML = '<p class="empty">Nothing open right now.</p>';
        } else {
          const rows = data.results.map(r => {
            const url = `http://127.0.0.1:${r.port}`;
            // never preview our own port — embedding this page inside itself recurses
            // (each nested copy tries to embed itself again).
            const previewable = r.http && !r.self;
            // loaded on click rather than automatically: with a dozen+ open ports,
            // eagerly loading every preview as live iframes at once (each one a full
            // page — some, like Wharf's own dashboard, with their own polling JS) bogs
            // the browser down fast. One click, one page, on demand.
            const preview = previewable
              ? `<button class="thumb thumb-load" data-url="${url}" title="Load preview of ${url}">▶ Preview</button>`
              : r.self
                ? `<span class="thumb-empty" title="This is Port Scanner itself">👋</span>`
                : `<span class="thumb-empty">&mdash;</span>`;
            const label = r.self ? `<strong>${r.service}</strong>` : r.service;
            return `
              <tr${r.self ? ' class="self-row"' : ""}>
                <td class="port">${r.port}</td>
                <td>${r.http && !r.self ? `<a href="${url}" target="_blank" rel="noopener">${r.service}</a>` : label}</td>
                <td class="preview">${preview}</td>
                <td class="banner">${r.banner ? escapeHtml(r.banner) : "&mdash;"}</td>
              </tr>`;
          }).join("");
          results.innerHTML = `<table>
            <thead><tr><th>Port</th><th>Service</th><th>Preview</th><th>Banner</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>`;
        }
      } catch (e) {
        status.textContent = "Scan failed — see tool logs.";
      } finally {
        btn.disabled = false;
      }
    }

    // click-to-load: swap a "Preview" button for a live (sandboxed) iframe thumbnail,
    // wrapped in a link so a second click on the thumbnail opens the real page.
    document.getElementById("results").addEventListener("click", (e) => {
      const btn = e.target.closest(".thumb-load");
      if (!btn) return;
      const url = btn.dataset.url;
      const a = document.createElement("a");
      a.className = "thumb";
      a.href = url;
      a.target = "_blank";
      a.rel = "noopener";
      a.title = `Open ${url}`;
      const iframe = document.createElement("iframe");
      iframe.src = url;
      iframe.tabIndex = -1;
      iframe.setAttribute("sandbox", "");
      iframe.referrerPolicy = "no-referrer";
      a.appendChild(iframe);
      btn.replaceWith(a);
    });

    scan();
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # keep the tool's log stream quiet; Wharf already tails stdout

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/scan":
            body = json.dumps(scan_all()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)

    def shutdown(*_args):
        print("SIGTERM received, shutting down")
        server.shutdown()

    signal.signal(signal.SIGTERM, shutdown)
    print(f"port-scanner listening on http://{HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
