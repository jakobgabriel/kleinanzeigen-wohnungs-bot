"""Tests for dynamic search configuration from NocoDB (with env fallback)."""

import requests

from app.config import Criteria
from app.searches import Search, SearchProvider
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


class FakeNoco:
    def __init__(self, rows=None, down=False):
        self.rows = rows or []
        self.down = down

    def get(self, url, headers=None, params=None, timeout=None):
        if self.down:
            raise requests.ConnectionError("down")
        offset = (params or {}).get("offset", 0)
        limit = (params or {}).get("limit", 200)
        return FakeResp(200, {"list": self.rows[offset : offset + limit]})


def _cfg(tmp_path, **over):
    base = dict(
        ka_urls=["https://www.kleinanzeigen.de/berlin"], rss_urls=["https://feed/rss"],
        criteria=Criteria(max_rent=1400, min_rooms=2, excluded_keywords=["tausch"]),
        json_store_path=str(tmp_path / "seen.json"),
        nocodb_url="https://noco.example", nocodb_token="tok",
        nocodb_searches_table_id="searches",
    )
    base.update(over)
    return make_config(**base)


# ----- env fallback -------------------------------------------------------- #
def test_env_searches_used_when_table_unconfigured(tmp_path):
    cfg = make_config(
        ka_urls=["https://ka/berlin", "https://ka/potsdam"], rss_urls=["https://feed/rss"],
        criteria=Criteria(max_rent=1000),
        json_store_path=str(tmp_path / "seen.json"),
    )
    searches = SearchProvider(cfg, FakeNoco()).get_searches()
    assert [s.source_type for s in searches] == ["kleinanzeigen", "kleinanzeigen", "rss"]
    assert all(s.criteria.max_rent == 1000 for s in searches)


def test_nocodb_unreachable_falls_back_to_env(tmp_path, caplog):
    cfg = _cfg(tmp_path)
    with caplog.at_level("WARNING"):
        searches = SearchProvider(cfg, FakeNoco(down=True)).get_searches()
    # env: 1 KA + 1 RSS
    assert {s.source_type for s in searches} == {"kleinanzeigen", "rss"}
    assert any("env vars" in r.message for r in caplog.records)


def test_empty_table_falls_back_to_env(tmp_path, caplog):
    cfg = _cfg(tmp_path)
    with caplog.at_level("WARNING"):
        searches = SearchProvider(cfg, FakeNoco(rows=[])).get_searches()
    assert len(searches) == 2  # the env searches
    assert any("falling back to env" in r.message for r in caplog.records)


def test_unreachable_reuses_last_known_good(tmp_path):
    cfg = _cfg(tmp_path)
    good = FakeNoco(rows=[{"url": "https://ka/x", "source_type": "kleinanzeigen", "enabled": True}])
    provider = SearchProvider(cfg, good)
    first = provider.get_searches()
    assert len(first) == 1 and first[0].url == "https://ka/x"
    # Now NocoDB goes down -> reuse cache, not env.
    provider._session = FakeNoco(down=True)
    second = provider.get_searches()
    assert second == first


# ----- row parsing + criteria merge ---------------------------------------- #
def test_rows_parsed_with_per_search_criteria(tmp_path):
    rows = [
        {"url": "https://ka/berlin", "source_type": "kleinanzeigen", "enabled": True,
         "max_rent": 1200, "min_rooms": 3, "excluded_keywords": "WG, Tausch"},
        {"url": "https://feed/rss", "source_type": "rss", "enabled": True},
    ]
    cfg = _cfg(tmp_path)
    searches = SearchProvider(cfg, FakeNoco(rows=rows)).get_searches()
    berlin = next(s for s in searches if "berlin" in s.url)
    assert berlin.criteria.max_rent == 1200          # row override
    assert berlin.criteria.min_rooms == 3            # row override
    assert berlin.criteria.excluded_keywords == ["wg", "tausch"]
    feed = next(s for s in searches if s.source_type == "rss")
    # blank cells inherit the global env defaults
    assert feed.criteria.max_rent == 1400
    assert feed.criteria.min_rooms == 2
    assert feed.criteria.excluded_keywords == ["tausch"]


def test_disabled_rows_excluded(tmp_path):
    rows = [
        {"url": "https://ka/on", "source_type": "kleinanzeigen", "enabled": True},
        {"url": "https://ka/off", "source_type": "kleinanzeigen", "enabled": False},
    ]
    cfg = _cfg(tmp_path)
    searches = SearchProvider(cfg, FakeNoco(rows=rows)).get_searches()
    assert [s.url for s in searches] == ["https://ka/on"]


def test_source_type_inferred_from_url_when_blank(tmp_path):
    rows = [
        {"url": "https://www.kleinanzeigen.de/x", "enabled": True},
        {"url": "https://other.example/feed.rss", "enabled": True},
    ]
    cfg = _cfg(tmp_path)
    searches = SearchProvider(cfg, FakeNoco(rows=rows)).get_searches()
    by_url = {s.url: s.source_type for s in searches}
    assert by_url["https://www.kleinanzeigen.de/x"] == "kleinanzeigen"
    assert by_url["https://other.example/feed.rss"] == "rss"


def test_rows_without_url_skipped(tmp_path):
    rows = [{"source_type": "rss", "enabled": True}, {"url": "https://ka/x", "enabled": True}]
    cfg = _cfg(tmp_path)
    searches = SearchProvider(cfg, FakeNoco(rows=rows)).get_searches()
    assert [s.url for s in searches] == ["https://ka/x"]


def test_enabled_defaults_true_when_blank(tmp_path):
    rows = [{"url": "https://ka/x", "source_type": "kleinanzeigen"}]
    cfg = _cfg(tmp_path)
    searches = SearchProvider(cfg, FakeNoco(rows=rows)).get_searches()
    assert len(searches) == 1 and searches[0].enabled is True


def test_schema_check_ok_and_skipped(tmp_path):
    cfg = _cfg(tmp_path)
    assert SearchProvider(cfg, FakeNoco(rows=[])).schema_check() is True
    cfg2 = make_config(json_store_path=str(tmp_path / "s.json"))  # no searches table
    assert SearchProvider(cfg2, FakeNoco()).schema_check() is False
