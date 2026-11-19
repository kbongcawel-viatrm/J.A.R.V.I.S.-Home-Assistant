#!/usr/bin/env python3
"""Kuma-style status dashboard — single static page, Python stdlib only.

Serves one HTML page that auto-refreshes monitor status:
  - ping checks (100% reply required)
  - HTTP checks (expects 200)
  - optional SSH host checks (memory/cpu) via --ssh

Usage:
    python3 dashboard.py [--port 8080] [--interval 30]

Monitors are configured in MONITORS below. Results + heartbeat history
(last 60 checks) are kept in memory.
"""

import argparse
import json
import shutil
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------- monitors
# type: "ping" | "http" | "ssh"
MONITORS = [
    {"name": "BRIDGE (LAN)", "type": "ping", "target": "192.168.1.51"},
    {"name": "ISP (WAN)",      "type": "ping", "target": "192.168.1.1"},
    # {"name": "Suricata Logs",  "type": "http", "target": "http://192.168.1.58:8080/"},
]

SCRIPT_DIR = __file__.rsplit("/", 1)[0]
SSH_TARGET = None          # e.g. "user@192.168.1.58" — enables host mem/cpu card
HISTORY_LEN = 60           # heartbeats kept per monitor

_state = {}                # name -> {"up": bool, "ms": float|None, "history": [(up, ms)]}
_state_lock = threading.Lock()


# ---------------------------------------------------------------- checks

def _tcp_alive(host, port=22, timeout=2):
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def check_ping(target):
    """Return (up, ms). Up requires all 4 replies."""
    t0 = time.time()
    if shutil.which("ping"):
        try:
            out = subprocess.run(
                ["ping", "-c", "4", "-W", "2", target],
                capture_output=True, text=True, timeout=15)
            text = out.stdout + out.stderr
            import re
            recv = re.search(r"(\d+)\s+(?:packets?\s+)?received", text)
            loss = re.search(r"([\d.]+)%\s+packet\s+loss", text)
            up = bool(recv and recv.group(1) == "4"
                      and (not loss or float(loss.group(1)) == 0.0))
            return up, (time.time() - t0) * 1000
        except (OSError, subprocess.TimeoutExpired):
            pass
    return _tcp_alive(target), (time.time() - t0) * 1000


def check_http(url):
    t0 = time.time()
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status == 200, (time.time() - t0) * 1000
    except Exception:
        return False, (time.time() - t0) * 1000


def check_ssh_host():
    """Return dict {"mem_pct": float, "load1": float, "cpus": int} or None."""
    if not SSH_TARGET or not shutil.which("ssh"):
        return None
    cmd = ("free -m | awk '/Mem:/{print $3\" \"$2}'; "
           "cut -d' ' -f1 /proc/loadavg; nproc")
    try:
        out = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=4", "-o", "BatchMode=yes",
             SSH_TARGET, cmd],
            capture_output=True, text=True, timeout=15)
        if out.returncode != 0:
            return None
        lines = [l.strip() for l in out.stdout.splitlines() if l.strip()]
        if len(lines) < 3:
            return None
        used, total = lines[0].split()
        load1 = float(lines[1].split()[0])
        cpus = int(lines[2])
        return {"mem_pct": int(used) / int(total) * 100,
                "load1": load1, "cpus": cpus}
    except Exception:
        return None


def run_check(m):
    if m["type"] == "ping":
        return check_ping(m["target"])
    if m["type"] == "http":
        return check_http(m["target"] if "://" in m["target"]
                          else f"http://{m['target']}")
    return False, None


def poll_loop(interval):
    host_info = None
    while True:
        host_info = check_ssh_host() if SSH_TARGET else None
        for m in MONITORS:
            up, ms = run_check(m)
            with _state_lock:
                s = _state.setdefault(m["name"], {"history": []})
                s["up"] = up
                s["ms"] = round(ms, 1) if ms is not None else None
                s["history"].append((up, s["ms"]))
                s["history"] = s["history"][-HISTORY_LEN:]
        with _state_lock:
            _state["__host__"] = host_info or {}
        time.sleep(interval)


# ---------------------------------------------------------------- page

