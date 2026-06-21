"""Listing sources: the Kleinanzeigen HTML scraper and the RSS parser.

Both produce ``list[Listing]``.  Fetching goes through :func:`http_get` which
retries transient failures with exponential backoff (A2) and treats a 403 as a
block (no retry).  The Kleinanzeigen parser keeps its CSS selectors as named
module-level constants so markup drift is a one-line patch (B1), and degrades
field-by-field rather than crashing when a card omits price/rooms/sqm.
"""

from __future__ import annotations

import logging
import random
import re
import time
from typing import Callable, List, Optional, Tuple
from urllib.parse import urljoin, urlparse, urlunparse

import feedparser
import requests
from bs4 import BeautifulSoup

from .models import Listing, parse_number

log = logging.getLogger("flatwatch.sources")

# --------------------------------------------------------------------------- #
# Kleinanzeigen selectors — patch here if the markup drifts (B1).
# --------------------------------------------------------------------------- #
KA_BASE_URL = "https://www.kleinanzeigen.de"
KA_CARD_SELECTOR = "article.aditem"
KA_CARD_ID_ATTR = "data-adid"
KA_TITLE_SELECTOR = "a.ellipsis"
KA_PRICE_SELECTOR = "p.aditem-main--middle--price-shipping--price"
KA_LOCATION_SELECTOR = "div.aditem-main--top--left"
KA_DESCRIPTION_SELECTOR = "p.aditem-main--middle--description"
KA_TAGS_SELECTOR = "span.simpletag"
KA_IMAGE_SELECTOR = "div.aditem-image img"

# Detail-page selectors — used only when ENRICH_DETAIL is enabled (B2).
KA_DETAIL_PRICE_SELECTOR = "#viewad-price"
KA_DETAIL_LIST_ITEM_SELECTOR = "li.addetailslist--detail"
KA_DETAIL_VALUE_SELECTOR = "span.addetailslist--detail--value"
KA_DETAIL_DESCRIPTION_SELECTOR = "#viewad-description-text"

# A 403 from Kleinanzeigen is a block, not a transient error — never retried.
BLOCK_STATUS = 403


class FetchError(Exception):
    """Raised when a source fetch fails after exhausting retries or is blocked."""

    def __init__(self, message: str, blocked: bool = False):
        super().__init__(message)
        self.blocked = blocked


