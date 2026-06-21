"""Results sink: write a full row per matching listing to a NocoDB table.

When ``NOCODB_LISTINGS_TABLE_ID`` is set, every newly-matched listing is written
to a dedicated ``flatwatch_listings`` table with its full attributes — so NocoDB
becomes the browsable record of everything flatwatch found, independent of
whether notifications are configured.

Writing is **best-effort and observational** (like run-logging): a failure is
caught, logged at WARNING, and never propagates to the poll loop. The caller
writes only the listings it is also persisting as seen, so there is exactly one
row per listing (no duplicates across notification retries).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import List, Optional

import requests

from .config import Config
from .models import Listing

log = logging.getLogger("flatwatch.results")

# NocoDB caps bulk inserts around 1000 rows per request.
_NOCODB_BULK = 1000

# Lifecycle status stamped on a row when it is first inserted. The user owns the
# column thereafter (flatwatch only inserts new rows and the availability recheck
# patches availability fields), so a hand-edited status is never overwritten.
_INITIAL_STATUS = "new"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ResultsSink:
    """Best-effort writer of full listing rows to the NocoDB results table."""

    def __init__(self, cfg: Config, session: Optional[requests.Session] = None):
        self.cfg = cfg
        self._session = session or requests.Session()

    @property
    def enabled(self) -> bool:
        return self.cfg.results_enabled

    def _headers(self) -> dict:
        return {"xc-token": self.cfg.nocodb_token, "Content-Type": "application/json"}

    def _records_url(self) -> str:
        base = self.cfg.nocodb_url.rstrip("/")
        return f"{base}/api/v2/tables/{self.cfg.nocodb_listings_table_id}/records"

    @staticmethod
    def _payload(listing: Listing, first_seen: str) -> dict:
        d = listing.details or {}
        return {
            "listing_id": listing.listing_id,
            "title": listing.title,
            "url": listing.url,
            "source": listing.source,
            # What kind of search found it (rent/buy · flat/house/land), and the
            # lifecycle status the user then tracks by hand in NocoDB.
            "search_type": listing.search_type or None,
            "status": _INITIAL_STATUS,
            "price": listing.price,
            "rooms": listing.rooms,
            "sqm": listing.sqm,
            "location": listing.location,
            "description": listing.description,
            # Rich detail attributes (populated when ENRICH_DETAIL=true).
            "bedrooms": d.get("bedrooms"),
            "bathrooms": d.get("bathrooms"),
            "floor": d.get("floor"),
            "apartment_type": d.get("apartment_type"),
            "available_from": d.get("available_from"),
            "additional_costs": d.get("additional_costs"),
            "warm_rent": d.get("warm_rent"),
            "deposit": d.get("deposit"),
            "features": ", ".join(listing.features) if listing.features else None,
            # Availability tracking (the daily recheck flips these).
            "available": True,
            "last_checked": first_seen,
            "first_seen": first_seen,
        }

    def write(self, listings: List[Listing]) -> None:
        """Bulk-insert full rows for ``listings``. Best-effort; never raises."""
        if not listings:
            return
        if not self.enabled:
            log.info(
                "Results table not configured (NOCODB_LISTINGS_TABLE_ID unset) — "
                "%d listing(s) not stored in NocoDB.", len(listings),
            )
            return
        now = _now_iso()
        payloads = [self._payload(l, now) for l in listings]
        url = self._records_url()
        try:
            for start in range(0, len(payloads), _NOCODB_BULK):
                batch = payloads[start : start + _NOCODB_BULK]
                resp = self._session.post(
                    url, headers=self._headers(), data=json.dumps(batch), timeout=self.cfg.http_timeout_s
                )
                if resp.status_code >= 400:
                    body = (getattr(resp, "text", "") or "")[:600]
                    log.warning(
                        "Results write FAILED: NocoDB returned HTTP %s — %s. "
                        "(A missing column is the usual cause — ensure flatwatch_listings "
                        "has all columns incl. bedrooms/features/available/last_checked/removed_at.)",
                        resp.status_code, body,
                    )
                    return
            log.info("Wrote %d listing(s) to the NocoDB results table.", len(payloads))
        except (requests.RequestException, ValueError) as exc:
            log.warning("Could not write results to NocoDB (non-fatal): %s", exc)
        except Exception as exc:  # observational: swallow anything
            log.warning("Unexpected results-write error (ignored): %s", exc)
