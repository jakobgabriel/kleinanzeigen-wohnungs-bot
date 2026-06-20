"""Tests for the heartbeat write + snapshot and 503 health logic (A3, #6)."""

import json
import socket
import time

import requests

from app import health
from app.health import _is_healthy


def _healthy_snap(**over):
    snap = {
        "consecutive_failures": 0,
        "fail_threshold": 3,
        "last_cycle_epoch": time.time(),
        "stale_after_s": 100,
    }
    snap.update(over)
    return snap


def test_is_healthy_when_fresh_and_no_failures():
    assert _is_healthy(_healthy_snap(), time.time()) is True


def test_is_healthy_during_startup_no_epoch():
    assert _is_healthy({"status": "starting"}, time.time()) is True


def test_unhealthy_on_consecutive_failures():
    assert _is_healthy(_healthy_snap(consecutive_failures=3), time.time()) is False


def test_unhealthy_on_stale_heartbeat():
    now = time.time()
    assert _is_healthy(_healthy_snap(last_cycle_epoch=now - 200), now) is False


def test_health_endpoint_returns_503_when_unhealthy(tmp_path):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = health.start_health_server(port)
    try:
        health.write_health(str(tmp_path / "h.json"), _healthy_snap(consecutive_failures=5, status="failed"))
        r = requests.get(f"http://127.0.0.1:{port}/health", timeout=5)
        assert r.status_code == 503 and r.json()["healthy"] is False

        health.write_health(str(tmp_path / "h.json"), _healthy_snap(status="success"))
        r2 = requests.get(f"http://127.0.0.1:{port}/health", timeout=5)
        assert r2.status_code == 200 and r2.json()["healthy"] is True
    finally:
        server.shutdown()


def test_write_health_persists_and_snapshots(tmp_path):
    path = str(tmp_path / "health.json")
    payload = {"status": "success", "last_cycle": "2026-06-20T00:00:00Z", "new_count": 3}
    health.write_health(path, payload)
    on_disk = json.loads(open(path).read())
    assert on_disk == payload
    assert health.snapshot()["new_count"] == 3


def test_start_health_server_disabled_returns_none():
    assert health.start_health_server(None) is None
