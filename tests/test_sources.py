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


# --------------------------------------------------------------------------- #
# Pagination (seite:N) with a flexible end
# --------------------------------------------------------------------------- #
_URL = "https://www.kleinanzeigen.de/s-wohnung-mieten/erfurt/c203l3741"


def _ka_page(ids):
    cards = "".join(
        f'<article class="aditem" data-adid="{i}"><div class="aditem-main">'
        f'<div class="aditem-main--middle">'
        f'<a class="ellipsis" href="/s-anzeige/x/{i}">Wohnung {i}</a>'
        f'<p class="aditem-main--middle--price-shipping--price">900 €</p>'
        f"</div></div></article>"
        for i in ids
    )
    return f"<html><body><ul>{cards}</ul></body></html>"


@pytest.mark.parametrize(
    "page, expected",
    [
        (1, "https://www.kleinanzeigen.de/s-wohnung-mieten/erfurt/c203l3741"),
        (3, "https://www.kleinanzeigen.de/s-wohnung-mieten/erfurt/seite:3/c203l3741"),
    ],
)
def test_ka_page_url(page, expected):
    assert sources.ka_page_url(_URL, page) == expected


def test_ka_page_url_replaces_existing_seite():
    deep = "https://www.kleinanzeigen.de/s-wohnung-mieten/erfurt/seite:2/c203l3741"
    assert sources.ka_page_url(deep, 4) == "https://www.kleinanzeigen.de/s-wohnung-mieten/erfurt/seite:4/c203l3741"
    assert sources.ka_page_url(deep, 1) == _URL  # page 1 strips the segment


def test_ka_page_url_category_only():
    assert (
        sources.ka_page_url("https://www.kleinanzeigen.de/s-wohnung-mieten/c203", 2)
        == "https://www.kleinanzeigen.de/s-wohnung-mieten/seite:2/c203"
    )


# --------------------------------------------------------------------------- #
# Umkreissuche (radius) — r<km> suffix on the category/location code
# --------------------------------------------------------------------------- #
def test_ka_radius_url_appends_suffix():
    assert (
        sources.ka_radius_url(_URL, 50)
        == "https://www.kleinanzeigen.de/s-wohnung-mieten/erfurt/c203l3741r50"
    )


def test_ka_radius_url_none_leaves_url_untouched():
    assert sources.ka_radius_url(_URL, None) == _URL
    deep = _URL + "r25"
    assert sources.ka_radius_url(deep, None) == deep  # respect an in-URL radius


def test_ka_radius_url_replaces_existing_radius():
    deep = "https://www.kleinanzeigen.de/s-wohnung-mieten/erfurt/c203l3741r25"
    assert (
        sources.ka_radius_url(deep, 100)
        == "https://www.kleinanzeigen.de/s-wohnung-mieten/erfurt/c203l3741r100"
    )


def test_ka_radius_url_zero_strips_radius():
    deep = "https://www.kleinanzeigen.de/s-wohnung-mieten/erfurt/c203l3741r25"
    assert sources.ka_radius_url(deep, 0) == _URL


def test_ka_radius_url_truncates_float_km():
    assert sources.ka_radius_url(_URL, 50.0).endswith("c203l3741r50")


def test_ka_radius_url_no_code_segment_unchanged():
    # An RSS/other URL with no c<digit> code segment is returned untouched.
    other = "https://example.com/feed.xml"
    assert sources.ka_radius_url(other, 50) == other


def test_fetch_kleinanzeigen_applies_radius():
    class RecordingSession:
        def __init__(self):
            self.urls = []

        def get(self, url, headers=None, timeout=None, params=None):
            self.urls.append(url)
            return FakeResponse(200, text=_ka_page([1]))

    sess = RecordingSession()
    sources.fetch_kleinanzeigen(
        _URL, user_agent="a", timeout=1, session=sess, sleep=lambda s: None,
        max_pages=1, radius_km=50,
    )
    assert sess.urls == ["https://www.kleinanzeigen.de/s-wohnung-mieten/erfurt/c203l3741r50"]


