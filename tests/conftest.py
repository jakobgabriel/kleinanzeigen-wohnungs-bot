"""Shared test fixtures. No test in this suite performs real network I/O."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.config import Config, Criteria

FIXTURES = Path(__file__).parent / "fixtures"


def make_config(**overrides) -> Config:
    """Build a Config with sane test defaults; override any field by keyword."""
    base = dict(
        ka_urls=["https://www.kleinanzeigen.de/s-wohnung-mieten/c203"],
        rss_urls=[],
        criteria=Criteria(),
        poll_interval_min=30,
        user_agent="test-agent",
        per_request_delay_s=0.0,
        request_jitter_s=0.0,
        http_timeout_s=5.0,
        max_retries=3,
        enrich_detail=False,
        ka_max_pages=1,
        persist_batch_size=25,
        ka_default_radius_km=None,
        recheck_enabled=True,
        recheck_interval_days=1,
        json_store_path="/tmp/flatwatch-test-seen.json",
        nocodb_url=None,
        nocodb_token=None,
        nocodb_table_id=None,
        nocodb_id_field="listing_id",
        nocodb_searches_table_id=None,
        nocodb_listings_table_id=None,
        telegram_token=None,
        telegram_chat_id=None,
        smtp_host=None,
        smtp_port=587,
        smtp_user=None,
        smtp_password=None,
        email_from=None,
        email_to=None,
        smtp_use_tls=True,
        ha_webhook_url=None,
        max_notify_per_cycle=15,
        health_path="/tmp/flatwatch-test-health.json",
        healthcheck_port=None,
        health_stale_after_min=0,
        failure_alert_threshold=3,
        alert_on_failures=True,
        mcp_enabled=False,
        mcp_host="127.0.0.1",
        mcp_port=8765,
        mcp_path="/mcp",
        mcp_auth_token=None,
        run_log_enabled=True,
        nocodb_runs_table_id=None,
        nocodb_run_events_table_id=None,
        run_log_retention_days=30,
        run_log_jsonl_path="/tmp/flatwatch-test-runs.jsonl",
        version="test",
    )
    base.update(overrides)
    return Config(**base)


@pytest.fixture
def config():
    return make_config()


@pytest.fixture
def tmp_config(tmp_path):
    return make_config(
        json_store_path=str(tmp_path / "seen.json"),
        health_path=str(tmp_path / "health.json"),
        run_log_jsonl_path=str(tmp_path / "runs.jsonl"),
    )


@pytest.fixture
def ka_html():
    return (FIXTURES / "kleinanzeigen_search.html").read_text(encoding="utf-8")
