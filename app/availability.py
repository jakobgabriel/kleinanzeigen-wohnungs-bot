"""Daily availability recheck for listings in the NocoDB results table.

Once a day, re-fetch each still-available Kleinanzeigen listing in
``flatwatch_listings`` and, if its ad page is gone (404/410 or a "no longer
available" page), flip its ``available`` flag to false and stamp ``removed_at``.
Still-reachable listings just get a fresh ``last_checked``.

Best-effort and observational: any failure is caught and logged, never raised,
and a transient/blocked fetch never marks a listing as removed.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import List, Optional

import requests

from .config import Config
from . import sources

log = logging.getLogger("flatwatch.availability")

_NOCODB_PAGE = 200

# Lower-cased markers Kleinanzeigen shows when an ad is gone.
_REMOVED_MARKERS = (
    "ist nicht mehr verfügbar",
    "wurde gelöscht",
    "nicht mehr verfügbar",
    "anzeige wurde nicht gefunden",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class AvailabilityChecker:
    """Rechecks results-table listings and tags removed ones as unavailable."""

    def __init__(self, cfg: Config, session: Optional[requests.Session] = None):
        self.cfg = cfg
        self._session = session or requests.Session()

    @property
    def enabled(self) -> bool:
        return self.cfg.results_enabled and self.cfg.recheck_enabled

    # ----- NocoDB plumbing -------------------------------------------------- #
    def _headers(self) -> dict:
        return {"xc-token": self.cfg.nocodb_token, "Content-Type": "application/json"}

    def _records_url(self) -> str:
        base = self.cfg.nocodb_url.rstrip("/")
        return f"{base}/api/v2/tables/{self.cfg.nocodb_listings_table_id}/records"

    def _fetch_available_rows(self) -> List[dict]:
        """Page through the results table, returning rows not already unavailable."""
        rows: List[dict] = []
        offset = 0
        url = self._records_url()
        while True:
            resp = self._session.get(
                url,
                headers=self._headers(),
                params={"limit": _NOCODB_PAGE, "offset": offset,
                        "fields": "Id,listing_id,url,source,available"},
                timeout=self.cfg.http_timeout_s,
            )
            resp.raise_for_status()
            page = resp.json().get("list", [])
            rows.extend(page)
            if len(page) < _NOCODB_PAGE:
                break
            offset += _NOCODB_PAGE
        return [r for r in rows if r.get("available") not in (False, 0, "false")]

    def _patch(self, updates: List[dict]) -> None:
        url = self._records_url()
        for start in range(0, len(updates), _NOCODB_PAGE):
            batch = updates[start: start + _NOCODB_PAGE]
            resp = self._session.patch(
                url, headers=self._headers(), data=json.dumps(batch), timeout=self.cfg.http_timeout_s
            )
            resp.raise_for_status()

    # ----- recheck ---------------------------------------------------------- #
    def _is_removed(self, listing_url: str) -> Optional[bool]:
        """True=removed, False=still there, None=unknown (blocked/transient)."""
        try:
            resp = sources.http_get(
                listing_url,
                user_agent=self.cfg.user_agent,
                timeout=self.cfg.http_timeout_s,
                max_retries=self.cfg.max_retries,
                session=self._session,
            )
        except requests.HTTPError as exc:
            status = getattr(exc.response, "status_code", None)
            return True if status in (404, 410) else None
        except sources.FetchError:
            return None  # blocked or exhausted retries — don't touch availability
        low = resp.text.lower()
        return any(marker in low for marker in _REMOVED_MARKERS)

    def run(self) -> int:
        """Recheck all available listings; return the number marked unavailable.

        Best-effort: never raises. Returns 0 when disabled or on any error.
        """
        if not self.enabled:
            return 0
        try:
            rows = self._fetch_available_rows()
        except (requests.RequestException, ValueError) as exc:
            log.warning("Availability recheck: could not read results table (%s).", exc)
            return 0

        now = _now_iso()
        updates: List[dict] = []
        removed = 0
        for row in rows:
            rid = row.get("Id") or row.get("id")
            url = row.get("url")
            if not rid or not url or (row.get("source") or "") != "kleinanzeigen":
                continue
            verdict = self._is_removed(url)
            if verdict is None:
                continue  # unknown — leave as-is
            if verdict:
                updates.append({"Id": rid, "available": False, "removed_at": now, "last_checked": now})
                removed += 1
            else:
                updates.append({"Id": rid, "last_checked": now})
            sources.polite_pause(self.cfg.per_request_delay_s, self.cfg.request_jitter_s)

        if updates:
            try:
                self._patch(updates)
            except (requests.RequestException, ValueError) as exc:
                log.warning("Availability recheck: could not write updates (%s).", exc)
                return 0
        log.info("Availability recheck: %d checked, %d marked unavailable.", len(updates), removed)
        return removed