def test_fetch_kleinanzeigen_radius_survives_pagination():
    class RecordingSession:
        def __init__(self):
            self.urls = []

        def get(self, url, headers=None, timeout=None, params=None):
            self.urls.append(url)
            # page 2 onward is empty so pagination stops after recording the URLs
            return FakeResponse(200, text=_ka_page([1] if len(self.urls) == 1 else []))

    sess = RecordingSession()
    sources.fetch_kleinanzeigen(
        _URL, user_agent="a", timeout=1, session=sess, sleep=lambda s: None,
        max_pages=3, radius_km=50,
    )
    # seite:N is inserted before the radius-bearing code segment on later pages.
    assert sess.urls[0].endswith("/erfurt/c203l3741r50")
    assert sess.urls[1] == "https://www.kleinanzeigen.de/s-wohnung-mieten/erfurt/seite:2/c203l3741r50"


def test_fetch_paginates_until_empty_page():
    sess = FakeSession([
        FakeResponse(200, text=_ka_page([1, 2])),
        FakeResponse(200, text=_ka_page([3, 4])),
        FakeResponse(200, text=_ka_page([])),  # past the last page
    ])
    out = sources.fetch_kleinanzeigen(_URL, user_agent="a", timeout=1, session=sess, sleep=lambda s: None, max_pages=5)
    assert [l.listing_id for l in out] == [f"kleinanzeigen:{i}" for i in (1, 2, 3, 4)]
    assert sess.calls == 3


def test_fetch_stops_when_page_repeats():
    # KA serves the last page again when asked beyond the end → no new ids → stop.
    sess = FakeSession([FakeResponse(200, text=_ka_page([1, 2])), FakeResponse(200, text=_ka_page([1, 2]))])
    out = sources.fetch_kleinanzeigen(_URL, user_agent="a", timeout=1, session=sess, sleep=lambda s: None, max_pages=5)
    assert [l.listing_id for l in out] == ["kleinanzeigen:1", "kleinanzeigen:2"]
    assert sess.calls == 2


def test_fetch_respects_max_pages_cap():
    pages = [FakeResponse(200, text=_ka_page([i])) for i in range(1, 11)]
    sess = FakeSession(pages)
    out = sources.fetch_kleinanzeigen(_URL, user_agent="a", timeout=1, session=sess, sleep=lambda s: None, max_pages=3)
    assert sess.calls == 3 and len(out) == 3


def test_fetch_pagination_error_after_page1_is_graceful():
    sess = FakeSession([FakeResponse(200, text=_ka_page([1, 2])), FakeResponse(404)])
    out = sources.fetch_kleinanzeigen(_URL, user_agent="a", timeout=1, session=sess, sleep=lambda s: None, max_pages=5)
    assert [l.listing_id for l in out] == ["kleinanzeigen:1", "kleinanzeigen:2"]
    assert sess.calls == 2


def test_fetch_page1_error_propagates():
    sess = FakeSession([FakeResponse(403)])
    with pytest.raises(FetchError):
        sources.fetch_kleinanzeigen(_URL, user_agent="a", timeout=1, session=sess, sleep=lambda s: None, max_pages=5)


def test_fetch_pauses_between_pages():
    sess = FakeSession([
        FakeResponse(200, text=_ka_page([1])),
        FakeResponse(200, text=_ka_page([2])),
        FakeResponse(200, text=_ka_page([])),
    ])
    slept = []
    sources.fetch_kleinanzeigen(
        _URL, user_agent="a", timeout=1, session=sess, sleep=slept.append,
        max_pages=5, per_request_delay_s=2.0, request_jitter_s=0.0,
    )
    assert slept == [2.0, 2.0]  # one polite pause after each non-final page
