"""
Ingest server API client for polling stream bitrate.
Supports multiple backends:
  - Oryx SRS: polls /api/v1/streams/ for recv_bytes / recv_30s
  - MediaMTX:  polls /v3/paths/list for bytesReceived deltas
  - Generic:   polls a user-supplied JSON endpoint; uses JSONPath-style
               key to extract a numeric bitrate value (kbps)

Bitrate modes (Oryx only):
  - Raw (default): Computes instantaneous bitrate from recv_bytes deltas
    between consecutive polls. Updates every poll interval with true
    real-time values. Uses a configurable averaging window.
  - SRS averaged: Reads recv_30s from the SRS API, which is a 30-second
    rolling average computed server-side. Only changes ~every 30s.

MediaMTX always uses raw (bytesReceived delta) mode.
"""
import time
from collections import deque
import requests
from PyQt6.QtCore import QThread, pyqtSignal


class IngestPoller(QThread):
    """Background thread that polls an ingest server API and emits bitrate."""

    bitrate_update = pyqtSignal(float, float, float)   # total_kbps, video_kbps, audio_kbps
    stream_online  = pyqtSignal(bool, str)              # is_online, stream_id
    connection_status = pyqtSignal(bool, str)            # connected, message
    error = pyqtSignal(str)

    def __init__(self, config_manager, parent=None):
        super().__init__(parent)
        self.cfg = config_manager
        self._running = False
        self._session = None

        # Raw bitrate computation: store (timestamp_ms, byte_count) samples
        self._byte_samples: deque = deque(maxlen=30)

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

    def _compute_raw_bitrate_srs(self, stream: dict) -> float:
        recv_bytes = int(stream.get("recv_bytes", 0))
        live_ms    = int(stream.get("live_ms", 0))

        self._byte_samples.append((live_ms, recv_bytes))

        if len(self._byte_samples) < 2:
            return 0.0

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

    def _poll_oryx(self, data: dict) -> tuple[bool, str, float]:
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

    def _poll_mediamtx(self, data: dict) -> tuple[bool, str, float]:
        """Parse MediaMTX response.

        /v3/paths/get/<name> returns a single path object directly.
        /v3/paths/list returns {items: [...]} but does NOT include bytesReceived
        (only the per-path GET endpoint does).
        """
        target_name = self.cfg.get("srs", "stream_name")

        # Detect which response shape we got
        if "items" in data:
            # /v3/paths/list response — find the matching ready path
            items = data.get("items", [])
            path = None
            for item in items:
                name = item.get("name", "")
                if target_name and name != target_name:
                    continue
                if item.get("ready", False):
                    path = item
                    break
            if not path and not target_name:
                for item in items:
                    if item.get("ready", False):
                        path = item
                        break
        else:
            # /v3/paths/get/<name> response — single object
            path = data if data.get("ready", False) else None

        if not path:
            self._byte_samples.clear()
            return False, "", 0.0

        # MediaMTX exposes bytesReceived at the top level of the path object
        # (NOT inside path.source — that only contains type and id)
        bytes_received = int(path.get("bytesReceived", 0))

        # Use wall-clock ms for delta computation
        now_ms = int(time.time() * 1000)
        self._byte_samples.append((now_ms, bytes_received))

        total = 0.0
        if len(self._byte_samples) >= 2:
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
                    self.bitrate_update.emit(total, total, 0.0)
                else:
                    self.stream_online.emit(False, "")
                    self.bitrate_update.emit(0.0, 0.0, 0.0)
                    self._byte_samples.clear()

            except requests.ConnectionError:
                consecutive_failures += 1
                self.connection_status.emit(False, "Cannot connect to server")
                if consecutive_failures >= max_fail_before_offline:
                    self.stream_online.emit(False, "")
                    self.bitrate_update.emit(0.0, 0.0, 0.0)
                    self._byte_samples.clear()
            except requests.Timeout:
                consecutive_failures += 1
                self.connection_status.emit(False, "Request timeout")
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
                else:
                    consecutive_failures += 1
                    self.connection_status.emit(False, f"HTTP {e.response.status_code}")
            except Exception as e:
                consecutive_failures += 1
                self.error.emit(str(e))

            self.msleep(interval_ms)

        if self._session:
            self._session.close()

    def stop(self):
        self._running = False
        self.wait(3000)