def http_get(
    url: str,
    *,
    user_agent: str,
    timeout: float,
    max_retries: int = 3,
    session: Optional[requests.Session] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> requests.Response:
    """GET ``url`` with retry + exponential backoff on transient failures (A2).

    - 5xx and timeouts/connection errors: retry up to ``max_retries`` times with
      2s, 4s, 8s backoff.
    - 403: raise immediately with ``blocked=True`` (do not retry a block).
    - retries exhausted: raise :class:`FetchError`.
    """
    sess = session or requests
    headers = {"User-Agent": user_agent, "Accept-Language": "de-DE,de;q=0.9"}
    last_exc: Optional[Exception] = None

    for attempt in range(max_retries + 1):
        try:
            resp = sess.get(url, headers=headers, timeout=timeout)
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_exc = exc
            log.warning("Transient error fetching %s (attempt %d): %s", url, attempt + 1, exc)
        else:
            if resp.status_code == BLOCK_STATUS:
                raise FetchError(f"403 blocked fetching {url}", blocked=True)
            if 500 <= resp.status_code < 600:
                last_exc = FetchError(f"{resp.status_code} from {url}")
                log.warning("Server error %d fetching %s (attempt %d)", resp.status_code, url, attempt + 1)
            else:
                resp.raise_for_status()
                return resp

        if attempt < max_retries:
            sleep(2 ** (attempt + 1))  # 2s, 4s, 8s

    raise FetchError(f"Exhausted retries fetching {url}: {last_exc}")


# --------------------------------------------------------------------------- #
# Kleinanzeigen
# --------------------------------------------------------------------------- #
def _text(node) -> Optional[str]:
    if node is None:
        return None
    text = node.get_text(" ", strip=True)
    return text or None


def _parse_kleinanzeigen(html: str, base_url: str = KA_BASE_URL, warn_empty: bool = True) -> List[Listing]:
    """Parse a Kleinanzeigen search-results page into listings.

    Resilient by design (B1): a card missing price/rooms/sqm yields ``None`` for
    those fields and still parses; if zero cards are found a WARNING is logged in
    case the selectors have gone stale.
    """
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select(KA_CARD_SELECTOR)
    listings: List[Listing] = []

    for card in cards:
        link = card.select_one(KA_TITLE_SELECTOR)
        title = _text(link)
        href = link.get("href") if link else None
        if not title or not href:
            continue  # a card with no title/link is unusable
        url = urljoin(base_url, href)
        native_id = card.get(KA_CARD_ID_ATTR)

        price = parse_number(_text(card.select_one(KA_PRICE_SELECTOR)))
        location = _text(card.select_one(KA_LOCATION_SELECTOR))
        description = _text(card.select_one(KA_DESCRIPTION_SELECTOR))

        rooms, sqm = _extract_tags(card)

        thumb = None
        img = card.select_one(KA_IMAGE_SELECTOR)
        if img is not None:
            thumb = img.get("src") or img.get("data-imgsrc") or img.get("srcset")

        missing = tuple(
            name for name, val in (("price", price), ("rooms", rooms), ("sqm", sqm)) if val is None
        )
        listings.append(
            Listing.create(
                source="kleinanzeigen",
                title=title,
                url=url,
                native_id=native_id,
                price=price,
                rooms=rooms,
                sqm=sqm,
                location=location,
                description=description,
                thumbnail=thumb,
                _missing=missing,
            )
        )

    if not listings and warn_empty:
        log.warning("0 cards parsed from Kleinanzeigen response — selectors may be stale.")
    return listings


def _extract_tags(card) -> Tuple[Optional[float], Optional[float]]:
    """Pull rooms ("2 Zimmer") and area ("65 m²") out of a card's tag spans."""
    rooms: Optional[float] = None
    sqm: Optional[float] = None
    for tag in card.select(KA_TAGS_SELECTOR):
        text = _text(tag) or ""
        low = text.lower()
        if "zimmer" in low and rooms is None:
            rooms = parse_number(text)
        elif "m²" in low or "m2" in low:
            if sqm is None:
                sqm = parse_number(text)
    return rooms, sqm


# Kleinanzeigen encodes the page as a "seite:N" path segment, inserted just
# before the category code (e.g. .../erfurt/seite:3/c203l3741). Page 1 has none.
_SEITE_RE = re.compile(r"^seite:\d+$")


def ka_page_url(url: str, page: int) -> str:
    """Return the KA search URL for a given 1-based page.

    Strips any existing ``seite:N`` segment first (so a deep-linked URL still
    paginates from the top), then inserts ``seite:N`` before the category segment
    for pages > 1.
    """
    parsed = urlparse(url)
    segs = [s for s in parsed.path.split("/") if s and not _SEITE_RE.match(s)]
    if page > 1:
        if segs and re.search(r"c\d", segs[-1]):
            segs.insert(len(segs) - 1, f"seite:{page}")
        else:
            segs.append(f"seite:{page}")
    new_path = "/" + "/".join(segs)
    return urlunparse((parsed.scheme, parsed.netloc, new_path, parsed.params, parsed.query, parsed.fragment))


def fetch_kleinanzeigen(
    url: str,
    *,
    user_agent: str,
    timeout: float,
    max_retries: int = 3,
    session: Optional[requests.Session] = None,
    sleep: Callable[[float], None] = time.sleep,
    max_pages: int = 1,
    per_request_delay_s: float = 0.0,
    request_jitter_s: float = 0.0,
) -> List[Listing]:
    """Fetch and parse a Kleinanzeigen search, walking pages until they run out.

    Pagination has a *flexible end* (the page count is unknown): it stops at the
    first page that yields no cards, or yields only listings already seen on an
    earlier page (Kleinanzeigen serves the last/first page when you ask past the
    end), or when ``max_pages`` is reached. Pages are spaced by the politeness
    delay. A failure on page > 1 ends pagination gracefully with what we have; a
    failure on page 1 propagates so the source is marked failed.
    """
    base = _origin(url)
    listings: List[Listing] = []
    seen_ids: set = set()

    for page in range(1, max(1, max_pages) + 1):
        page_url = ka_page_url(url, page)
        try:
            resp = http_get(
                page_url,
                user_agent=user_agent,
                timeout=timeout,
                max_retries=max_retries,
                session=session,
                sleep=sleep,
            )
        except (FetchError, requests.HTTPError) as exc:
            if page == 1:
                raise
            log.info("Stopping pagination for %s at page %d: %s", url, page, exc)
            break

        page_listings = _parse_kleinanzeigen(resp.text, base_url=base, warn_empty=(page == 1))
        fresh = [l for l in page_listings if l.listing_id not in seen_ids]
        if not fresh:
            break  # empty page or a repeat of earlier results → past the last page

        seen_ids.update(l.listing_id for l in fresh)
        listings.extend(fresh)

        if page < max_pages:
            polite_pause(per_request_delay_s, request_jitter_s, sleep)

    if max_pages > 1:
        log.info("Fetched %d listing(s) across up to %d page(s) from %s.", len(listings), max_pages, url)
    return listings


def _origin(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return KA_BASE_URL


# --------------------------------------------------------------------------- #
# Detail-page enrichment (B2)
# --------------------------------------------------------------------------- #
def _parse_kleinanzeigen_detail(html: str) -> dict:
    """Parse a KA detail page for price/rooms/sqm/description.

    Returns a dict with whichever fields were found; missing ones are absent so
    the caller fills only the gaps. Resilient: never raises on stale markup.
    """
    soup = BeautifulSoup(html, "html.parser")
    out: dict = {}

    price = parse_number(_text(soup.select_one(KA_DETAIL_PRICE_SELECTOR)))
    if price is not None:
        out["price"] = price

    for item in soup.select(KA_DETAIL_LIST_ITEM_SELECTOR):
        value_node = item.select_one(KA_DETAIL_VALUE_SELECTOR)
        value_text = _text(value_node)
        if not value_text:
            continue
        # The label is the li text minus the value span text.
        label = (_text(item) or "").replace(value_text, "").strip().lower()
        if ("wohnfläche" in label or "fläche" in label or "m²" in value_text.lower()) and "sqm" not in out:
            num = parse_number(value_text)
            if num is not None:
                out["sqm"] = num
        elif "zimmer" in label and "rooms" not in out:
            num = parse_number(value_text)
            if num is not None:
                out["rooms"] = num

    desc = _text(soup.select_one(KA_DETAIL_DESCRIPTION_SELECTOR))
    if desc:
        out["description"] = desc

    return out


def fetch_kleinanzeigen_detail(
    url: str,
    *,
    user_agent: str,
    timeout: float,
    max_retries: int = 3,
    session: Optional[requests.Session] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Fetch a single KA detail page and return parsed fields (B2)."""
    resp = http_get(
        url,
        user_agent=user_agent,
        timeout=timeout,
        max_retries=max_retries,
        session=session,
        sleep=sleep,
    )
    return _parse_kleinanzeigen_detail(resp.text)


def enrich_listing(listing: Listing, fields: dict) -> Listing:
    """Fill only the listing's missing (None) price/rooms/sqm from ``fields``.

    Mutates and returns the listing. Known fields are never overwritten —
    enrichment only fills gaps. The ``_missing`` tuple is recomputed.
    """
    for attr in ("price", "rooms", "sqm"):
        if getattr(listing, attr) is None and fields.get(attr) is not None:
            setattr(listing, attr, fields[attr])
    if listing.description is None and fields.get("description"):
        listing.description = fields["description"]
    listing._missing = tuple(
        name for name in ("price", "rooms", "sqm") if getattr(listing, name) is None
    )
    return listing


# --------------------------------------------------------------------------- #
# RSS
# --------------------------------------------------------------------------- #
def _parse_rss(content: bytes, feed_url: str) -> List[Listing]:
    """Parse an RSS/Atom feed body into listings."""
    parsed = feedparser.parse(content)
    listings: List[Listing] = []
    for entry in parsed.entries:
        title = (getattr(entry, "title", "") or "").strip()
        link = getattr(entry, "link", "") or ""
        if not title or not link:
            continue
        native_id = getattr(entry, "id", None) or getattr(entry, "guid", None)
        description = getattr(entry, "summary", None)
        blob = f"{title} {description or ''}"
        listings.append(
            Listing.create(
                source="rss",
                title=title,
                url=link,
                native_id=native_id,
                price=parse_number(_grep(blob, ("€", "eur", "miete"))),
                rooms=parse_number(_grep(blob, ("zimmer",))),
                sqm=parse_number(_grep(blob, ("m²", "m2"))),
                description=description,
            )
        )
    if not listings:
        log.warning("0 entries parsed from RSS feed %s.", feed_url)
    return listings


def _grep(text: str, keywords) -> Optional[str]:
    """Return the whitespace-delimited token preceding/containing a keyword."""
    low = text.lower()
    for kw in keywords:
        idx = low.find(kw)
        if idx == -1:
            continue
        start = max(0, idx - 12)
        return text[start : idx + len(kw)]
    return None


def fetch_rss(
    url: str,
    *,
    user_agent: str,
    timeout: float,
    max_retries: int = 3,
    session: Optional[requests.Session] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> List[Listing]:
    """Fetch and parse a single RSS feed URL."""
    resp = http_get(
        url,
        user_agent=user_agent,
        timeout=timeout,
        max_retries=max_retries,
        session=session,
        sleep=sleep,
    )
    return _parse_rss(resp.content, url)


def polite_pause(per_request_delay_s: float, jitter_s: float, sleep: Callable[[float], None] = time.sleep) -> None:
    """Sleep a base delay plus random jitter to avoid hammering portals."""
    sleep(per_request_delay_s + random.uniform(0, max(0.0, jitter_s)))
