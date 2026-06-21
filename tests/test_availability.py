"""Tests for the daily availability recheck (tag removed listings)."""

import json

import requests

from app import sources
from app.availability import AvailabilityChecker
from tests.conftest import make_config


class FakeResp:
    def __init__(self, status=200, text="", payload=None):
        self.status_code = status
        self.text = text
        self._payload = payload or {}
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)

    def json(self):
        return self._payload


class FakeSession:
    """Routes NocoDB record reads/writes vs. listing-page fetches by URL."""

    def __init__(self, rows, listings):
        self.rows = rows
        self.listings = listings  # url -> "alive" | "removed" | "404"
        self.patched = []

    def get(self, url, headers=None, params=None, timeout=None):
        if "/records" in url:
            params = params or {}
            off, lim = params.get("offset", 0), params.get("limit", 200)
            return FakeResp(200, payload={"list": self.rows[off:off + lim]})
        kind = self.listings.get(url, "alive")
        if kind == "404":
            return FakeResp(404)
        text = "Diese Anzeige ist nicht mehr verfügbar" if kind == "removed" else "<html>ok</html>"
        return FakeResp(200, text=text)

    def patch(self, url, headers=None, data=None, timeout=None):
        self.patched.extend(json.loads(data))
        return FakeResp(200)


def _cfg(tmp_path, **over):
    base = dict(
        json_store_path=str(tmp_path / "seen.json"),
        nocodb_url="https://noco", nocodb_token="t", nocodb_listings_table_id="listings",
        per_request_delay_s=0.0, request_jitter_s=0.0,
    )
    base.update(over)
    return make_config(**base)


def _row(i, source="kleinanzeigen", available=True):
    return {"Id": i, "listing_id": f"k:{i}", "url": f"https://ka/{i}", "source": source, "available": available}


def test_recheck_marks_removed_and_stamps_alive(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "polite_pause", lambda *a, **k: None)
    rows = [_row(1), _row(2), _row(3)]
    listings = {"https://ka/1": "alive", "https://ka/2": "removed", "https://ka/3": "404"}
    sess = FakeSession(rows, listings)
    removed = AvailabilityChecker(_cfg(tmp_path), session=sess).run()
    assert removed == 2
    by_id = {u["Id"]: u for u in sess.patched}
    assert set(by_id[1]) == {"Id", "last_checked"}        # alive -> only timestamp
    assert by_id[2]["available"] is False and by_id[2]["removed_at"]
    assert by_id[3]["available"] is False                  # 404 -> removed


def test_recheck_skips_rss_and_already_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "polite_pause", lambda *a, **k: None)
    rows = [_row(1, source="rss"), _row(2, available=False)]
    sess = FakeSession(rows, {})
    assert AvailabilityChecker(_cfg(tmp_path), session=sess).run() == 0
    assert sess.patched == []


def test_recheck_blocked_does_not_mark_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "polite_pause", lambda *a, **k: None)

    def boom(*a, **k):
        raise sources.FetchError("403 blocked", blocked=True)

    monkeypatch.setattr(sources, "http_get", boom)
    sess = FakeSession([_row(1)], {})
    assert AvailabilityChecker(_cfg(tmp_path), session=sess).run() == 0
    assert sess.patched == []  # unknown verdict -> left untouched


def test_recheck_noop_when_disabled(tmp_path):
    sess = FakeSession([_row(1)], {"https://ka/1": "removed"})
    assert AvailabilityChecker(_cfg(tmp_path, recheck_enabled=False), session=sess).run() == 0
    assert sess.patched == []


def test_recheck_disabled_without_listings_table(tmp_path):
    cfg = make_config(json_store_path=str(tmp_path / "seen.json"))  # no listings table id
    assert AvailabilityChecker(cfg).enabled is False


def test_recheck_read_failure_is_non_fatal(tmp_path, caplog):
    class DownSession(FakeSession):
        def get(self, *a, **k):
            raise requests.ConnectionError("down")

    with caplog.at_level("WARNING"):
        assert AvailabilityChecker(_cfg(tmp_path), session=DownSession([], {})).run() == 0
