"""Domain models and parsing helpers for flatwatch.

A :class:`Listing` is the normalized representation of a rental advert pulled
from any source.  ``parse_number`` turns the messy German-formatted strings the
portals hand us ("1.250,00 €", "65 m²", "2,5 Zimmer") into floats, and
``stable_id`` derives the deterministic dedup key.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Optional

# A number, German-formatted: thousands separated by ".", decimals by ",".
# Examples matched: "1.250,00", "1250", "65", "2,5".
_NUMBER_RE = re.compile(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d+(?:,\d+)?")


def parse_number(text: Optional[str]) -> Optional[float]:
    """Extract the first numeric value from a German-formatted string.

    Returns ``None`` when nothing numeric is present so that downstream
    filtering can treat the attribute as "unknown" rather than zero — an
    unparseable attribute must never disqualify a listing.

    >>> parse_number("1.250,00 €")
    1250.0
    >>> parse_number("65 m²")
    65.0
    >>> parse_number("2,5 Zimmer")
    2.5
    >>> parse_number("") is None
    True
    >>> parse_number("VB / Verhandlungsbasis") is None
    True
    """
    if not text:
        return None
    match = _NUMBER_RE.search(text)
    if not match:
        return None
    raw = match.group(0)
    # German notation -> machine float: drop thousands ".", swap decimal "," -> ".".
    normalized = raw.replace(".", "").replace(",", ".")
    try:
        return float(normalized)
    except ValueError:
        return None


def stable_id(source: str, native_id: Optional[str], url: str) -> str:
    """Compute the deterministic dedup key for a listing.

    Prefer the source's own native id (``source:native_id``); when absent, fall
    back to a hash of the URL (``source:sha1(url)[:16]``).  The result is stable
    across restarts so dedup survives container recreation.
    """
    if native_id:
        return f"{source}:{native_id}"
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    return f"{source}:{digest}"


@dataclass
class Listing:
    """A normalized rental listing.

    Numeric attributes are ``Optional`` on purpose: a portal that omits the
    price or room count yields ``None``, and the filter biases toward keeping
    such listings rather than dropping them.
    """

    listing_id: str
    source: str
    title: str
    url: str
    price: Optional[float] = None
    rooms: Optional[float] = None
    sqm: Optional[float] = None
    location: Optional[str] = None
    description: Optional[str] = None
    thumbnail: Optional[str] = None
    # Attributes still missing after list-card parsing; used by detail enrichment.
    _missing: tuple = field(default=(), repr=False)

    @classmethod
    def create(
        cls,
        source: str,
        title: str,
        url: str,
        native_id: Optional[str] = None,
        **kwargs,
    ) -> "Listing":
        """Build a Listing, deriving ``listing_id`` from source/native_id/url."""
        return cls(
            listing_id=stable_id(source, native_id, url),
            source=source,
            title=title.strip(),
            url=url,
            **kwargs,
        )

    @property
    def haystack(self) -> str:
        """Lower-cased concatenation of text fields for keyword matching."""
        parts = [self.title, self.location or "", self.description or ""]
        return " ".join(parts).lower()
