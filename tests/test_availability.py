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


def _row(i, source="kleinanzeigen", available=True, status=None):
    row = {"Id": i, "listing_id": f"k:{i}", "url": f"https://ka/{i}", "source": source, "available": available}
    if status is not None:
        row["status"] = status
    return row


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
    assert by_id[2]["status"] == "expired"                 # untouched -> auto-expire
    assert by_id[3]["available"] is False                  # 404 -> removed


def test_recheck_expires_only_untouched_status(tmp_path, monkeypatch):
    """A removed ad auto-expires only when status is new/unset; user edits stay."""
    monkeypatch.setattr(sources, "polite_pause", lambda *a, **k: None)
    rows = [_row(1, status="new"), _row(2, status="interested"), _row(3)]  # 3 has no status
    listings = {f"https://ka/{i}": "removed" for i in (1, 2, 3)}
    sess = FakeSession(rows, listings)
    AvailabilityChecker(_cfg(tmp_path), session=sess).run()
    by_id = {u["Id"]: u for u in sess.patched}
    assert by_id[1]["status"] == "expired"                 # status "new" -> expired
    assert "status" not in by_id[2]                        # "interested" preserved
    assert by_id[3]["status"] == "expired"                 # unset -> expired
    assert all(u["available"] is False for u in sess.patched)


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


def test_recheck_read_404_logs_clear_hint(tmp_path, caplog):
    """A 404 on the records read is non-fatal and names the likely cause."""
    class NotFoundSession(FakeSession):
        def get(self, url, headers=None, params=None, timeout=None):
            return FakeResp(404, text='{"msg":"Table not found"}')

    with caplog.at_level("WARNING"):
        assert AvailabilityChecker(_cfg(tmp_path), session=NotFoundSession([], {})).run() == 0
    assert "HTTP 404" in caplog.text and "NOCODB_LISTINGS_TABLE_ID" in caplog.text


def test_recheck_read_sends_no_fields_projection(tmp_path, monkeypatch):
    """The read must not send a `fields` projection (a column mismatch 404s it)."""
    monkeypatch.setattr(sources, "polite_pause", lambda *a, **k: None)
    seen_params = []

    class RecordingSession(FakeSession):
        def get(self, url, headers=None, params=None, timeout=None):
            if "/records" in url:
                seen_params.append(params or {})
            return super().get(url, headers=headers, params=params, timeout=timeout)

    sess = RecordingSession([_row(1)], {"https://ka/1": "alive"})
    AvailabilityChecker(_cfg(tmp_path), session=sess).run()
    assert seen_params and all("fields" not in p for p in seen_params)
