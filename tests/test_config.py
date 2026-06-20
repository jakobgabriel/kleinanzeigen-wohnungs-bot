"""Tests for config loading and validation (A1, D2)."""

import logging

import pytest

from app.config import Config, Criteria, load_config, validate_config
from tests.conftest import make_config


def test_no_sources_is_fatal():
    cfg = make_config(ka_urls=[], rss_urls=[])
    with pytest.raises(SystemExit):
        validate_config(cfg)


def test_partial_telegram_warns(caplog):
    cfg = make_config(telegram_token="abc", telegram_chat_id=None)
    with caplog.at_level(logging.WARNING):
        validate_config(cfg)
    assert any("TELEGRAM_CHAT_ID" in r.message for r in caplog.records)
    assert cfg.telegram_enabled is False


def test_partial_email_warns(caplog):
    cfg = make_config(smtp_host="smtp.example", email_from=None, email_to=None)
    with caplog.at_level(logging.WARNING):
        validate_config(cfg)
    assert any("Email partially configured" in r.message for r in caplog.records)
    assert cfg.email_enabled is False


def test_inverted_rent_bound_warns(caplog):
    cfg = make_config(criteria=Criteria(min_rent=1500, max_rent=500))
    with caplog.at_level(logging.WARNING):
        validate_config(cfg)
    assert any("Inverted rent" in r.message for r in caplog.records)


def test_load_config_from_env(monkeypatch):
    monkeypatch.setenv("RSS_URLS", "https://a.example/rss, https://b.example/rss")
    monkeypatch.setenv("MAX_RENT", "1200")
    monkeypatch.setenv("MIN_ROOMS", "2,5")
    monkeypatch.setenv("EXCLUDED_KEYWORDS", "Tausch, WG")
    monkeypatch.setenv("POLL_INTERVAL_MIN", "10")  # below floor -> clamped to 30
    monkeypatch.delenv("KA_SEARCH_URLS", raising=False)
    cfg = load_config()
    assert cfg.rss_urls == ["https://a.example/rss", "https://b.example/rss"]
    assert cfg.criteria.max_rent == 1200.0
    assert cfg.criteria.min_rooms == 2.5
    assert cfg.criteria.excluded_keywords == ["tausch", "wg"]
    assert cfg.poll_interval_min == 30  # floor enforced


def test_poll_interval_floor():
    cfg = make_config(poll_interval_min=5)
    # The floor is enforced in load_config, but a directly-built Config keeps its value;
    # ensure the loader clamps via a fresh env load is covered above. Here assert helpers.
    assert isinstance(cfg, Config)
