"""flatwatch entry point: the poll loop.

Run with ``python -m app.main``.  Each cycle fetches every source, filters
against the criteria, dedups against NocoDB ∪ JSON, notifies new matches, and
persists them — instrumented end-to-end with run-logging (Epic E).  The first
cycle primes silently (no backlog spam).  One bad cycle never kills the loop:
catch-log-continue.
"""

from __future__ import annotations

import logging
import random
import signal
import sys
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

import requests

from .config import Config, Criteria, load_config
from .filters import matches
from .health import start_health_server, write_health
from .models import Listing
from .notify import Notifier
from .results import ResultsSink
from .runlog import Run, RunLogger
from .searches import Search, SearchProvider
from . import sources
from .store import SeenStore

# A fetched listing paired with the criteria of the search it came from.
Pair = Tuple[Listing, Criteria]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("flatwatch.main")

_STOP = False
_TRIGGER_NOW = False


def _handle_signal(signum, _frame):
    global _STOP
    log.info("Received signal %s — finishing current cycle then exiting.", signum)
    _STOP = True


def _handle_trigger(signum, _frame):
    """SIGUSR1 → run one cycle immediately, tagged trigger=manual."""
    global _TRIGGER_NOW
    log.info("Received signal %s — scheduling an on-demand manual cycle.", signum)
    _TRIGGER_NOW = True


def fetch_all(cfg: Config, searches: List[Search], session: requests.Session, run: Run) -> Tuple[List[Pair], int]:
    """Fetch every active search. Returns ([(listing, search criteria)], polled).

    Each listing is paired with the criteria of the search it came from, so
    per-search bounds (from the NocoDB searches table) are applied downstream.
    """
    ka = sum(1 for s in searches if s.source_type == "kleinanzeigen")
    rss = len(searches) - ka
    run.event("fetch_start", message=f"{ka} KA + {rss} RSS searches")
    pairs: List[Pair] = []
    polled = 0

    for search in searches:
        polled += 1
        try:
            if search.source_type == "kleinanzeigen":
                found = sources.fetch_kleinanzeigen(
                    search.url,
                    user_agent=cfg.user_agent,
                    timeout=cfg.http_timeout_s,
                    max_retries=cfg.max_retries,
                    session=session,
                    max_pages=cfg.ka_max_pages,
                    per_request_delay_s=cfg.per_request_delay_s,
                    request_jitter_s=cfg.request_jitter_s,
                )
            else:
                found = sources.fetch_rss(
                    search.url,
                    user_agent=cfg.user_agent,
                    timeout=cfg.http_timeout_s,
                    max_retries=cfg.max_retries,
                    session=session,
                )
            pairs.extend((listing, search.criteria) for listing in found)
            run.event("fetch_source", message="ok", source=search.url, count=len(found))
        except sources.FetchError as exc:
            run.source_failed(search.url, str(exc), blocked=getattr(exc, "blocked", False))
            if getattr(exc, "blocked", False):
                log.warning("Source blocked (403): %s", search.url)
            else:
                log.error("Source failed: %s — %s", search.url, exc)
        except Exception as exc:  # never let one source kill the cycle
            run.source_failed(search.url, repr(exc))
            log.exception("Unexpected error fetching %s", search.url)
        sources.polite_pause(cfg.per_request_delay_s, cfg.request_jitter_s)

    run.event("fetch_done", count=len(pairs))
    return pairs, polled


