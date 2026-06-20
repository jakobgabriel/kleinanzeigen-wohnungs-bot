"""Tests for criteria matching (A1)."""

from app.config import Criteria
from app.models import Listing


from app.filters import matches


def _listing(**kw):
    base = dict(source="rss", title="Wohnung", url="https://x")
    base.update(kw)
    return Listing.create(**base)


def test_pass_within_all_bounds():
    crit = Criteria(min_rent=500, max_rent=1500, min_rooms=2, max_rooms=4, min_sqm=40, max_sqm=120)
    lst = _listing(price=1200, rooms=3, sqm=80)
    assert matches(lst, crit) is True


def test_over_rent_rejected():
    crit = Criteria(max_rent=1000)
    assert matches(_listing(price=1200, rooms=2, sqm=60), crit) is False


def test_excluded_keyword_rejected():
    crit = Criteria(excluded_keywords=["tausch"])
    assert matches(_listing(title="2-Zimmer Tauschangebot", price=900), crit) is False


def test_required_keyword_missing_rejected():
    crit = Criteria(required_keywords=["balkon"])
    assert matches(_listing(title="Wohnung ohne alles", description="schlicht"), crit) is False


def test_required_keyword_present_passes():
    crit = Criteria(required_keywords=["balkon"])
    assert matches(_listing(title="Wohnung mit Balkon"), crit) is True


def test_all_none_attrs_passes():
    """An unparseable/missing attribute must never disqualify (false-positive bias)."""
    crit = Criteria(min_rent=500, max_rent=1500, min_rooms=2, max_rooms=4, min_sqm=40, max_sqm=120)
    lst = _listing(price=None, rooms=None, sqm=None)
    assert matches(lst, crit) is True


def test_under_min_rooms_rejected():
    crit = Criteria(min_rooms=3)
    assert matches(_listing(rooms=1), crit) is False


def test_no_criteria_passes_everything():
    assert matches(_listing(price=99999, rooms=99), Criteria()) is True
