"""Integration tests for the poll cycle: silent prime, dedup, batching (A1)."""

import json

import requests

from app import main as main_mod
from app.models import Listing
from app.notify import Notifier
from app.runlog import RunLogger
from app.store import SeenStore
from tests.conftest import make_config


class DummySession:
    def post(self, *a, **k):
        return _Ok()

    def get(self, *a, **k):
        return _Ok({"list": []})


class _Ok:
    def __init__(self, payload=None):
        self._payload = payload or {}
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _make(tmp_path, listings, **cfg_over):
    opts = dict(
        ka_urls=["https://ka.example/s"],
        rss_urls=[],
        json_store_path=str(tmp_path / "seen.json"),
        run_log_jsonl_path=str(tmp_path / "runs.jsonl"),
        run_log_enabled=False,
        health_path=str(tmp_path / "health.json"),
    )
    opts.update(cfg_over)
    cfg = make_config(**opts)
    store = SeenStore(cfg, session=DummySession())
    notifier = Notifier(cfg, session=DummySession())
    runlogger = RunLogger(cfg, session=DummySession())
    return cfg, store, notifier, runlogger


def _patch_fetch(monkeypatch, listings):
    def fake_ka(url, **kw):
        return listings
    monkeypatch.setattr(main_mod.sources, "fetch_kleinanzeigen", fake_ka)
    monkeypatch.setattr(main_mod.sources, "polite_pause", lambda *a, **k: None)


def _listing(i, **kw):
    base = dict(source="kleinanzeigen", title=f"Wohnung {i}", url=f"https://ka/ad/{i}", native_id=str(i))
    base.update(kw)
    return Listing.create(**base)


def test_silent_prime_notifies_nothing(tmp_path, monkeypatch):
    listings = [_listing(1), _listing(2)]
    _patch_fetch(monkeypatch, listings)
    cfg, store, notifier, runlogger = _make(tmp_path, listings)

    sent = []
    monkeypatch.setattr(notifier, "notify", lambda l: sent.append(l))

    stats = main_mod.run_cycle(cfg, store, notifier, runlogger, DummySession(), prime=True)
    assert sent == []  # nothing notified on prime
    assert stats["new_count"] == 2
    # All primed listings now marked seen.
    assert store.is_new("kleinanzeigen:1") is False
    assert store.is_new("kleinanzeigen:2") is False


def test_second_cycle_notifies_only_new(tmp_path, monkeypatch):
    listings = [_listing(1), _listing(2)]
    _patch_fetch(monkeypatch, listings)
    cfg, store, notifier, runlogger = _make(tmp_path, listings)

    # Prime with the first two.
    main_mod.run_cycle(cfg, store, notifier, runlogger, DummySession(), prime=True)

    # Add a third listing and run a scheduled cycle.
    listings.append(_listing(3))
    notified = []
    monkeypatch.setattr(notifier, "notify", lambda l: notified.append(l) or _Result())

    stats = main_mod.run_cycle(cfg, store, notifier, runlogger, DummySession(), prime=False)
    assert [l.listing_id for l in notified] == ["kleinanzeigen:3"]
    assert stats["new_count"] == 1
    assert stats["notified"] == 1


class _Result:
    telegram_ok = None
    email_ok = None
    ha_ok = None
    any_sent = False
    any_failed = False


def test_batching_guard_caps_individual_and_summarizes(tmp_path, monkeypatch):
    listings = [_listing(i) for i in range(20)]
    _patch_fetch(monkeypatch, listings)
    cfg, store, notifier, runlogger = _make(tmp_path, listings, max_notify_per_cycle=15)

    individual = []
    summaries = []
    monkeypatch.setattr(notifier, "notify", lambda l: individual.append(l) or _Result())
    monkeypatch.setattr(notifier, "send_summary", lambda l: summaries.append(l))

    stats = main_mod.run_cycle(cfg, store, notifier, runlogger, DummySession(), prime=False)
    assert len(individual) == 15
    assert len(summaries) == 1 and len(summaries[0]) == 5
    # All 20 marked seen despite the cap.
    assert all(store.is_new(f"kleinanzeigen:{i}") is False for i in range(20))