def _enrich_new(cfg: Config, session: requests.Session, run: Run, new_pairs: List[Pair]) -> List[Pair]:
    """Enrich Kleinanzeigen listings missing price/rooms/sqm, then re-filter (B2).

    Only new (not-yet-seen) listings reach here, so a detail page is never
    fetched for an already-seen id. Each detail page is fetched at most once,
    spaced by PER_REQUEST_DELAY_S. Listings that fall out of their search's
    criteria once their real values are known are dropped here.
    """
    enriched_count = 0
    for listing, _criteria in new_pairs:
        if listing.source != "kleinanzeigen" or not listing._missing:
            continue
        try:
            fields = sources.fetch_kleinanzeigen_detail(
                listing.url,
                user_agent=cfg.user_agent,
                timeout=cfg.http_timeout_s,
                max_retries=cfg.max_retries,
                session=session,
            )
            before = listing._missing
            sources.enrich_listing(listing, fields)
            if listing._missing != before:
                enriched_count += 1
        except sources.FetchError as exc:
            run.event("fetch_source", level="warning", message=f"enrich failed: {exc}", source=listing.url)
        except Exception as exc:  # enrichment is best-effort, never fatal
            log.warning("Detail enrichment failed for %s: %s", listing.url, exc)
        sources.polite_pause(cfg.per_request_delay_s, cfg.request_jitter_s)

    refiltered = [(l, c) for (l, c) in new_pairs if matches(l, c)]
    run.event(
        "filter",
        message="re-filter after enrichment",
        count=len(refiltered),
    )
    if enriched_count:
        log.info("Enriched %d listing(s) from detail pages; %d remain after re-filter.",
                 enriched_count, len(refiltered))
    return refiltered


@dataclass
class CycleState:
    """Cross-cycle health state, owned by the poll loop (#6/#7)."""
    consecutive_failures: int = 0
    last_success_at: Optional[str] = None
    alerted: bool = False


def run_cycle(
    cfg: Config,
    store: SeenStore,
    notifier: Notifier,
    runlogger: RunLogger,
    session: requests.Session,
    *,
    prime: bool,
    trigger: Optional[str] = None,
    search_provider: Optional[SearchProvider] = None,
    state: Optional["CycleState"] = None,
    results: Optional[ResultsSink] = None,
) -> dict:
    """Execute one poll cycle. Returns the heartbeat stats dict.

    ``trigger`` overrides the recorded trigger; when omitted it is derived as
    ``startup_prime`` on the priming run and ``scheduled`` thereafter.
    ``search_provider`` supplies the active searches (NocoDB table or env vars).
    ``state`` carries cross-cycle health info (consecutive failures, last success).
    """
    state = state if state is not None else CycleState()
    results = results if results is not None else ResultsSink(cfg, session)
    run = runlogger.start(trigger=trigger or ("startup_prime" if prime else "scheduled"))
    provider = search_provider or SearchProvider(cfg, session)
    notified = 0
    new_count = 0
    filtered_count = 0
    fetched_count = 0
    polled = 0
    status = None

    try:
        searches = provider.get_searches()
        pairs, polled = fetch_all(cfg, searches, session, run)
        fetched_count = len(pairs)

        # Filter each listing against the criteria of its own search.
        candidates = [(l, c) for (l, c) in pairs if matches(l, c)]
        # Collapse listings matched by more than one overlapping search.
        candidates = _dedup_pairs(candidates)
        filtered_count = len(candidates)
        run.event("filter", message="candidates kept", count=filtered_count)

        # Dedup against the seen-store (loads the NocoDB id cache once, not per listing)
        store.begin_cycle()
        new_pairs = [(l, c) for (l, c) in candidates if store.is_new(l.listing_id)]
        run.event("dedup", message=f"{len(new_pairs)} new of {filtered_count} candidates", count=len(new_pairs))

        # Detail-page enrichment (B2): fill missing fields on new listings only
        # (never refetches an already-seen id), then re-filter with real values.
        if cfg.enrich_detail and new_pairs:
            new_pairs = _enrich_new(cfg, session, run, new_pairs)
        new_listings = [l for (l, _c) in new_pairs]
        new_count = len(new_listings)

        if prime:
            # Silent prime: record everything (incl. full results) but notify nothing.
            _persist(store, results, new_listings)
            run.event("persist", message="silent prime", count=new_count)
        else:
            notified = _notify_new(cfg, store, notifier, results, run, new_listings)

    except Exception as exc:  # catch-log-continue; record + classify as failed
        log.exception("Cycle failed")
        run.capture_error("run_cycle", exc)
        status = "failed"

    record = run.finish(
        status=status,
        sources_polled=polled,
        fetched=fetched_count,
        filtered=filtered_count,
        new=new_count,
        notified=notified,
    )

    # Cross-cycle failure tracking (#6/#7): only a fully failed cycle counts.
    if record.status == "failed":
        state.consecutive_failures += 1
    else:
        state.consecutive_failures = 0
        state.last_success_at = record.finished_at

    stats = {
        "status": record.status,
        "last_cycle": record.finished_at,
        "last_success_at": state.last_success_at,
        "consecutive_failures": state.consecutive_failures,
        "duration_ms": record.duration_ms,
        "sources_polled": polled,
        "fetched": fetched_count,
        "filtered": filtered_count,
        "new_count": new_count,
        "notified": notified,
        "errors": record.errors,
        # Freshness metadata the /health handler uses to decide 200 vs 503 (#6).
        "last_cycle_epoch": time.time(),
        "stale_after_s": cfg.health_stale_after_s,
        "fail_threshold": cfg.failure_alert_threshold,
    }
    # A4: one greppable key=value summary line.
    log.info(
        "cycle_complete sources_polled=%d fetched=%d filtered=%d new=%d notified=%d errors=%d "
        "status=%s consecutive_failures=%d duration_ms=%s",
        polled, fetched_count, filtered_count, new_count, notified, record.errors,
        record.status, state.consecutive_failures, record.duration_ms,
    )
    write_health(cfg.health_path, stats)
    _maybe_alert(cfg, notifier, state)
    return stats


