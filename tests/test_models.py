"""Tests for number parsing, stable_id determinism, and Listing (A1)."""

import pytest

from app.models import Listing, content_signature, parse_number, stable_id


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("1.250,00 €", 1250.0),
        ("65 m²", 65.0),
        ("2,5 Zimmer", 2.5),
        ("890,00 €", 890.0),
        ("1 Zimmer", 1.0),
        ("12.345,67", 12345.67),
        ("VB", None),
        ("", None),
        (None, None),
        ("Verhandlungsbasis", None),
        ("auf Anfrage", None),
    ],
)
def test_parse_number(raw, expected):
    assert parse_number(raw) == expected


def test_stable_id_prefers_native_id():
    assert stable_id("kleinanzeigen", "2901234567", "http://x") == "kleinanzeigen:2901234567"


def test_stable_id_falls_back_to_url_hash():
    sid = stable_id("rss", None, "https://example.com/listing/42")
    assert sid.startswith("rss:")
    assert len(sid.split(":", 1)[1]) == 16


def test_stable_id_is_deterministic():
    a = stable_id("rss", None, "https://example.com/x")
    b = stable_id("rss", None, "https://example.com/x")
    assert a == b


def test_stable_id_differs_by_url():
    a = stable_id("rss", None, "https://example.com/a")
    b = stable_id("rss", None, "https://example.com/b")
    assert a != b


# --------------------------------------------------------------------------- #
# content_signature — catch the same flat reposted under a different ad-id
# --------------------------------------------------------------------------- #
def _sig_listing(**kw):
    base = dict(source="kleinanzeigen", title="Wohnung", url="https://x/1", native_id="1",
                price=900.0, sqm=65.0, rooms=2.0, location="10115 Berlin")
    base.update(kw)
    return Listing.create(**base)


def test_content_signature_eligible_when_price_and_sqm():
    sig = content_signature(_sig_listing())
    assert sig.startswith("sig:")
    assert len(sig.split(":", 1)[1]) == 16


def test_content_signature_none_when_price_missing():
    assert content_signature(_sig_listing(price=None)) is None


def test_content_signature_none_when_sqm_missing():
    assert content_signature(_sig_listing(sqm=None)) is None


def test_content_signature_is_deterministic():
    assert content_signature(_sig_listing(native_id="1")) == content_signature(_sig_listing(native_id="2"))


def test_content_signature_rounds_price_and_sqm():
    # A repost rendering 64,9 m² / 900,40 € collapses to the same flat.
    assert content_signature(_sig_listing(sqm=64.9, price=900.4)) == content_signature(_sig_listing())


def test_content_signature_differs_by_location():
    assert content_signature(_sig_listing(location="Hamburg")) != content_signature(_sig_listing())


def test_content_signature_normalizes_postal_code():
    assert content_signature(_sig_listing(location="10115 Berlin")) == \
        content_signature(_sig_listing(location="Berlin"))


def test_content_signature_ignores_source():
    # The same eligible flat on a different source shares a signature (cross-source).
    ka = _sig_listing(source="kleinanzeigen", url="https://ka/1")
    rss = _sig_listing(source="rss", url="https://feed/1")
    assert content_signature(ka) == content_signature(rss)


def test_listing_create_sets_id_and_haystack():
    lst = Listing.create(
        source="kleinanzeigen",
        title="  Schöne Wohnung  ",
        url="https://x/y",
        native_id="42",
        location="Berlin",
        description="mit Balkon",
    )
    assert lst.listing_id == "kleinanzeigen:42"
    assert lst.title == "Schöne Wohnung"
    assert "balkon" in lst.haystack
    assert "berlin" in lst.haystack
