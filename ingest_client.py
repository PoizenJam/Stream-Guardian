"""
Ingest server API client for polling stream bitrate.
Supports multiple backends:
  - Oryx SRS: polls /api/v1/streams/ for recv_bytes / recv_30s
  - MediaMTX:  polls /v3/paths/get/<name> for bytesReceived deltas
  - Generic:   polls a user-supplied JSON endpoint; uses JSONPath-style
               key to extract a numeric bitrate value (kbps)

Bitrate modes (Oryx only):
  - Raw (default): Computes instantaneous bitrate from recv_bytes deltas
    between consecutive polls. Updates every poll interval with true
    real-time values. Uses a configurable averaging window.
  - SRS averaged: Reads recv_30s from the SRS API, which is a 30-second
    rolling average computed server-side. Only changes ~every 30s.

MediaMTX always uses raw (byte-count delta) mode. When the publisher is
connected over SRT, the poller also reads its SRT connection stats
(/v3/srtconns/get/<id>: packetsReceived, packetsReceivedLoss,
packetsReceivedDrop, msRTT) and emits per-poll deltas on link_stats. The
legacy /v3/srtconns/ path is used because MediaMTX still serves it after the
September 2026 rename to /v3/srt/conns/, so it works on old and new servers.
"""
import time
from collections import deque
import requests
from PyQt6.QtCore import QThread, pyqtSignal


