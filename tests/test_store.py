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
        params = params or {}
        if "offset" in params:  # paginated seen-id load (begin_cycle)
            offset, limit = params["offset"], params["limit"]
            return FakeResp(200, {"list": self.rows[offset : offset + limit]})
        return FakeResp(200, {"list": self.rows[:1]})  # schema_check / limit=1 probe

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
    # Present in NocoDB but not JSON -> not new (loaded once via begin_cycle).
    sess.rows.append({"listing_id": "kleinanzeigen:5"})
    store = SeenStore(cfg, session=sess)
    store.begin_cycle()
    assert store.is_new("kleinanzeigen:5") is False
    # Absent from both -> new.
    assert store.is_new("kleinanzeigen:404") is True


def test_is_new_does_not_query_per_listing(tmp_path):
    """Reads are served from the in-memory cache — no GET per is_new call (#2)."""
    cfg = _nocodb_config(tmp_path)

    class CountingSession(FakeNocoSession):
        def __init__(self):
            super().__init__()
            self.gets = 0

        def get(self, *a, **k):
            self.gets += 1
            return super().get(*a, **k)

    sess = CountingSession()
    sess.rows.extend({"listing_id": f"k:{i}"} for i in range(5))
    store = SeenStore(cfg, session=sess)
    store.begin_cycle()
    gets_after_load = sess.gets
    for i in range(100):
        store.is_new(f"k:{i}")
    assert sess.gets == gets_after_load  # zero additional network reads


def test_begin_cycle_retries_after_nocodb_recovers(tmp_path):
    cfg = _nocodb_config(tmp_path)
    sess = FakeNocoSession(down=True)
    sess.rows.append({"listing_id": "k:1"})
    store = SeenStore(cfg, session=sess)
    store.begin_cycle()  # fails -> not loaded, degrades to JSON
    assert store.is_new("k:1") is True
    sess.down = False
    store.begin_cycle()  # recovers -> loads cache
    assert store.is_new("k:1") is False


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


def test_schema_check_ok(tmp_path):
    cfg = _nocodb_config(tmp_path)
    sess = FakeNocoSession()
    sess.rows.append({"listing_id": "kleinanzeigen:1", "title": "x"})
    assert SeenStore(cfg, session=sess).schema_check() is True


def test_schema_check_missing_id_field_warns(tmp_path, caplog):
    cfg = _nocodb_config(tmp_path)
    sess = FakeNocoSession()
    sess.rows.append({"wrong_field": "x"})  # id field absent from records
    with caplog.at_level("WARNING"):
        assert SeenStore(cfg, session=sess).schema_check() is False
    assert any("id field" in r.message for r in caplog.records)


def test_schema_check_unreachable_falls_back(tmp_path, caplog):
    cfg = _nocodb_config(tmp_path)
    with caplog.at_level("WARNING"):
        assert SeenStore(cfg, session=FakeNocoSession(down=True)).schema_check() is False
    assert any("schema check failed" in r.message for r in caplog.records)


def test_schema_check_skipped_without_nocodb(tmp_path):
    cfg = make_config(json_store_path=str(tmp_path / "seen.json"))
    assert SeenStore(cfg, session=FakeNocoSession()).schema_check() is False


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
