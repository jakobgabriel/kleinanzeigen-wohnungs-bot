"""Dedup store: NocoDB primary, JSON file fallback (union semantics).

A listing is *new* only if its id is absent from ``NocoDB ∪ JSON``.  After
notifying, the id is written to both.  NocoDB being unreachable degrades to the
JSON file (logged, non-fatal) — correctness never depends on NocoDB being up.

Reads are served from an **in-memory id set** loaded once (and kept current as
new ids are marked), not a per-listing query: ``is_new`` never touches the
network, so a slow/unreachable NocoDB can never turn a cycle into an
N×timeout stall.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Iterable, Optional, Set

import requests

from .config import Config

log = logging.getLogger("flatwatch.store")

# Page size for the one-shot seen-id load (NocoDB caps page size around 1000).
_NOCODB_PAGE = 1000


class SeenStore:
    """Tracks which listing ids have already been notified."""

    def __init__(self, cfg: Config, session: Optional[requests.Session] = None):
        self.cfg = cfg
        self._session = session or requests.Session()
        self._lock = threading.Lock()
        self._json_ids: Set[str] = self._load_json()
        # In-memory mirror of the NocoDB seen-ids, loaded once via begin_cycle().
        self._nocodb_ids: Set[str] = set()
        self._nocodb_loaded = False
        self._nocodb_ok = cfg.nocodb_enabled

    # ----- JSON fallback ---------------------------------------------------- #
    def _load_json(self) -> Set[str]:
        path = self.cfg.json_store_path
        if not os.path.exists(path):
            return set()
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return set(data.get("seen", []) if isinstance(data, dict) else data)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Could not read JSON store %s: %s", path, exc)
            return set()

    def _save_json(self) -> None:
        path = self.cfg.json_store_path
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"seen": sorted(self._json_ids)}, fh)
            os.replace(tmp, path)
        except OSError as exc:
            log.error("Could not write JSON store %s: %s", path, exc)

    # ----- NocoDB ----------------------------------------------------------- #
    def _nocodb_headers(self) -> dict:
        return {"xc-token": self.cfg.nocodb_token, "Content-Type": "application/json"}

    def _nocodb_url(self) -> str:
        base = self.cfg.nocodb_url.rstrip("/")
        return f"{base}/api/v2/tables/{self.cfg.nocodb_table_id}/records"

    def _load_nocodb_ids(self) -> None:
        """Load the full set of seen ids from NocoDB into memory (one paginated read).

        On success the cache is authoritative for the process; on failure we keep
        whatever we had and degrade to JSON for this cycle (a single failure, not
        one per listing). Retried by :meth:`begin_cycle` until it succeeds.
        """
        if not self.cfg.nocodb_enabled:
            return
        field = self.cfg.nocodb_id_field
        ids: Set[str] = set()
        offset = 0
        try:
            while True:
                resp = self._session.get(
                    self._nocodb_url(),
                    headers=self._nocodb_headers(),
                    params={"limit": _NOCODB_PAGE, "offset": offset, "fields": field},
                    timeout=self.cfg.http_timeout_s,
                )
                resp.raise_for_status()
                rows = resp.json().get("list", [])
                ids.update(r.get(field) for r in rows if r.get(field))
                if len(rows) < _NOCODB_PAGE:
                    break
                offset += _NOCODB_PAGE
            with self._lock:
                self._nocodb_ids = ids
            self._nocodb_loaded = True
            self._nocodb_ok = True
            log.info("Loaded %d seen id(s) from NocoDB.", len(ids))
        except (requests.RequestException, ValueError) as exc:
            self._note_nocodb_down(exc)

    def _nocodb_insert(self, listing_id: str, extra: Optional[dict] = None) -> bool:
        if not self.cfg.nocodb_enabled:
            return False
        payload = {self.cfg.nocodb_id_field: listing_id}
        if extra:
            payload.update(extra)
        try:
            resp = self._session.post(
                self._nocodb_url(),
                headers=self._nocodb_headers(),
                data=json.dumps(payload),
                timeout=self.cfg.http_timeout_s,
            )
            resp.raise_for_status()
            return True
        except (requests.RequestException, ValueError) as exc:
            self._note_nocodb_down(exc)
            return False

    def _note_nocodb_down(self, exc: Exception) -> None:
        if self._nocodb_ok:
            log.warning("NocoDB unreachable, degrading to JSON store: %s", exc)
        self._nocodb_ok = False

    # ----- Public API ------------------------------------------------------- #
    def begin_cycle(self) -> None:
        """Ensure the NocoDB seen-id cache is loaded; retry if a prior load failed.

        Called once at the start of each cycle. After the first successful load it
        is a no-op (steady-state reads cost no network), but if NocoDB was down it
        keeps retrying so the cache re-syncs once NocoDB returns.
        """
        if not self.cfg.nocodb_enabled or self._nocodb_loaded:
            return
        self._load_nocodb_ids()

    def is_new(self, listing_id: str) -> bool:
        """A listing is new only if absent from NocoDB ∪ JSON (in-memory, no I/O)."""
        with self._lock:
            return listing_id not in self._json_ids and listing_id not in self._nocodb_ids

    def mark_seen(self, listing_id: str, extra: Optional[dict] = None) -> None:
        """Persist a listing id to both stores (NocoDB best-effort, JSON always)."""
        with self._lock:
            self._json_ids.add(listing_id)
            self._nocodb_ids.add(listing_id)
            self._save_json()
        self._nocodb_insert(listing_id, extra)

    def prime(self, listing_ids: Iterable[str]) -> None:
        """Mark a batch of ids as seen during the silent startup prime."""
        ids = list(listing_ids)
        with self._lock:
            self._json_ids.update(ids)
            self._nocodb_ids.update(ids)
            self._save_json()
        for lid in ids:
            self._nocodb_insert(lid)

    def schema_check(self) -> bool:
        """D1: verify the NocoDB table is reachable and the id field exists."""
        if not self.cfg.nocodb_enabled:
            return False
        try:
            resp = self._session.get(
                self._nocodb_url(),
                headers=self._nocodb_headers(),
                params={"limit": 1},
                timeout=self.cfg.http_timeout_s,
            )
            resp.raise_for_status()
            rows = resp.json().get("list", [])
            if rows and self.cfg.nocodb_id_field not in rows[0]:
                log.warning(
                    "NocoDB table reachable but id field %r not found in records — "
                    "check NOCODB_ID_FIELD.",
                    self.cfg.nocodb_id_field,
                )
                return False
            log.info("NocoDB dedup table reachable.")
            return True
        except (requests.RequestException, ValueError) as exc:
            log.warning("NocoDB schema check failed, falling back to JSON: %s", exc)
            self._nocodb_ok = False
            return False