class IngestPoller(QThread):
    """Background thread that polls an ingest server API and emits bitrate."""

    bitrate_update = pyqtSignal(float, float, float)   # total_kbps, video_kbps, audio_kbps
    stream_online  = pyqtSignal(bool, str)              # is_online, stream_id
    # SRT link stats (MediaMTX only): {"available": False} or per-poll deltas
    # {"available": True, "received": n, "lost": n, "dropped": n, "rtt_ms": x}
    link_stats     = pyqtSignal(dict)
    connection_status = pyqtSignal(bool, str)            # connected, message
    error = pyqtSignal(str)

    def __init__(self, config_manager, parent=None):
        super().__init__(parent)
        self.cfg = config_manager
        self._running = False
        self._session = None

        # Raw bitrate computation: store (timestamp_ms, byte_count) samples
        self._byte_samples: deque = deque(maxlen=30)

        # SRT link stats: previous counters (conn_id, received, lost, dropped)
        self._srt_prev = None
        self._link_was_available = None

    # ------------------------------------------------------------------
    # URL / header helpers
    # ------------------------------------------------------------------

    def _build_url(self) -> str:
        backend = self.cfg.get("srs", "backend")
        scheme  = "https" if self.cfg.get("srs", "use_ssl") else "http"
        host    = self.cfg.get("srs", "host")
        port    = self.cfg.get("srs", "port")

        if backend == "mediamtx":
            stream_name = self.cfg.get("srs", "stream_name")
            if stream_name:
                # Direct path lookup — returns full per-path stats including bytesReceived
                return f"{scheme}://{host}:{port}/v3/paths/get/{stream_name}"
            # No specific stream — fall back to list (won't include bytesReceived,
            # but at least lets the user see paths exist)
            return f"{scheme}://{host}:{port}/v3/paths/list"
        elif backend == "generic":
            path = self.cfg.get("srs", "api_path")
            return f"{scheme}://{host}:{port}{path}"
        else:  # oryx (default)
            path = self.cfg.get("srs", "api_path")
            return f"{scheme}://{host}:{port}{path}"

    def _build_headers(self) -> dict:
        headers = {"Accept": "application/json"}
        token = self.cfg.get("srs", "auth_token")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    # ------------------------------------------------------------------
    # Oryx SRS helpers (unchanged from original srs_client.py)
    # ------------------------------------------------------------------

    def _find_srs_stream(self, streams: list) -> dict | None:
        target_app  = self.cfg.get("srs", "stream_app")
        target_name = self.cfg.get("srs", "stream_name")

        for s in streams:
            app  = s.get("app", "")
            name = s.get("name", "")
            is_active = s.get("publish", {}).get("active", True)
            if target_app and app != target_app:
                continue
            if target_name and name != target_name:
                continue
            if is_active or not target_name:
                return s

        if not target_name:
            for s in streams:
                if not target_app or s.get("app", "") == target_app:
                    if s.get("publish", {}).get("active", True):
                        return s
        return None

    def _compute_raw_bitrate_srs(self, stream: dict) -> float | None:
        recv_bytes = int(stream.get("recv_bytes", 0))
        live_ms    = int(stream.get("live_ms", 0))

        self._byte_samples.append((live_ms, recv_bytes))

        # One sample can't give a rate. Returning 0 here used to feed a fake
        # 0 kbps reading into the average on every reconnect.
        if len(self._byte_samples) < 2:
            return None

        window = min(
            int(self.cfg.get("srs", "raw_bitrate_avg_window")),
            len(self._byte_samples) - 1,
        )
        window = max(window, 1)

        samples = list(self._byte_samples)
        recent  = samples[-(window + 1):]

        dt_ms   = recent[-1][0] - recent[0][0]
        d_bytes = recent[-1][1] - recent[0][1]

        if dt_ms > 0 and d_bytes >= 0:
            return float((d_bytes * 8) / dt_ms)  # kbps
        return 0.0

    def _extract_srs_averaged(self, stream: dict) -> float:
        kbps = stream.get("kbps", {})
        if isinstance(kbps, dict):
            return float(kbps.get("recv_30s", 0))
        if isinstance(kbps, (int, float)):
            return float(kbps)
        publish = stream.get("publish", {})
        if isinstance(publish, dict):
            pub_kbps = publish.get("kbps", {})
            if isinstance(pub_kbps, dict):
                return float(pub_kbps.get("recv_30s", 0))
        return 0.0

    def _poll_oryx(self, data: dict) -> tuple[bool, str, float | None]:
        """Returns (stream_found, stream_id, total_kbps)."""
        if data.get("code", -1) != 0:
            return False, "", 0.0

        streams = data.get("streams", [])
        stream  = self._find_srs_stream(streams)
        if not stream:
            self._byte_samples.clear()
            return False, "", 0.0

        use_raw = self.cfg.get("srs", "use_raw_bitrate")
        total   = self._compute_raw_bitrate_srs(stream) if use_raw else self._extract_srs_averaged(stream)
        sid     = stream.get("id", stream.get("name", "unknown"))
        return True, str(sid), total

    # ------------------------------------------------------------------
    # MediaMTX helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _mtx_publisher_online(path: dict) -> bool:
        """Newer MediaMTX: "online" = a publisher is connected, while the
        deprecated "ready" means "available to readers", which stays true
        with alwaysAvailable fallback content even after the publisher drops.
        Older servers only have "ready"."""
        if "online" in path:
            return bool(path["online"])
        return bool(path.get("ready", False))

    def _poll_mediamtx(self, data: dict) -> tuple[bool, str, float | None]:
        """Parse MediaMTX response.

        /v3/paths/get/<name> returns a single path object directly.
        /v3/paths/list returns {items: [...]} but does NOT include byte
        counters (only the per-path GET endpoint does).
        """
        target_name = self.cfg.get("srs", "stream_name")
        online = self._mtx_publisher_online

        # Detect which response shape we got
        if "items" in data:
            # /v3/paths/list response — find the matching online path
            items = data.get("items", [])
            path = None
            for item in items:
                name = item.get("name", "")
                if target_name and name != target_name:
                    continue
                if online(item):
                    path = item
                    break
            if not path and not target_name:
                for item in items:
                    if online(item):
                        path = item
                        break
        else:
            # /v3/paths/get/<name> response — single object
            path = data if online(data) else None

        if not path:
            self._byte_samples.clear()
            self._emit_link_unavailable()
            return False, "", 0.0

        self._poll_srt_link(path)

        # Byte counter at the top level of the path object (not in path.source).
        # "inboundBytes" replaced the deprecated "bytesReceived" in 2026.
        bytes_received = int(path.get("inboundBytes", path.get("bytesReceived", 0)))

        # Use wall-clock ms for delta computation
        now_ms = int(time.time() * 1000)
        self._byte_samples.append((now_ms, bytes_received))

        total = None  # first sample after (re)connect: no rate yet
        if len(self._byte_samples) >= 2:
            total = 0.0
            window = min(
                int(self.cfg.get("srs", "raw_bitrate_avg_window")),
                len(self._byte_samples) - 1,
            )
            window = max(window, 1)
            samples = list(self._byte_samples)
            recent  = samples[-(window + 1):]
            dt_ms   = recent[-1][0] - recent[0][0]
            d_bytes = recent[-1][1] - recent[0][1]
            if dt_ms > 0 and d_bytes >= 0:
                total = float((d_bytes * 8) / dt_ms)

        sid = path.get("name", "unknown")
        return True, sid, total

    def _emit_link_unavailable(self):
        self._srt_prev = None
        if self._link_was_available is not False:
            self._link_was_available = False
            self.link_stats.emit({"available": False})

    def _poll_srt_link(self, path: dict):
        """Read SRT stats for the path's publisher, if it is an SRT connection."""
        source = path.get("source") or {}
        if not self.cfg.get("srs", "link_stats_enabled") or source.get("type") != "srtConn" \
                or not source.get("id"):
            self._emit_link_unavailable()
            return
        scheme = "https" if self.cfg.get("srs", "use_ssl") else "http"
        url = (f"{scheme}://{self.cfg.get('srs', 'host')}:{self.cfg.get('srs', 'port')}"
               f"/v3/srtconns/get/{source['id']}")
        try:
            resp = self._session.get(url, headers=self._build_headers(), timeout=3)
            resp.raise_for_status()
            stats = self.parse_srt_conn(resp.json(), self._srt_prev)
        except Exception:
            # Link stats are optional; never let them break bitrate monitoring
            self._emit_link_unavailable()
            return
        self._srt_prev = stats.pop("_counters")
        self._link_was_available = True
        self.link_stats.emit(stats)

    @staticmethod
    def parse_srt_conn(conn: dict, prev) -> dict:
        """Turn one /v3/srtconns/get response into per-poll deltas.
        prev is the "_counters" tuple from the previous call (or None). A new
        connection id or a counter going backwards restarts from zero deltas."""
        cid = conn.get("id", "")
        received = int(conn.get("packetsReceived", 0))
        lost = int(conn.get("packetsReceivedLoss", 0))
        dropped = int(conn.get("packetsReceivedDrop", 0))
        counters = (cid, received, lost, dropped)
        if prev is None or prev[0] != cid or received < prev[1] or lost < prev[2] or dropped < prev[3]:
            deltas = (0, 0, 0)
        else:
            deltas = (received - prev[1], lost - prev[2], dropped - prev[3])
        return {
            "available": True,
            "received": deltas[0],
            "lost": deltas[1],
            "dropped": deltas[2],
            "rtt_ms": float(conn.get("msRTT", 0.0)),
            "_counters": counters,
        }

    # ------------------------------------------------------------------
    # Generic endpoint helpers
    # ------------------------------------------------------------------

    def _poll_generic(self, data) -> tuple[bool, str, float]:
        """Extract a bitrate value from an arbitrary JSON response.

        Uses the configured generic_bitrate_key as a dot-separated path
        into the JSON (e.g. "stream.bitrate_kbps" or just "bitrate").
        """
        key_path = self.cfg.get("srs", "generic_bitrate_key")
        if not key_path:
            return False, "", 0.0

        # Walk the key path
        val = data
        try:
            for part in key_path.split("."):
                if isinstance(val, list):
                    val = val[int(part)]
                elif isinstance(val, dict):
                    val = val[part]
                else:
                    return False, "", 0.0
            kbps = float(val)
            return True, "generic", kbps
        except (KeyError, IndexError, ValueError, TypeError):
            return False, "", 0.0

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self):
        self._running = True
        self._session = requests.Session()
        self._byte_samples.clear()
        consecutive_failures = 0
        max_fail_before_offline = 3

        while self._running:
            interval_ms = self.cfg.get("srs", "poll_interval_ms")
            backend     = self.cfg.get("srs", "backend")

            try:
                url     = self._build_url()
                headers = self._build_headers()
                resp    = self._session.get(url, headers=headers, timeout=5)
                resp.raise_for_status()
                data    = resp.json()

                # Dispatch to backend-specific parser
                if backend == "mediamtx":
                    found, sid, total = self._poll_mediamtx(data)
                elif backend == "generic":
                    found, sid, total = self._poll_generic(data)
                else:
                    found, sid, total = self._poll_oryx(data)

                # Connection succeeded
                self.connection_status.emit(True, "Connected")
                consecutive_failures = 0

                if found:
                    self.stream_online.emit(True, sid)
                    if total is not None:
                        self.bitrate_update.emit(total, total, 0.0)
                else:
                    self.stream_online.emit(False, "")
                    self.bitrate_update.emit(0.0, 0.0, 0.0)
                    self._byte_samples.clear()
                    self._emit_link_unavailable()

            except (requests.ConnectionError, requests.Timeout) as e:
                # A hung server (timeout) is as unreachable as a refused one.
                # Timeouts used to be excluded, so the engine received no
                # samples at all and could never react.
                consecutive_failures += 1
                is_timeout = isinstance(e, requests.Timeout)
                self.connection_status.emit(
                    False, "Request timeout" if is_timeout else "Cannot connect to server"
                )
                self._offline_if_persistent(consecutive_failures, max_fail_before_offline)
            except requests.HTTPError as e:
                # MediaMTX returns 404 from /v3/paths/get/<n> when the path
                # doesn't exist (i.e. publisher disconnected and on-demand path
                # was destroyed). Treat as "no stream" rather than an API error
                # so the disconnect threshold can fire.
                if backend == "mediamtx" and e.response.status_code == 404:
                    self.connection_status.emit(True, "Connected")
                    consecutive_failures = 0
                    self.stream_online.emit(False, "")
                    self.bitrate_update.emit(0.0, 0.0, 0.0)
                    self._byte_samples.clear()
                    self._emit_link_unavailable()
                else:
                    consecutive_failures += 1
                    self.connection_status.emit(False, f"HTTP {e.response.status_code}")
                    self._offline_if_persistent(consecutive_failures, max_fail_before_offline)
            except Exception as e:
                consecutive_failures += 1
                self.error.emit(str(e))
                self._offline_if_persistent(consecutive_failures, max_fail_before_offline)

            # Sleep in slices so stop() isn't stuck behind a long poll interval
            remaining = int(interval_ms)
            while self._running and remaining > 0:
                step = min(100, remaining)
                self.msleep(step)
                remaining -= step

        if self._session:
            self._session.close()

    def _offline_if_persistent(self, failures: int, limit: int):
        """Any repeated failure (refused, timeout, HTTP error, bad JSON) means
        the stream can't be seen; report it offline so the disconnect logic
        can act. Previously only refused connections did this."""
        if failures >= limit:
            self.stream_online.emit(False, "")
            self.bitrate_update.emit(0.0, 0.0, 0.0)
            self._byte_samples.clear()
            self._emit_link_unavailable()

    def stop(self):
        self._running = False
        # An in-flight request can take up to its 5 s timeout; the sleep
        # between polls is interruptible, so 7 s covers the worst case.
        self.wait(7000)