def _maybe_alert(cfg: Config, notifier: Notifier, state: CycleState) -> None:
    """Fire one alert per sustained outage, and a recovery note (#7). Never raises."""
    if not cfg.alert_on_failures:
        return
    try:
        if state.consecutive_failures >= cfg.failure_alert_threshold and not state.alerted:
            state.alerted = True
            notifier.send_alert(
                f"⚠️ flatwatch: {state.consecutive_failures} cycles failed in a row — "
                f"check sources/logs."
            )
        elif state.consecutive_failures == 0 and state.alerted:
            state.alerted = False
            notifier.send_alert("✅ flatwatch: recovered, cycles succeeding again.")
    except Exception as exc:  # alerting is observational, never load-bearing
        log.warning("Failure-alert send error (ignored): %s", exc)


# A listing is only marked seen once it's actually delivered. If every configured
# channel fails it stays unseen and is retried next cycle — but bounded, so a
# permanently-undeliverable listing can't loop forever.
MAX_NOTIFY_ATTEMPTS = 5
_NOTIFY_ATTEMPTS: dict = {}


def _settle(listing: Listing, delivered: bool, run: Run, persist: List[Listing], *, note: str) -> bool:
    """Decide a listing's fate after a notify attempt (#1). Returns delivered.

    Appends to ``persist`` (the batch to mark seen, flushed once by the caller)
    when delivered, or when the bounded retry budget is exhausted. Otherwise the
    listing is left unseen so the next cycle retries it.
    """
    lid = listing.listing_id
    if delivered:
        _NOTIFY_ATTEMPTS.pop(lid, None)
        persist.append(listing)
        run.event("notify_item", message=note, source=listing.source)
        return True

    attempts = _NOTIFY_ATTEMPTS.get(lid, 0) + 1
    _NOTIFY_ATTEMPTS[lid] = attempts
    if attempts >= MAX_NOTIFY_ATTEMPTS:
        _NOTIFY_ATTEMPTS.pop(lid, None)
        persist.append(listing)
        log.warning("Giving up on %s after %d failed notify attempts; marking seen.", lid, attempts)
        run.event("notify_item", level="error", message=f"{note}; gave up after {attempts} attempts", source=listing.source)
    else:
        log.warning("Notification undelivered for %s (attempt %d) — will retry next cycle.", lid, attempts)
        run.event("notify_item", level="warning", message=f"{note}; undelivered, retry next cycle ({attempts})", source=listing.source)
    return False


