"""Tests for the NocoDB results sink (flatwatch_listings)."""

import json

import requests

from app.models import Listing
from app.results import ResultsSink
from tests.conftest import make_config


class FakeResp:
    def __init__(self, status=200):
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class FakeSession:
    def __init__(self, down=False):
        self.down = down
        self.posts = []

    def post(self, url, headers=None, data=None, timeout=None):
        if self.down:
            raise requests.ConnectionError("down")
        self.posts.append({"url": url, "rows": json.loads(data)})
        return FakeResp(200)


def _cfg(tmp_path, **over):
    base = dict(
        json_store_path=str(tmp_path / "seen.json"),
        nocodb_url="https://noco.example", nocodb_token="tok",
        nocodb_listings_table_id="listings",
    )
    base.update(over)
    return make_config(**base)


def _listing(i, **kw):
    base = dict(source="kleinanzeigen", title=f"Wohnung {i}", url=f"https://ka/{i}", native_id=str(i),
                price=900 + i, rooms=2, sqm=60, location="Berlin", description="schön")
    base.update(kw)
    return Listing.create(**base)


def test_write_bulk_payload_with_full_fields(tmp_path):
    sess = FakeSession()
    ResultsSink(_cfg(tmp_path), session=sess).write([_listing(1), _listing(2)])
    assert len(sess.posts) == 1                 # single bulk POST
    rows = sess.posts[0]["rows"]
    assert len(rows) == 2
    row = rows[0]
    assert set(row) >= {
        "listing_id", "title", "url", "source", "price", "rooms", "sqm",
        "location", "description", "first_seen",
    }
    assert row["listing_id"] == "kleinanzeigen:1"
    assert row["price"] == 901 and row["sqm"] == 60
    assert row["first_seen"]


def test_write_includes_rich_detail_and_availability_fields(tmp_path):
    sess = FakeSession()
    lst = _listing(1)
    lst.details = {"bedrooms": 2.0, "bathrooms": 1.0, "floor": "3", "warm_rent": 1360.0,
                   "apartment_type": "Etagenwohnung", "available_from": "Juli 2026",
                   "additional_costs": 180.0, "deposit": "2.360 €"}
    lst.features = ["Balkon", "Keller"]
    ResultsSink(_cfg(tmp_path), session=sess).write([lst])
    row = sess.posts[0]["rows"][0]
    assert row["bedrooms"] == 2.0 and row["bathrooms"] == 1.0 and row["floor"] == "3"
    assert row["warm_rent"] == 1360.0 and row["additional_costs"] == 180.0
    assert row["apartment_type"] == "Etagenwohnung" and row["available_from"] == "Juli 2026"
    assert row["deposit"] == "2.360 €"
    assert row["features"] == "Balkon, Keller"
    assert row["available"] is True and row["last_checked"]


def test_noop_when_table_id_unset(tmp_path):
    cfg = make_config(json_store_path=str(tmp_path / "seen.json"))  # no listings table id
    sess = FakeSession()
    sink = ResultsSink(cfg, session=sess)
    assert sink.enabled is False
    sink.write([_listing(1)])
    assert sess.posts == []


def test_write_surfaces_nocodb_error_body(tmp_path, caplog):
    """A NocoDB rejection (e.g. unknown column) is logged with its body, not hidden."""
    class ErrResp:
        status_code = 400
        text = '{"msg":"Field \'bedrooms\' not found in table"}'

        def raise_for_status(self):
            raise requests.HTTPError()

    class ErrSession:
        def post(self, *a, **k):
            return ErrResp()

    with caplog.at_level("WARNING"):
        ResultsSink(_cfg(tmp_path), session=ErrSession()).write([_listing(1)])
    assert "Results write FAILED" in caplog.text and "bedrooms" in caplog.text


def test_write_logs_when_table_unconfigured(tmp_path, caplog):
    cfg = make_config(json_store_path=str(tmp_path / "seen.json"))  # no listings table id
    with caplog.at_level("INFO"):
        ResultsSink(cfg, session=FakeSession()).write([_listing(1)])
    assert "Results table not configured" in caplog.text


def test_write_never_raises_on_failure(tmp_path, caplog):
    sess = FakeSession(down=True)
    with caplog.at_level("WARNING"):
        ResultsSink(_cfg(tmp_path), session=sess).write([_listing(1)])  # must not raise
    assert any("Could not write results" in r.message for r in caplog.records)


def test_write_empty_is_noop(tmp_path):
    sess = FakeSession()
    ResultsSink(_cfg(tmp_path), session=sess).write([])
    assert sess.posts == []
