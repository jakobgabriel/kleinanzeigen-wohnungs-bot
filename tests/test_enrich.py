"""Tests for detail-page enrichment (B2)."""

from pathlib import Path

import requests

from app import main as main_mod
from app import sources
from app.config import Criteria
from app.models import Listing
from app.notify import Notifier
from app.runlog import RunLogger
from app.sources import _parse_kleinanzeigen_detail, enrich_listing
from app.store import SeenStore
from tests.conftest import make_config

FIXTURES = Path(__file__).parent / "fixtures"


def _detail_html():
    return (FIXTURES / "kleinanzeigen_detail.html").read_text(encoding="utf-8")


# ----- parser -------------------------------------------------------------- #
def test_parse_detail_extracts_fields():
    fields = _parse_kleinanzeigen_detail(_detail_html())
    assert fields["price"] == 1180.0
    assert fields["sqm"] == 58.0
    assert fields["rooms"] == 2.5
    assert "provisionsfrei" in fields["description"].lower()


def test_parse_detail_on_garbage_returns_empty():
    assert _parse_kleinanzeigen_detail("<html><body>nope</body></html>") == {}


# ----- enrich_listing ------------------------------------------------------ #
def test_enrich_fills_only_missing():
    lst = Listing.create(
        source="kleinanzeigen", title="WG", url="https://x/1", native_id="1",
        price=None, rooms=None, sqm=45.0, _missing=("price", "rooms"),
    )
    enrich_listing(lst, {"price": 900.0, "rooms": 2.0, "sqm": 99.0})
    assert lst.price == 900.0
    assert lst.rooms == 2.0
    assert lst.sqm == 45.0  # known value is never overwritten
    assert lst._missing == ()


# ----- cycle integration --------------------------------------------------- #
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


class _Result:
    telegram_ok = email_ok = ha_ok = None
    any_sent = False
    any_failed = False


def _wire(tmp_path, **over):
    opts = dict(
        ka_urls=["https://ka.example/s"], rss_urls=[],
        json_store_path=str(tmp_path / "seen.json"),
        run_log_jsonl_path=str(tmp_path / "runs.jsonl"), run_log_enabled=False,
        health_path=str(tmp_path / "health.json"),
        enrich_detail=True,
    )
    opts.update(over)
    cfg = make_config(**opts)
    return cfg, SeenStore(cfg, DummySession()), Notifier(cfg, DummySession()), RunLogger(cfg, DummySession())


def test_enrichment_disabled_by_default(tmp_path, monkeypatch):
    listing = Listing.create(source="kleinanzeigen", title="WG", url="https://ka/ad/1",
                             native_id="1", _missing=("price", "rooms", "sqm"))
    monkeypatch.setattr(main_mod.sources, "fetch_kleinanzeigen", lambda u, **k: [listing])
    monkeypatch.setattr(main_mod.sources, "polite_pause", lambda *a, **k: None)
    calls = []
    monkeypatch.setattr(main_mod.sources, "fetch_kleinanzeigen_detail", lambda u, **k: calls.append(u) or {})

    cfg, store, notifier, rl = _wire(tmp_path, enrich_detail=False)
    monkeypatch.setattr(notifier, "notify", lambda l: _Result())
    main_mod.run_cycle(cfg, store, notifier, rl, DummySession(), prime=False)
    assert calls == []  # no detail fetches when disabled


def test_enrichment_fills_then_keeps_matching(tmp_path, monkeypatch):
    listing = Listing.create(source="kleinanzeigen", title="WG", url="https://ka/ad/1",
                             native_id="1", price=None, rooms=None, sqm=None,
                             _missing=("price", "rooms", "sqm"))
    monkeypatch.setattr(main_mod.sources, "fetch_kleinanzeigen", lambda u, **k: [listing])
    monkeypatch.setattr(main_mod.sources, "polite_pause", lambda *a, **k: None)
    monkeypatch.setattr(main_mod.sources, "fetch_kleinanzeigen_detail",
                        lambda u, **k: {"price": 900.0, "rooms": 2.0, "sqm": 60.0})

    cfg, store, notifier, rl = _wire(tmp_path, criteria=Criteria(max_rent=1000))
    notified = []
    monkeypatch.setattr(notifier, "notify", lambda l: notified.append(l) or _Result())
    main_mod.run_cycle(cfg, store, notifier, rl, DummySession(), prime=False)
    assert [l.listing_id for l in notified] == ["kleinanzeigen:1"]
    assert notified[0].price == 900.0


def test_enrichment_drops_now_overbudget(tmp_path, monkeypatch):
    listing = Listing.create(source="kleinanzeigen", title="WG", url="https://ka/ad/1",
                             native_id="1", price=None, _missing=("price",))
    monkeypatch.setattr(main_mod.sources, "fetch_kleinanzeigen", lambda u, **k: [listing])
    monkeypatch.setattr(main_mod.sources, "polite_pause", lambda *a, **k: None)
    # Detail reveals the rent is over budget -> should be re-filtered out.
    monkeypatch.setattr(main_mod.sources, "fetch_kleinanzeigen_detail", lambda u, **k: {"price": 2500.0})

    cfg, store, notifier, rl = _wire(tmp_path, criteria=Criteria(max_rent=1000))
    notified = []
    monkeypatch.setattr(notifier, "notify", lambda l: notified.append(l) or _Result())
    stats = main_mod.run_cycle(cfg, store, notifier, rl, DummySession(), prime=False)
    assert notified == []
    assert stats["new_count"] == 0


def test_enrichment_skips_already_complete(tmp_path, monkeypatch):
    listing = Listing.create(source="kleinanzeigen", title="WG", url="https://ka/ad/1",
                             native_id="1", price=900, rooms=2, sqm=60, _missing=())
    monkeypatch.setattr(main_mod.sources, "fetch_kleinanzeigen", lambda u, **k: [listing])
    monkeypatch.setattr(main_mod.sources, "polite_pause", lambda *a, **k: None)
    calls = []
    monkeypatch.setattr(main_mod.sources, "fetch_kleinanzeigen_detail", lambda u, **k: calls.append(u) or {})

    cfg, store, notifier, rl = _wire(tmp_path)
    monkeypatch.setattr(notifier, "notify", lambda l: _Result())
    main_mod.run_cycle(cfg, store, notifier, rl, DummySession(), prime=False)
    assert calls == []  # nothing missing -> no detail fetch
