"""Health/heartbeat signal (A3, #6).

After every cycle the loop writes ``/data/health.json`` with the last-cycle
stats.  When ``HEALTHCHECK_PORT`` is set a tiny stdlib HTTP server exposes the
same payload at ``GET /health``.  The endpoint returns **200** when healthy and
**503** when the service is alive but failing — either the heartbeat is stale (no
completed cycle within the staleness window) or too many cycles have failed in a
row — so Docker's HEALTHCHECK (and any external monitor) can actually catch it.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

log = logging.getLogger("flatwatch.health")

# Shared in-memory snapshot of the most recent cycle, updated via write_health().
_LATEST: dict = {"status": "starting", "last_cycle": None, "new_count": 0}
_LOCK = threading.Lock()


def _is_healthy(snap: dict, now: float) -> bool:
    """Healthy unless too many consecutive failures or a stale heartbeat (#6).

    Before the first cycle (no ``last_cycle_epoch``) the service is considered
    healthy so it isn't marked down during startup.
    """
    threshold = snap.get("fail_threshold")
    if threshold and snap.get("consecutive_failures", 0) >= threshold:
        return False
    epoch = snap.get("last_cycle_epoch")
    stale_after = snap.get("stale_after_s")
    if epoch and stale_after and now > epoch + stale_after:
        return False
    return True


def write_health(path: str, payload: dict) -> None:
    """Persist the heartbeat to disk and update the in-memory snapshot."""
    with _LOCK:
        _LATEST.clear()
        _LATEST.update(payload)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("Could not write health file %s: %s", path, exc)


def snapshot() -> dict:
    with _LOCK:
        return dict(_LATEST)


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (stdlib naming)
        if self.path.rstrip("/") not in ("/health", "/healthz", ""):
            self.send_response(404)
            self.end_headers()
            return
        snap = snapshot()
        healthy = _is_healthy(snap, time.time())
        snap["healthy"] = healthy
        body = json.dumps(snap).encode("utf-8")
        self.send_response(200 if healthy else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence default request logging
        return


def start_health_server(port: Optional[int]) -> Optional[ThreadingHTTPServer]:
    """Start the health HTTP server in a daemon thread (if a port is set)."""
    if not port:
        return None
    server = ThreadingHTTPServer(("0.0.0.0", port), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="health")
    thread.start()
    log.info("Health endpoint listening on :%d/health", port)
    return server
