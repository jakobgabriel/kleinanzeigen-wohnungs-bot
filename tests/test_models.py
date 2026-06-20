"""Tests for number parsing, stable_id determinism, and Listing (A1)."""

import pytest

from app.models import Listing, parse_number, stable_id


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
