"""Criteria matching.

Filtering philosophy (do not relitigate): an unparseable attribute (``None``)
must never disqualify a listing — bias toward false positives over missed flats.
A bound that is ``None`` in the criteria is simply not applied.
"""

from __future__ import annotations

import logging
from typing import Optional

from .config import Criteria
from .models import Listing

log = logging.getLogger("flatwatch.filters")


def _within(value: Optional[float], lo: Optional[float], hi: Optional[float]) -> bool:
    """True unless ``value`` is known and falls outside an applied bound."""
    if value is None:
        return True  # unknown attribute never disqualifies
    if lo is not None and value < lo:
        return False
    if hi is not None and value > hi:
        return False
    return True


def matches(listing: Listing, criteria: Criteria) -> bool:
    """Return True if the listing satisfies the criteria.

    Numeric bounds only reject a listing whose attribute is *known* and out of
    range.  Required keywords must all be present in the listing text; excluded
    keywords reject the listing if any is present.
    """
    if not _within(listing.price, criteria.min_rent, criteria.max_rent):
        return False
    if not _within(listing.rooms, criteria.min_rooms, criteria.max_rooms):
        return False
    if not _within(listing.sqm, criteria.min_sqm, criteria.max_sqm):
        return False

    haystack = listing.haystack
    for kw in criteria.excluded_keywords:
        if kw in haystack:
            return False
    for kw in criteria.required_keywords:
        if kw not in haystack:
            return False

    return True
