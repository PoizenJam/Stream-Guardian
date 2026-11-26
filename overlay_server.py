"""
Local web overlay for an OBS Browser Source (Stream Guardian 2.0).

Serves two things on http://127.0.0.1:<port>/ (or all interfaces when LAN
access is allowed, so an OBS on another PC can use it):

  /             the overlay page (transparent background)
  /stats.json   live data for every monitored stream

Overlay URL options (query string):
  stream=<name>        which stream to show (default: the first)
  hide_when_normal=1   only show the badge while protection is active
  show_bitrate=0       hide the kbps figure (state only)
  scale=1.5            size multiplier

The HTTP server runs on its own daemon thread. The GUI thread pushes
snapshots in with update(); requests only read a copy under a lock.
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _OverlayHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    # HTTPServer sets SO_REUSEADDR. On Windows that flag lets a second socket
    # bind a port another program is already listening on, so a port conflict
    # "succeeded" silently and requests were split between the two programs.
    # A plain bind on Windows fails properly and still allows an immediate
    # restart on the same port. Elsewhere SO_REUSEADDR is the right default.
    allow_reuse_address = sys.platform != "win32"

OVERLAY_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Stream Guardian overlay</title>
<style>
  html, body { margin: 0; background: transparent; overflow: hidden; }
  #badge {
    display: none; align-items: center; gap: .5em;
    font: 600 calc(20px * var(--scale)) "Segoe UI", system-ui, sans-serif;
    color: #fff; padding: .35em .8em; border-radius: .6em;
    background: rgba(20, 20, 24, .78); margin: 8px; width: max-content;
    text-shadow: 0 1px 2px rgba(0,0,0,.6);
  }
  #dot { width: .7em; height: .7em; border-radius: 50%; background: #3ec45c; }
  .LOW_BITRATE #dot { background: #e8a83c; }
  .DISCONNECTED #dot { background: #e04040; }
  .OFFLINE #dot { background: #777; }
  #kbps { opacity: .85; font-weight: 500; }
</style></head>
<body><div id="badge"><span id="dot"></span><span id="label"></span><span id="kbps"></span></div>
<script>
const q = new URLSearchParams(location.search);
const want = q.get("stream");
const hideNormal = q.get("hide_when_normal") === "1";
const showKbps = q.get("show_bitrate") !== "0";
document.documentElement.style.setProperty("--scale", parseFloat(q.get("scale") || "1") || 1);
const LABELS = {NORMAL: "Live", LOW_BITRATE: "Low bitrate", DISCONNECTED: "Reconnecting", OFFLINE: "Offline"};
const badge = document.getElementById("badge");
async function tick() {
  let s = null;
  try {
    const data = await (await fetch("stats.json", {cache: "no-store"})).json();
    s = (want && data.streams.find(x => x.name === want)) || data.streams[0] || null;
  } catch (e) { s = null; }
  const state = s ? s.state : "OFFLINE";
  badge.className = state;
  document.getElementById("label").textContent = LABELS[state] || state;
  document.getElementById("kbps").textContent =
    (s && showKbps && state !== "DISCONNECTED") ? Math.round(s.average_kbps).toLocaleString() + " kbps" : "";
  badge.style.display = (hideNormal && state === "NORMAL") ? "none" : "flex";
}
tick(); setInterval(tick, 1000);
</script></body></html>
"""


class OverlayServer:
    def __init__(self):
        self._server = None
        self._thread = None
        self._lock = threading.Lock()
        self._snapshots = {}  # stream_id -> dict
        self._order = []      # stream ids in display order
        self.bound = ("", 0)

    @property
    def running(self) -> bool:
        return self._server is not None

    def start(self, port: int, allow_lan: bool = False) -> str:
        """Start (or restart) serving. Returns "" on success, else the error."""
        self.stop()
        host = "0.0.0.0" if allow_lan else "127.0.0.1"
        snapshots = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # keep the console quiet

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path in ("/", "/overlay", "/overlay.html"):
                    body, ctype = OVERLAY_HTML.encode("utf-8"), "text/html; charset=utf-8"
                elif path == "/stats.json":
                    body = json.dumps(snapshots.payload()).encode("utf-8")
                    ctype = "application/json"
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        try:
            self._server = _OverlayHTTPServer((host, int(port)), Handler)
        except OSError as e:
            self._server = None
            return f"could not listen on {host}:{port} ({e})"
        self.bound = (host, int(port))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True,
                                        name="StreamGuardianOverlay")
        self._thread.start()
        return ""

    def stop(self):
        server, self._server = self._server, None
        if server:
            server.shutdown()
            server.server_close()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None

    def set_order(self, stream_ids: list):
        with self._lock:
            self._order = list(stream_ids)
            for sid in list(self._snapshots):
                if sid not in self._order:
                    del self._snapshots[sid]

    def update(self, stream_id: str, snapshot: dict):
        with self._lock:
            self._snapshots[stream_id] = dict(snapshot)

    def payload(self) -> dict:
        with self._lock:
            streams = [self._snapshots[sid] for sid in self._order if sid in self._snapshots]
        return {"streams": streams}
