"""Dynamic search configuration from a NocoDB table.

Static env vars (``KA_SEARCH_URLS`` / ``RSS_URLS`` + the global ``Criteria``) are
fine for a fixed setup, but they require a redeploy to change. When
``NOCODB_SEARCHES_TABLE_ID`` is set, :class:`SearchProvider` instead reads a
``flatwatch_searches`` table at the start of every poll cycle, so edits made in
NocoDB's grid take effect on the next cycle — no restart.

Each row is one :class:`Search`: a URL + source type + its own criteria, where any
blank criteria cell inherits the global env default. Reading is best-effort and
never load-bearing: if NocoDB is unreachable the last-known-good list is reused,
and if there is none (or the table is empty) the env-var searches are used.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

import requests

from .config import Config, Criteria
from .models import parse_number

log = logging.getLogger("flatwatch.searches")

VALID_SOURCE_TYPES = ("kleinanzeigen", "rss")

# Column names in the flatwatch_searches table (patch here if you rename them).
COL_ENABLED = "enabled"
COL_LABEL = "label"
COL_SOURCE_TYPE = "source_type"
COL_URL = "url"
COL_MIN_RENT = "min_rent"
COL_MAX_RENT = "max_rent"
COL_MIN_ROOMS = "min_rooms"
COL_MAX_ROOMS = "max_rooms"
COL_MIN_SQM = "min_sqm"
COL_MAX_SQM = "max_sqm"
COL_REQUIRED_KEYWORDS = "required_keywords"
COL_EXCLUDED_KEYWORDS = "excluded_keywords"
COL_RADIUS_KM = "radius_km"

_NOCODB_PAGE = 200


@dataclass(frozen=True)
class Search:
    """A single search: where to look, and the criteria to apply to it.

    ``radius_km`` is the Umkreissuche radius for Kleinanzeigen searches (include
    listings within that many km of the town); ``None`` means no radius applied.
    """

    url: str
    source_type: str
    criteria: Criteria
    enabled: bool = True
    label: str = ""
    radius_km: Optional[float] = None


def _to_float(value) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return parse_number(str(value))


def _to_bool(value, default: bool = True) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "on", "checked"}


def _to_keywords(value) -> Optional[List[str]]:
    if value is None or value == "":
        return None
    items = [k.strip().lower() for k in str(value).split(",") if k.strip()]
    return items or None


class SearchProvider:
    """Yields the active list of searches, from NocoDB when configured, else env."""

    def __init__(self, cfg: Config, session: Optional[requests.Session] = None):
        self.cfg = cfg
        self._session = session or requests.Session()
        self._cache: Optional[List[Search]] = None  # last-known-good from NocoDB

    # ----- env fallback ----------------------------------------------------- #
    def env_searches(self) -> List[Search]:
        """Build searches from the static env vars + global criteria."""
        crit = self.cfg.criteria
        radius = self.cfg.ka_default_radius_km
        searches = [Search(u, "kleinanzeigen", crit, radius_km=radius) for u in self.cfg.ka_urls]
        searches += [Search(u, "rss", crit) for u in self.cfg.rss_urls]
        return searches

    # ----- NocoDB ----------------------------------------------------------- #
    def _headers(self) -> dict:
        return {"xc-token": self.cfg.nocodb_token, "Content-Type": "application/json"}

    def _records_url(self) -> str:
        base = self.cfg.nocodb_url.rstrip("/")
        return f"{base}/api/v2/tables/{self.cfg.nocodb_searches_table_id}/records"

    def _fetch_rows(self) -> List[dict]:
        rows: List[dict] = []
        offset = 0
        while True:
            resp = self._session.get(
                self._records_url(),
                headers=self._headers(),
                params={"limit": _NOCODB_PAGE, "offset": offset},
                timeout=self.cfg.http_timeout_s,
            )
            resp.raise_for_status()
            page = resp.json().get("list", [])
            rows.extend(page)
            if len(page) < _NOCODB_PAGE:
                return rows
            offset += _NOCODB_PAGE

    def _merged_criteria(self, row: dict) -> Criteria:
        """Per-search criteria, inheriting the global env default for blank cells."""
        g = self.cfg.criteria
        min_rent = _to_float(row.get(COL_MIN_RENT))
        max_rent = _to_float(row.get(COL_MAX_RENT))
        min_rooms = _to_float(row.get(COL_MIN_ROOMS))
        max_rooms = _to_float(row.get(COL_MAX_ROOMS))
        min_sqm = _to_float(row.get(COL_MIN_SQM))
        max_sqm = _to_float(row.get(COL_MAX_SQM))
        req = _to_keywords(row.get(COL_REQUIRED_KEYWORDS))
        exc = _to_keywords(row.get(COL_EXCLUDED_KEYWORDS))
        return Criteria(
            min_rent=min_rent if min_rent is not None else g.min_rent,
            max_rent=max_rent if max_rent is not None else g.max_rent,
            min_rooms=min_rooms if min_rooms is not None else g.min_rooms,
            max_rooms=max_rooms if max_rooms is not None else g.max_rooms,
            min_sqm=min_sqm if min_sqm is not None else g.min_sqm,
            max_sqm=max_sqm if max_sqm is not None else g.max_sqm,
            required_keywords=req if req is not None else g.required_keywords,
            excluded_keywords=exc if exc is not None else g.excluded_keywords,
        )

    def _row_to_search(self, row: dict) -> Optional[Search]:
        url = (row.get(COL_URL) or "").strip()
        if not url:
            return None
        source_type = (row.get(COL_SOURCE_TYPE) or "").strip().lower()
        if source_type not in VALID_SOURCE_TYPES:
            # Infer from the URL when the column is blank/unknown.
            source_type = "kleinanzeigen" if "kleinanzeigen" in url.lower() else "rss"
        radius = _to_float(row.get(COL_RADIUS_KM))
        return Search(
            url=url,
            source_type=source_type,
            criteria=self._merged_criteria(row),
            enabled=_to_bool(row.get(COL_ENABLED)),
            label=(row.get(COL_LABEL) or "").strip(),
            # Blank radius cell inherits the global KA_DEFAULT_RADIUS_KM default.
            radius_km=radius if radius is not None else self.cfg.ka_default_radius_km,
        )

    # ----- public ----------------------------------------------------------- #
    def get_searches(self) -> List[Search]:
        """Return the active searches for this cycle (NocoDB → cache → env)."""
        if not self.cfg.searches_from_nocodb:
            return self.env_searches()
        try:
            rows = self._fetch_rows()
        except (requests.RequestException, ValueError) as exc:
            fallback = "last-known-good cache" if self._cache else "env vars"
            log.warning("Could not read searches table (%s); using %s.", exc, fallback)
            return self._cache or self.env_searches()

        parsed = [self._row_to_search(r) for r in rows]
        active = [s for s in parsed if s and s.enabled]
        if not active:
            log.warning("Searches table empty or all-disabled; falling back to env vars.")
            return self.env_searches()
        self._cache = active
        log.info("Loaded %d active search(es) from NocoDB.", len(active))
        return active

    def schema_check(self) -> bool:
        """Verify the searches table is reachable on startup (D-style check)."""
        if not self.cfg.searches_from_nocodb:
            return False
        try:
            resp = self._session.get(
                self._records_url(), headers=self._headers(),
                params={"limit": 1}, timeout=self.cfg.http_timeout_s,
            )
            resp.raise_for_status()
            log.info("NocoDB searches table reachable.")
            return True
        except (requests.RequestException, ValueError) as exc:
            log.warning("Searches table unreachable, will use env searches: %s", exc)
            return False