def test_filter_excludes_out_of_criteria(tmp_path, monkeypatch):
    from app.config import Criteria
    listings = [_listing(1, price=2000), _listing(2, price=900)]
    _patch_fetch(monkeypatch, listings)
    cfg, store, notifier, runlogger = _make(tmp_path, listings, criteria=Criteria(max_rent=1000))

    notified = []
    monkeypatch.setattr(notifier, "notify", lambda l: notified.append(l) or _Result())
    main_mod.run_cycle(cfg, store, notifier, runlogger, DummySession(), prime=False)
    assert [l.listing_id for l in notified] == ["kleinanzeigen:2"]


def test_multiple_ka_areas_all_polled_under_global_criteria(tmp_path, monkeypatch):
    """B3: several KA search URLs (areas) are each polled; global criteria apply."""
    from app.config import Criteria

    per_url = {
        "https://ka.example/berlin": [_listing(1, price=900)],
        "https://ka.example/potsdam": [_listing(2, price=2000), _listing(3, price=800)],
    }

    def fake_ka(url, **kw):
        return per_url[url]

    monkeypatch.setattr(main_mod.sources, "fetch_kleinanzeigen", fake_ka)
    monkeypatch.setattr(main_mod.sources, "polite_pause", lambda *a, **k: None)

    cfg, store, notifier, runlogger = _make(
        tmp_path, [], ka_urls=list(per_url), criteria=Criteria(max_rent=1000)
    )
    notified = []
    monkeypatch.setattr(notifier, "notify", lambda l: notified.append(l) or _Result())
    stats = main_mod.run_cycle(cfg, store, notifier, runlogger, DummySession(), prime=False)

    assert stats["sources_polled"] == 2
    # The over-budget Potsdam listing is filtered out; both areas contributed.
    assert sorted(l.listing_id for l in notified) == ["kleinanzeigen:1", "kleinanzeigen:3"]


def test_cycle_survives_source_exception(tmp_path, monkeypatch):
    def boom(url, **kw):
        raise main_mod.sources.FetchError("503 fail")
    monkeypatch.setattr(main_mod.sources, "fetch_kleinanzeigen", boom)
    monkeypatch.setattr(main_mod.sources, "polite_pause", lambda *a, **k: None)
    cfg, store, notifier, runlogger = _make(tmp_path, [])
    stats = main_mod.run_cycle(cfg, store, notifier, runlogger, DummySession(), prime=False)
    # Cycle completes despite the source failing.
    assert stats["new_count"] == 0
    assert stats["status"] in ("partial", "failed")


def _spy_trigger(monkeypatch, runlogger):
    captured = {}
    orig = runlogger.start

    def spy(trigger="scheduled"):
        captured["trigger"] = trigger
        return orig(trigger=trigger)

    monkeypatch.setattr(runlogger, "start", spy)
    return captured


def test_trigger_derived_scheduled_and_prime(tmp_path, monkeypatch):
    _patch_fetch(monkeypatch, [_listing(1)])
    cfg, store, notifier, runlogger = _make(tmp_path, [])
    monkeypatch.setattr(notifier, "notify", lambda l: _Result())

    cap = _spy_trigger(monkeypatch, runlogger)
    main_mod.run_cycle(cfg, store, notifier, runlogger, DummySession(), prime=True)
    assert cap["trigger"] == "startup_prime"
    main_mod.run_cycle(cfg, store, notifier, runlogger, DummySession(), prime=False)
    assert cap["trigger"] == "scheduled"


def test_trigger_manual_override(tmp_path, monkeypatch):
    _patch_fetch(monkeypatch, [_listing(1)])
    cfg, store, notifier, runlogger = _make(tmp_path, [])
    monkeypatch.setattr(notifier, "notify", lambda l: _Result())

    cap = _spy_trigger(monkeypatch, runlogger)
    main_mod.run_cycle(cfg, store, notifier, runlogger, DummySession(), prime=False, trigger="manual")
    assert cap["trigger"] == "manual"


def test_sigusr1_sets_manual_trigger_and_sleep_wakes(monkeypatch):
    # Handler flips the flag; _sleep_interval returns promptly while it's set.
    monkeypatch.setattr(main_mod, "_TRIGGER_NOW", False)
    main_mod._handle_trigger(getattr(main_mod.signal, "SIGUSR1", 10), None)
    assert main_mod._TRIGGER_NOW is True
    assert main_mod._sleep_interval(30) is True  # would otherwise block 30 min
    # reset module global so other tests are unaffected
    monkeypatch.setattr(main_mod, "_TRIGGER_NOW", False)
