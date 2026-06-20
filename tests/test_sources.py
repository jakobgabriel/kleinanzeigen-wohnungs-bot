"""Tests for the KA parser, RSS parser, and HTTP retry/backoff (A1, A2, B1).

All HTTP is faked — no network calls.
"""

import pytest
import requests

from app import sources
from app.sources import FetchError, _parse_kleinanzeigen, _parse_rss, http_get


# --------------------------------------------------------------------------- #
# Kleinanzeigen parser (B1)
# --------------------------------------------------------------------------- #
def test_parse_kleinanzeigen_fixture(ka_html):
    listings = _parse_kleinanzeigen(ka_html)
    assert len(listings) == 4
    first = listings[0]
    assert first.listing_id == "kleinanzeigen:2901234567"
    assert first.title == "Schöne 3-Zimmer-Wohnung mit Balkon"
    assert first.price == 1250.0
    assert first.rooms == 3.0
    assert first.sqm == 82.0
    assert first.location == "10115 Berlin Mitte"
    assert first.url.startswith("https://www.kleinanzeigen.de/s-anzeige/")
    assert first.thumbnail and first.thumbnail.startswith("https://img.kleinanzeigen.de")


def test_card_missing_price_and_rooms_degrades(ka_html):
    listings = _parse_kleinanzeigen(ka_html)
    wg = next(l for l in listings if l.listing_id == "kleinanzeigen:2907654321")
    assert wg.price is None
    assert wg.rooms is None
    assert wg.sqm == 45.0
    assert "price" in wg._missing and "rooms" in wg._missing


def test_card_decimal_rooms(ka_html):
    listings = _parse_kleinanzeigen(ka_html)
    tausch = next(l for l in listings if l.listing_id == "kleinanzeigen:2905555555")
    assert tausch.rooms == 2.5
    assert tausch.sqm is None


def test_card_without_thumbnail(ka_html):
    listings = _parse_kleinanzeigen(ka_html)
    apt = next(l for l in listings if l.listing_id == "kleinanzeigen:2908888888")
    assert apt.thumbnail is None
    assert apt.price == 650.0


def test_zero_cards_warns(caplog):
    with caplog.at_level("WARNING"):
        result = _parse_kleinanzeigen("<html><body>no cards here</body></html>")
    assert result == []
    assert any("0 cards parsed" in r.message for r in caplog.records)


# --------------------------------------------------------------------------- #
# RSS parser
# --------------------------------------------------------------------------- #
RSS_SAMPLE = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Feed</title>
  <item>
    <title>3 Zimmer Wohnung 75 m fuer 1.100 Euro Miete</title>
    <link>https://feed.example/listing/1</link>
    <guid>listing-1</guid>
    <description>Schoene Wohnung mit Balkon, 3 Zimmer, 75 m2.</description>
  </item>
  <item>
    <title>Apartment</title>
    <link>https://feed.example/listing/2</link>
  </item>
</channel></rss>"""


def test_parse_rss_extracts_fields():
    listings = _parse_rss(RSS_SAMPLE, "https://feed.example/rss")
    assert len(listings) == 2
    assert listings[0].listing_id == "rss:listing-1"
    assert listings[0].rooms == 3.0


def test_parse_rss_empty_warns(caplog):
    with caplog.at_level("WARNING"):
        result = _parse_rss(b"<rss></rss>", "https://feed.example/rss")
    assert result == []
    assert any("0 entries" in r.message for r in caplog.records)


# --------------------------------------------------------------------------- #
# HTTP retry/backoff (A2)
# --------------------------------------------------------------------------- #
class FakeResponse:
    def __init__(self, status_code=200, text="", content=b""):
        self.status_code = status_code
        self.text = text
        self.content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")


class FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def get(self, url, headers=None, timeout=None, params=None):
        self.calls += 1
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_http_get_success_no_retry():
    sess = FakeSession([FakeResponse(200, text="ok")])
    slept = []
    resp = http_get("http://x", user_agent="a", timeout=1, session=sess, sleep=slept.append)
    assert resp.text == "ok"
    assert sess.calls == 1
    assert slept == []


def test_http_get_403_blocked_no_retry():
    sess = FakeSession([FakeResponse(403)])
    slept = []
    with pytest.raises(FetchError) as exc:
        http_get("http://x", user_agent="a", timeout=1, session=sess, sleep=slept.append)
    assert exc.value.blocked is True
    assert sess.calls == 1
    assert slept == []


def test_http_get_retries_5xx_then_succeeds():
    sess = FakeSession([FakeResponse(503), FakeResponse(500), FakeResponse(200, text="finally")])
    slept = []
    resp = http_get("http://x", user_agent="a", timeout=1, max_retries=3, session=sess, sleep=slept.append)
    assert resp.text == "finally"
    assert sess.calls == 3
    assert slept == [2, 4]  # backoff before attempts 2 and 3


def test_http_get_retries_timeout_then_raises():
    sess = FakeSession([requests.Timeout(), requests.Timeout(), requests.Timeout(), requests.Timeout()])
    slept = []
    with pytest.raises(FetchError):
        http_get("http://x", user_agent="a", timeout=1, max_retries=3, session=sess, sleep=slept.append)
    assert sess.calls == 4  # initial + 3 retries
    assert slept == [2, 4, 8]


def test_fetch_kleinanzeigen_uses_http_get(ka_html):
    sess = FakeSession([FakeResponse(200, text=ka_html)])
    listings = sources.fetch_kleinanzeigen(
        "https://www.kleinanzeigen.de/s", user_agent="a", timeout=1, session=sess, sleep=lambda s: None
    )
    assert len(listings) == 4
