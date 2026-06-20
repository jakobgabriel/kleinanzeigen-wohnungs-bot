"""Run-logging to NocoDB (Epic E).

Every poll cycle ("run") is captured as one :class:`RunRecord` plus an ordered
list of :class:`RunEvent` phase events.  Events are buffered in memory during the
run and flushed exactly once at the end (≤ 1 run insert + 1 batch insert).

Run-logging is **observational, never load-bearing**: every public method here
is wrapped so that any failure is caught, logged at WARNING, and never
propagates to the poll loop.  When NocoDB is unreachable the run + events are
appended to a JSONL file and replayed on the next successful connection.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import requests

from .config import Config

log = logging.getLogger("flatwatch.runlog")

# Lifecycle phases, in canonical order (see §2 of the spec).
PHASES = (
    "run_start",
    "fetch_start",
    "fetch_source",
    "fetch_done",
    "filter",
    "dedup",
    "notify_start",
    "notify_item",
    "notify_done",
    "persist",
    "run_end",
    "run_error",
)

# NocoDB's bulk insert limit; batches larger than this are paginated.
NOCODB_BULK_LIMIT = 1000


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RunEvent:
    run_id: str
    seq: int
    phase: str
    level: str = "info"
    message: str = ""
    source: Optional[str] = None
    count: Optional[int] = None
    elapsed_ms: int = 0
    at: str = field(default_factory=_now_iso)
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass
class RunRecord:
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    started_at: str = field(default_factory=_now_iso)
    finished_at: Optional[str] = None
    duration_ms: Optional[int] = None
    status: str = "success"  # success | partial | failed
    trigger: str = "scheduled"  # scheduled | startup_prime | manual
    sources_polled: int = 0
    fetched: int = 0
    filtered: int = 0
    new: int = 0
    notified: int = 0
    errors: int = 0
    error_summary: str = ""
    host: str = field(default_factory=socket.gethostname)
    version: str = "dev"


class RunLogger:
    """Buffers a single run's record + events, then flushes them once at the end.

    Typical usage::

        run = runlogger.start(trigger="scheduled")
        run.event("fetch_start")
        ...
        run.finish(status="success")   # flushes to NocoDB / JSONL
    """

    def __init__(self, cfg: Config, session: Optional[requests.Session] = None):
        self.cfg = cfg
        self._session = session or requests.Session()

    @property
    def enabled(self) -> bool:
        return self.cfg.run_log_enabled

    def start(self, trigger: str = "scheduled") -> "Run":
        record = RunRecord(trigger=trigger, version=self.cfg.version)
        return Run(self, record)

    # ----- NocoDB plumbing -------------------------------------------------- #
    def _headers(self) -> dict:
        return {"xc-token": self.cfg.nocodb_token, "Content-Type": "application/json"}

    def _records_url(self, table_id: str) -> str:
        base = self.cfg.nocodb_url.rstrip("/")
        return f"{base}/api/v2/tables/{table_id}/records"

    def _nocodb_ready(self) -> bool:
        return bool(
            self.cfg.nocodb_url
            and self.cfg.nocodb_token
            and self.cfg.nocodb_runs_table_id
            and self.cfg.nocodb_run_events_table_id
        )

    def _insert_run(self, record: RunRecord) -> None:
        resp = self._session.post(
            self._records_url(self.cfg.nocodb_runs_table_id),
            headers=self._headers(),
            data=json.dumps(_run_payload(record)),
            timeout=self.cfg.http_timeout_s,
        )
        resp.raise_for_status()

    def _insert_events(self, events: List[RunEvent]) -> None:
        url = self._records_url(self.cfg.nocodb_run_events_table_id)
        for start in range(0, len(events), NOCODB_BULK_LIMIT):
            batch = [_event_payload(e) for e in events[start : start + NOCODB_BULK_LIMIT]]
            resp = self._session.post(
                url, headers=self._headers(), data=json.dumps(batch), timeout=self.cfg.http_timeout_s
            )
            resp.raise_for_status()

    # ----- Flush + fallback ------------------------------------------------- #
    def flush(self, record: RunRecord, events: List[RunEvent]) -> None:
        """Persist a finished run. Best-effort; never raises (E2)."""
        if not self.enabled:
            return
        try:
            if self._nocodb_ready():
                self._replay_backlog()  # opportunistically drain the JSONL backlog first
                self._insert_run(record)
                self._insert_events(events)
                log.info("Run %s logged to NocoDB (%d events).", record.run_id, len(events))
            else:
                self._append_jsonl(record, events)
        except (requests.RequestException, ValueError, OSError) as exc:
            log.warning("Run-log flush to NocoDB failed, falling back to JSONL: %s", exc)
            try:
                self._append_jsonl(record, events)
            except OSError as exc2:
                log.warning("Run-log JSONL fallback also failed: %s", exc2)
        except Exception as exc:  # observational: swallow anything
            log.warning("Unexpected run-log error (ignored): %s", exc)

    def _append_jsonl(self, record: RunRecord, events: List[RunEvent]) -> None:
        path = self.cfg.run_log_jsonl_path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        line = json.dumps({"run": asdict(record), "events": [asdict(e) for e in events]})
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        log.info("Run %s buffered to %s.", record.run_id, path)

    def _replay_backlog(self) -> None:
        """Upload any JSONL-buffered runs to NocoDB, then truncate on success (E2)."""
        path = self.cfg.run_log_jsonl_path
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            return
        with open(path, "r", encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        if not lines:
            return
        for ln in lines:
            obj = json.loads(ln)
            record = RunRecord(**obj["run"])
            events = [RunEvent(**e) for e in obj["events"]]
            self._insert_run(record)
            self._insert_events(events)
        os.remove(path)
        log.info("Replayed %d buffered run(s) from %s to NocoDB.", len(lines), path)

    # ----- Retention (E4) --------------------------------------------------- #
    def prune(self) -> None:
        """Delete runs (and their events) older than the retention window."""
        days = self.cfg.run_log_retention_days
        if not self.enabled or days <= 0 or not self._nocodb_ready():
            return
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        try:
            self._prune_table(self.cfg.nocodb_runs_table_id, "started_at", cutoff)
            self._prune_table(self.cfg.nocodb_run_events_table_id, "at", cutoff)
        except (requests.RequestException, ValueError) as exc:
            log.warning("Run-log pruning failed (non-fatal): %s", exc)

    def _prune_table(self, table_id: str, field_name: str, cutoff_iso: str) -> None:
        url = self._records_url(table_id)
        while True:
            resp = self._session.get(
                url,
                headers=self._headers(),
                params={"where": f"({field_name},lt,{cutoff_iso})", "limit": NOCODB_BULK_LIMIT},
                timeout=self.cfg.http_timeout_s,
            )
            resp.raise_for_status()
            rows = resp.json().get("list", [])
            if not rows:
                return
            ids = [{"Id": r.get("Id") or r.get("id")} for r in rows if (r.get("Id") or r.get("id"))]
            if not ids:
                return
            self._session.delete(url, headers=self._headers(), data=json.dumps(ids), timeout=self.cfg.http_timeout_s)


class Run:
    """A single in-flight run; accumulates events and totals in memory."""

    def __init__(self, logger: RunLogger, record: RunRecord):
        self._logger = logger
        self.record = record
        self.events: List[RunEvent] = []
        self._seq = 0
        self._t0 = time.monotonic()
        self._failed_sources: List[str] = []
        self.event("run_start", message=f"trigger={record.trigger}")

    def _elapsed_ms(self) -> int:
        return int((time.monotonic() - self._t0) * 1000)

    def event(
        self,
        phase: str,
        *,
        level: str = "info",
        message: str = "",
        source: Optional[str] = None,
        count: Optional[int] = None,
    ) -> None:
        """Append a lifecycle event. Never raises (observational)."""
        try:
            self._seq += 1
            self.events.append(
                RunEvent(
                    run_id=self.record.run_id,
                    seq=self._seq,
                    phase=phase,
                    level=level,
                    message=message,
                    source=source,
                    count=count,
                    elapsed_ms=self._elapsed_ms(),
                )
            )
            if level == "error":
                self.record.errors += 1
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("Failed to record run event %s (ignored): %s", phase, exc)

    def source_failed(self, source: str, reason: str, *, blocked: bool = False) -> None:
        """Record a failed/blocked source as a fetch_source error event (E3)."""
        self._failed_sources.append(source)
        phase_note = "403_blocked" if blocked else "failed"
        self.event(
            "fetch_source",
            level="warning" if blocked else "error",
            message=f"{phase_note}: {reason}",
            source=source,
        )

    def capture_error(self, phase: str, exc: BaseException) -> None:
        """Record an unhandled exception as a run_error event (E3)."""
        tb = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        self.event("run_error", level="error", message=f"in {phase}: {tb}")

    def finish(
        self,
        *,
        status: Optional[str] = None,
        sources_polled: int = 0,
        fetched: int = 0,
        filtered: int = 0,
        new: int = 0,
        notified: int = 0,
    ) -> RunRecord:
        """Close the run, classify status (E3), and flush (E2). Never raises."""
        rec = self.record
        rec.sources_polled = sources_polled
        rec.fetched = fetched
        rec.filtered = filtered
        rec.new = new
        rec.notified = notified
        rec.finished_at = _now_iso()
        rec.duration_ms = self._elapsed_ms()
        rec.status = status or self._classify(sources_polled)
        if self._failed_sources:
            rec.error_summary = (rec.error_summary + " failed_sources=" + ",".join(self._failed_sources)).strip()
        self.event("run_end", message=f"status={rec.status} new={new} notified={notified}")
        self._logger.flush(rec, self.events)
        return rec

    def _classify(self, sources_polled: int) -> str:
        """success / partial / failed based on source outcomes (E3)."""
        n_failed = len(self._failed_sources)
        if n_failed == 0 and self.record.errors == 0:
            return "success"
        if sources_polled > 0 and n_failed >= sources_polled:
            return "failed"
        return "partial"


def _run_payload(record: RunRecord) -> dict:
    d = asdict(record)
    # error_summary may be long; NocoDB text columns are fine but keep it bounded.
    if d.get("error_summary"):
        d["error_summary"] = d["error_summary"][:2000]
    return d


def _event_payload(event: RunEvent) -> dict:
    d = asdict(event)
    if d.get("message"):
        d["message"] = d["message"][:2000]
    return d
