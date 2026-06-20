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
from typing import List, Optional, Tuple

import requests

from .config import Config, load_config
from .filters import matches
from .health import start_health_server, write_health
from .models import Listing
from .notify import Notifier
from .runlog import Run, RunLogger
from . import sources
from .store import SeenStore

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


def fetch_all(cfg: Config, session: requests.Session, run: Run) -> Tuple[List[Listing], int, int]:
    """Fetch every configured source. Returns (listings, sources_polled, n_errors)."""
    run.event("fetch_start", message=f"{len(cfg.ka_urls)} KA + {len(cfg.rss_urls)} RSS")
    listings: List[Listing] = []
    polled = 0

    jobs = [("kleinanzeigen", url, sources.fetch_kleinanzeigen) for url in cfg.ka_urls]
    jobs += [("rss", url, sources.fetch_rss) for url in cfg.rss_urls]

    for source_name, url, fetcher in jobs:
        polled += 1
        try:
            found = fetcher(
                url,
                user_agent=cfg.user_agent,
                timeout=cfg.http_timeout_s,
                max_retries=cfg.max_retries,
                session=session,
            )
            listings.extend(found)
            run.event("fetch_source", message="ok", source=url, count=len(found))
        except sources.FetchError as exc:
            run.source_failed(url, str(exc), blocked=getattr(exc, "blocked", False))
            if getattr(exc, "blocked", False):
                log.warning("Source blocked (403): %s", url)
            else:
                log.error("Source failed: %s — %s", url, exc)
        except Exception as exc:  # never let one source kill the cycle
            run.source_failed(url, repr(exc))
            log.exception("Unexpected error fetching %s", url)
        sources.polite_pause(cfg.per_request_delay_s, cfg.request_jitter_s)

    run.event("fetch_done", count=len(listings))
    return listings, polled, len(run._failed_sources)


def _enrich_new(cfg: Config, session: requests.Session, run: Run, new_listings: List[Listing]) -> List[Listing]:
    """Enrich Kleinanzeigen listings missing price/rooms/sqm, then re-filter (B2).

    Only new (not-yet-seen) listings reach here, so a detail page is never
    fetched for an already-seen id. Each detail page is fetched at most once,
    spaced by PER_REQUEST_DELAY_S. Listings that fall out of criteria once their
    real values are known are dropped here.
    """
    enriched_count = 0
    for listing in new_listings:
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

    refiltered = [l for l in new_listings if matches(l, cfg.criteria)]
    run.event(
        "filter",
        message="re-filter after enrichment",
        count=len(refiltered),
    )
    if enriched_count:
        log.info("Enriched %d listing(s) from detail pages; %d remain after re-filter.",
                 enriched_count, len(refiltered))
    return refiltered


def run_cycle(
    cfg: Config,
    store: SeenStore,
    notifier: Notifier,
    runlogger: RunLogger,
    session: requests.Session,
    *,
    prime: bool,
    trigger: Optional[str] = None,
) -> dict:
    """Execute one poll cycle. Returns the heartbeat stats dict.

    ``trigger`` overrides the recorded trigger; when omitted it is derived as
    ``startup_prime`` on the priming run and ``scheduled`` thereafter.
    """
    run = runlogger.start(trigger=trigger or ("startup_prime" if prime else "scheduled"))
    notified = 0
    new_count = 0
    filtered_count = 0
    fetched_count = 0
    polled = 0
    status = None

    try:
        listings, polled, _ = fetch_all(cfg, session, run)
        fetched_count = len(listings)

        # Filter
        candidates = [l for l in listings if matches(l, cfg.criteria)]
        filtered_count = len(candidates)
        run.event("filter", message="candidates kept", count=filtered_count)

        # Dedup
        new_listings = [l for l in candidates if store.is_new(l.listing_id)]
        run.event("dedup", message=f"{len(new_listings)} new of {filtered_count} candidates", count=len(new_listings))

        # Detail-page enrichment (B2): fill missing fields on new listings only
        # (never refetches an already-seen id), then re-filter with real values.
        if cfg.enrich_detail and new_listings:
            new_listings = _enrich_new(cfg, session, run, new_listings)
        new_count = len(new_listings)

        if prime:
            # Silent prime: mark everything seen, notify nothing.
            store.prime([l.listing_id for l in new_listings])
            run.event("persist", message="silent prime", count=new_count)
        else:
            notified = _notify_new(cfg, store, notifier, run, new_listings)

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

    stats = {
        "status": record.status,
        "last_cycle": record.finished_at,
        "duration_ms": record.duration_ms,
        "sources_polled": polled,
        "fetched": fetched_count,
        "filtered": filtered_count,
        "new_count": new_count,
        "notified": notified,
        "errors": record.errors,
    }
    # A4: one greppable key=value summary line.
    log.info(
        "cycle_complete sources_polled=%d fetched=%d filtered=%d new=%d notified=%d errors=%d status=%s duration_ms=%s",
        polled, fetched_count, filtered_count, new_count, notified, record.errors, record.status, record.duration_ms,
    )
    write_health(cfg.health_path, stats)
    return stats


def _notify_new(cfg: Config, store: SeenStore, notifier: Notifier, run: Run, new_listings: List[Listing]) -> int:
    """Notify new listings with a batching guard (C2). Returns count notified."""
    if not new_listings:
        run.event("notify_start", count=0)
        run.event("notify_done", count=0)
        return 0

    run.event("notify_start", count=len(new_listings))
    cap = cfg.max_notify_per_cycle
    individual = new_listings[:cap]
    overflow = new_listings[cap:]
    notified = 0

    for listing in individual:
        result = notifier.notify(listing)
        if result.any_sent or not (cfg.telegram_enabled or cfg.email_enabled or cfg.ha_webhook_url):
            notified += 1
        level = "warning" if result.any_failed else "info"
        run.event(
            "notify_item",
            level=level,
            message=f"telegram={result.telegram_ok} email={result.email_ok} ha={result.ha_ok}",
            source=listing.source,
        )
        store.mark_seen(listing.listing_id, extra=_seen_extra(listing))

    if overflow:
        notifier.send_summary(overflow)
        for listing in overflow:
            store.mark_seen(listing.listing_id, extra=_seen_extra(listing))
        run.event("notify_item", message=f"summary for {len(overflow)} overflow listings", count=len(overflow))

    run.event("notify_done", count=notified)
    run.event("persist", message="marked seen", count=len(new_listings))
    return notified


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

    store.schema_check()
    runlogger.prune()  # E4: prune old run-logs on startup
    start_health_server(cfg.healthcheck_port)

    log.info(
        "flatwatch starting: %d KA + %d RSS sources, poll every %d min.",
        len(cfg.ka_urls), len(cfg.rss_urls), cfg.poll_interval_min,
    )

    first = True
    manual = False
    while not _STOP:
        try:
            trigger = "manual" if manual else None
            run_cycle(cfg, store, notifier, runlogger, session, prime=first, trigger=trigger)
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
