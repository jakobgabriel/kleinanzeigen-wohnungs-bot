"""Tests for the manual-trigger plumbing and the MCP server bootstrap."""

import importlib.util
import threading
import time

import pytest

from app import main as main_mod
from app import mcp_server
from tests.conftest import make_config

HAS_FASTMCP = importlib.util.find_spec("fastmcp") is not None


# ----- request_manual_run (used by the MCP trigger_run tool) ---------------- #
def test_request_manual_run_async_sets_flag(monkeypatch):
    monkeypatch.setattr(main_mod, "_TRIGGER_NOW", False)
    out = main_mod.request_manual_run(wait=False)
    assert main_mod._TRIGGER_NOW is True
    assert out["scheduled"] is True and "last_cycle" in out
    monkeypatch.setattr(main_mod, "_TRIGGER_NOW", False)


def test_request_manual_run_wait_completes_when_cycle_done(monkeypatch):
    monkeypatch.setattr(main_mod, "_TRIGGER_NOW", False)

    # Simulate the poll loop finishing the manual cycle shortly after it's scheduled.
    def finisher():
        time.sleep(0.05)
        main_mod._CYCLE_DONE.set()

    threading.Thread(target=finisher, daemon=True).start()
    out = main_mod.request_manual_run(wait=True, timeout=5.0)
    assert out["completed"] is True and out["scheduled"] is True
    monkeypatch.setattr(main_mod, "_TRIGGER_NOW", False)


def test_request_manual_run_wait_times_out(monkeypatch):
    monkeypatch.setattr(main_mod, "_TRIGGER_NOW", False)
    main_mod._CYCLE_DONE.clear()
    out = main_mod.request_manual_run(wait=True, timeout=0.1)  # nobody sets the event
    assert out["completed"] is False
    monkeypatch.setattr(main_mod, "_TRIGGER_NOW", False)


# ----- start_mcp_server ----------------------------------------------------- #
def test_start_mcp_server_noop_when_disabled():
    cfg = make_config(mcp_enabled=False)
    called = []
    server = mcp_server.start_mcp_server(
        cfg, request_run=lambda **k: called.append(k) or {}, get_status=lambda: {}
    )
    assert server is None and called == []


@pytest.mark.skipif(not HAS_FASTMCP, reason="fastmcp not installed")
def test_start_mcp_server_starts_thread_on_ephemeral_port():
    cfg = make_config(mcp_enabled=True, mcp_host="127.0.0.1", mcp_port=0)
    server = mcp_server.start_mcp_server(
        cfg,
        request_run=lambda wait=False: {"scheduled": True},
        get_status=lambda: {"status": "ok"},
        list_searches=lambda: [{"label": "x"}],
    )
    try:
        assert server is not None
        assert any(t.name == "mcp" for t in threading.enumerate())
    finally:
        if server is not None:
            server.should_exit = True
