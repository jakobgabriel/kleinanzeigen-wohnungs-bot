"""Tests for the heartbeat write + snapshot (A3)."""

import json

from app import health


def test_write_health_persists_and_snapshots(tmp_path):
    path = str(tmp_path / "health.json")
    payload = {"status": "success", "last_cycle": "2026-06-20T00:00:00Z", "new_count": 3}
    health.write_health(path, payload)
    on_disk = json.loads(open(path).read())
    assert on_disk == payload
    assert health.snapshot()["new_count"] == 3


def test_start_health_server_disabled_returns_none():
    assert health.start_health_server(None) is None
