"""Tests for the dedup union store with NocoDB mocked (A1)."""

import json

import requests

from app.store import SeenStore
from tests.conftest import make_config


class FakeResp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        return self._payload


class FakeNocoSession:
    """Simulates a NocoDB records endpoint backed by an in-memory set."""

    def __init__(self, down=False):
        self.rows = []
        self.down = down
        self.inserts = 0

    def get(self, url, headers=None, params=None, timeout=None):
        if self.down:
            raise requests.ConnectionError("nocodb down")
        where = (params or {}).get("where", "")
        if "eq," in where:
            wanted = where.split("eq,")[1].rstrip(")")
            hit = [r for r in self.rows if r.get("listing_id") == wanted]
            return FakeResp(200, {"list": hit[:1]})
        return FakeResp(200, {"list": self.rows[:1]})

    def post(self, url, headers=None, data=None, timeout=None):
        if self.down:
            raise requests.ConnectionError("nocodb down")
        self.inserts += 1
        self.rows.append(json.loads(data))
        return FakeResp(200, {})


def _nocodb_config(tmp_path):
    return make_config(
        json_store_path=str(tmp_path / "seen.json"),
        nocodb_url="https://nocodb.example",
        nocodb_token="tok",
        nocodb_table_id="tbl1",
    )


def test_json_only_dedup(tmp_path):
    cfg = make_config(json_store_path=str(tmp_path / "seen.json"))
    store = SeenStore(cfg, session=FakeNocoSession())
    assert store.is_new("rss:1") is True
    store.mark_seen("rss:1")
    assert store.is_new("rss:1") is False


def test_dedup_persists_across_instances(tmp_path):
    path = str(tmp_path / "seen.json")
    cfg = make_config(json_store_path=path)
    SeenStore(cfg, session=FakeNocoSession()).mark_seen("rss:99")
    fresh = SeenStore(make_config(json_store_path=path), session=FakeNocoSession())
    assert fresh.is_new("rss:99") is False


def test_union_new_only_if_absent_from_both(tmp_path):
    cfg = _nocodb_config(tmp_path)
    sess = FakeNocoSession()
    store = SeenStore(cfg, session=sess)
    # Present in NocoDB but not JSON -> not new.
    sess.rows.append({"listing_id": "kleinanzeigen:5"})
    assert store.is_new("kleinanzeigen:5") is False
    # Absent from both -> new.
    assert store.is_new("kleinanzeigen:404") is True


def test_mark_seen_writes_both_stores(tmp_path):
    cfg = _nocodb_config(tmp_path)
    sess = FakeNocoSession()
    store = SeenStore(cfg, session=sess)
    store.mark_seen("kleinanzeigen:7", extra={"title": "t", "url": "u"})
    assert sess.inserts == 1
    assert {"listing_id": "kleinanzeigen:7", "title": "t", "url": "u"} == sess.rows[0]
    # JSON also has it.
    with open(cfg.json_store_path) as fh:
        assert "kleinanzeigen:7" in json.load(fh)["seen"]


def test_nocodb_down_degrades_to_json(tmp_path, caplog):
    cfg = _nocodb_config(tmp_path)
    sess = FakeNocoSession(down=True)
    store = SeenStore(cfg, session=sess)
    with caplog.at_level("WARNING"):
        # NocoDB unreachable -> treated as not-present, so listing is new.
        assert store.is_new("kleinanzeigen:1") is True
        store.mark_seen("kleinanzeigen:1")
    # Still recorded in JSON despite NocoDB being down.
    assert store.is_new("kleinanzeigen:1") is False
    assert any("degrading to JSON" in r.message for r in caplog.records)