PAGE = """<!doctype html>
<html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gondar</title>
<style>
  * { box-sizing: border-box; margin: 0; }
  body { background:#0d1117; color:#c9d1d9; font:14px/1.5 -apple-system,"Segoe UI",Roboto,sans-serif; padding:24px; }
  .wrap { max-width:720px; margin:0 auto; }
  h1 { font-size:20px; font-weight:600; margin-bottom:4px; }
  .sub { color:#8b949e; font-size:12px; margin-bottom:20px; }
  .card { background:#161b22; border:1px solid #30363d; border-radius:8px; padding:14px 16px; margin-bottom:12px; }
  .row { display:flex; align-items:center; gap:12px; }
  .dot { width:10px; height:10px; border-radius:50%; flex:none; }
  .up .dot { background:#3fb950; box-shadow:0 0 8px #3fb95088; }
  .down .dot { background:#f85149; box-shadow:0 0 8px #f8514988; }
  .unknown .dot { background:#6e7681; }
  .name { font-weight:600; flex:1; }
  .meta { color:#8b949e; font-size:12px; font-variant-numeric:tabular-nums; }
  .bars { display:flex; gap:2px; margin-top:10px; }
  .bar { flex:1; height:14px; border-radius:2px; background:#21262d; }
  .bar.up { background:#238636; }
  .bar.down { background:#da3633; }
  .bar.unknown { background:#30363d; }
  .host { display:flex; gap:24px; flex-wrap:wrap; }
  .host .stat b { color:#e6edf3; }
  footer { margin-top:20px; color:#6e7681; font-size:11px; text-align:center; }
</style></head><body><div class="wrap">
<h1><img src="/cf.svg" alt="" style="height:28px;vertical-align:-6px;margin-right:8px"><img src="/eye.svg" alt="" style="height:28px;vertical-align:-6px;margin-right:8px">Gondar</h1>
<div class="sub" id="ts">loading…</div>
<div id="monitors"></div>
<div class="card" id="hostcard" style="display:none"><div class="host" id="hoststats"></div></div>
<footer>auto-refreshes every <span id="iv">30</span>s · script-only dashboard</footer>
</div>
<script>
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function tick(){
  try {
    const r = await fetch('/api'); const d = await r.json();
    document.getElementById('ts').textContent = 'updated ' + new Date().toLocaleTimeString();
    const el = document.getElementById('monitors'); el.innerHTML = '';
    for (const [name, m] of Object.entries(d.monitors)) {
      const up = m.up, cls = up===true?'up':up===false?'down':'unknown';
      const ms = m.ms!=null ? m.ms.toFixed(0)+' ms' : '—';
      const uptime = m.history.length ? Math.round(m.history.filter(h=>h[0]).length/m.history.length*100) : 0;
      const bars = m.history.map(h => `<div class="bar ${h[0]===true?'up':h[0]===false?'down':'unknown'}" title="${h[1]??'—'}ms"></div>`).join('');
      el.insertAdjacentHTML('beforeend',
        `<div class="card ${cls}"><div class="row">
          <span class="dot"></span><span class="name">${esc(name)}</span>
          <span class="meta">${up===true?'UP':up===false?'DOWN':'?'}</span>
          <span class="meta">${ms}</span>
          <span class="meta">${uptime}%</span></div>
         <div class="bars">${bars}</div></div>`);
    }
    const h = d.host;
    if (h && Object.keys(h).length) {
      document.getElementById('hostcard').style.display='';
      document.getElementById('hoststats').innerHTML =
        `<span class="stat">Fedora Host<br><b>${h.mem_pct.toFixed(0)}%</b> mem</span>
         <span class="stat">load<br><b>${h.load1.toFixed(2)}</b> / ${h.cpus} cores</span>`;
    }
  } catch(e) { document.getElementById('ts').textContent = 'backend unreachable'; }
}
document.getElementById('iv').textContent = REFRESH;
tick(); setInterval(tick, REFRESH*1000);
</script></body></html>"""


# ---------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype="text/html; charset=utf-8", code=200):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path.startswith("/ingest"):
            try:
                n = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(n) or b"{}")
                name = str(data.get("name", "")).strip()
                if not name:
                    self._send('{"error":"name required"}', "application/json", 400)
                    return
                up = data.get("up")
                ms = data.get("ms")
                extra = data.get("extra", {})
                with _state_lock:
                    s = _state.setdefault(name, {"history": []})
                    s["up"] = up if isinstance(up, bool) else None
                    s["ms"] = ms
                    s["extra"] = extra
                    s["history"].append((s["up"], s["ms"]))
                    s["history"] = s["history"][-HISTORY_LEN:]
                self._send('{"ok":true}', "application/json")
            except Exception as e:
                self._send(json.dumps({"error": str(e)}), "application/json", 400)
        else:
            self._send("not found", "text/plain", 404)

    def do_GET(self):
        if self.path.endswith(".svg"):
            name = self.path.rsplit("/", 1)[-1]
            if name in ("cf.svg", "eye.svg"):
                try:
                    data = open(SCRIPT_DIR + "/" + name, "rb").read()
                    self._send(data, "image/svg+xml")
                    return
                except OSError:
                    pass
            self._send("not found", "text/plain", 404)
        elif self.path.startswith("/api"):
            with _state_lock:
                monitors = {k: dict(v) for k, v in _state.items()
                            if not k.startswith("__")}
                host = _state.get("__host__", {})
            self._send(json.dumps({"monitors": monitors, "host": host}),
                       "application/json")
        else:
            self._send(PAGE.replace("REFRESH", str(REFRESH)))


def main():
    global SSH_TARGET, REFRESH
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--interval", type=int, default=60, dest="REFRESH_VAL")
    ap.add_argument("--ssh", default=None)
    args = ap.parse_args()
    SSH_TARGET = args.ssh
    REFRESH = args.REFRESH_VAL

    threading.Thread(target=poll_loop, args=(args.REFRESH_VAL,), daemon=True).start()
    print(f"dashboard on http://0.0.0.0:{args.port}  (refresh {args.REFRESH_VAL}s)")
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
REFRESH = 30