def _notify_new(cfg: Config, store: SeenStore, notifier: Notifier, results: ResultsSink, run: Run, new_listings: List[Listing]) -> int:
    """Notify new listings with a batching guard (C2). Returns count notified."""
    if not new_listings:
        run.event("notify_start", count=0)
        run.event("notify_done", count=0)
        return 0

    run.event("notify_start", count=len(new_listings))
    channels = cfg.telegram_enabled or cfg.email_enabled or cfg.ha_webhook_url
    cap = cfg.max_notify_per_cycle
    individual = new_listings[:cap]
    overflow = new_listings[cap:]
    notified = 0
    persist: List[Listing] = []

    for listing in individual:
        result = notifier.notify(listing)
        delivered = result.any_sent or not channels
        note = f"telegram={result.telegram_ok} email={result.email_ok} ha={result.ha_ok}"
        if _settle(listing, delivered, run, persist, note=note):
            notified += 1

    if overflow:
        sent = notifier.send_summary(overflow)
        delivered = sent or not channels
        for listing in overflow:
            _settle(listing, delivered, run, persist, note=f"summary({len(overflow)})")

    # Persist exactly the listings we're committing this cycle (one batch).
    if persist:
        _persist(store, results, persist)
        run.event("persist", message="marked seen", count=len(persist))

    run.event("notify_done", count=notified)
    return notified


def _persist(store: SeenStore, results: ResultsSink, listings: List[Listing]) -> None:
    """Mark listings seen (one batch) and write their full rows to the results table."""
    if not listings:
        return
    store.mark_seen_many([(l.listing_id, _seen_extra(l)) for l in listings])
    results.write(listings)


def _dedup_pairs(pairs: List[Pair]) -> List[Pair]:
    """Drop within-cycle duplicate listings (same id from overlapping searches)."""
    seen = set()
    out: List[Pair] = []
    for listing, criteria in pairs:
        if listing.listing_id in seen:
            continue
        seen.add(listing.listing_id)
        out.append((listing, criteria))
    return out


def _seen_extra(listing: Listing) -> dict:
    return {"title": listing.title, "url": listing.url, "source": listing.source}


def main() -> int:
    global _TRIGGER_NOW
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    if hasattr(signal, "SIGUSR1"):  # POSIX only; enables `docker kill -s USR1`
        signal.signal(signal.SIGUSR1, _handle_trigger)

    cfg = load_config()
    session = requests.Session()
    store = SeenStore(cfg, session=session)
    notifier = Notifier(cfg, session=session)
    runlogger = RunLogger(cfg, session=session)
    search_provider = SearchProvider(cfg, session=session)
    results = ResultsSink(cfg, session=session)

    store.schema_check()
    if cfg.searches_from_nocodb:
        search_provider.schema_check()
    runlogger.prune()  # E4: prune old run-logs on startup
    start_health_server(cfg.healthcheck_port)

    source = "NocoDB searches table" if cfg.searches_from_nocodb else "env vars"
    log.info(
        "flatwatch starting: searches from %s, poll every %d min.",
        source, cfg.poll_interval_min,
    )

    first = True
    manual = False
    state = CycleState()
    while not _STOP:
        try:
            trigger = "manual" if manual else None
            run_cycle(cfg, store, notifier, runlogger, session,
                      prime=first, trigger=trigger, search_provider=search_provider,
                      state=state, results=results)
        except Exception:  # ultimate backstop — the loop must never die
            log.exception("Unexpected top-level cycle error; continuing.")
        first = False
        manual = False

        if _STOP:
            break
        # Wake early for an on-demand manual cycle (SIGUSR1); otherwise sleep the interval.
        if _sleep_interval(cfg.poll_interval_min) and _TRIGGER_NOW:
            _TRIGGER_NOW = False
            manual = True

    log.info("flatwatch stopped.")
    return 0


def _sleep_interval(poll_interval_min: int) -> bool:
    """Sleep the poll interval in 1s slices so signals interrupt promptly.

    Returns early (True) if a manual trigger arrives mid-sleep, else returns
    True after the full interval; only an exit signal makes it stop short.
    """
    total = poll_interval_min * 60 + random.uniform(0, 30)  # small jitter
    waited = 0.0
    while waited < total and not _STOP:
        if _TRIGGER_NOW:
            return True
        time.sleep(1)
        waited += 1
    return True


if __name__ == "__main__":
    sys.exit(main())
