"""12-factor configuration for flatwatch.

Everything is driven by environment variables (documented in ``.env.example``).
:func:`load_config` reads them once at startup, validates them (D2), and returns
an immutable :class:`Config`.  ``Criteria`` holds the user's filter bounds; any
bound left ``None`` is simply not applied.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import List, Optional

log = logging.getLogger("flatwatch.config")


# --------------------------------------------------------------------------- #
# Small env helpers
# --------------------------------------------------------------------------- #
def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


def _env_float(name: str) -> Optional[float]:
    raw = _env(name)
    if raw is None:
        return None
    try:
        return float(raw.replace(",", "."))
    except ValueError:
        log.warning("Ignoring non-numeric %s=%r", name, raw)
        return None


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("Ignoring non-integer %s=%r, using default %d", name, raw, default)
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _env_list(name: str, sep: str = ",") -> List[str]:
    raw = _env(name)
    if not raw:
        return []
    return [item.strip() for item in raw.split(sep) if item.strip()]


def _env_url_list(name: str) -> List[str]:
    """Split a URL list on newlines and/or commas (URLs never contain either)."""
    raw = _env(name)
    if not raw:
        return []
    normalized = raw.replace("\n", ",")
    return [item.strip() for item in normalized.split(",") if item.strip()]


# --------------------------------------------------------------------------- #
# Criteria
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Criteria:
    """User filter bounds. A ``None`` bound means "don't constrain this axis"."""

    min_rent: Optional[float] = None
    max_rent: Optional[float] = None
    min_rooms: Optional[float] = None
    max_rooms: Optional[float] = None
    min_sqm: Optional[float] = None
    max_sqm: Optional[float] = None
    required_keywords: List[str] = field(default_factory=list)
    excluded_keywords: List[str] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> "Criteria":
        return cls(
            min_rent=_env_float("MIN_RENT"),
            max_rent=_env_float("MAX_RENT"),
            min_rooms=_env_float("MIN_ROOMS"),
            max_rooms=_env_float("MAX_ROOMS"),
            min_sqm=_env_float("MIN_SQM"),
            max_sqm=_env_float("MAX_SQM"),
            required_keywords=[k.lower() for k in _env_list("REQUIRED_KEYWORDS")],
            excluded_keywords=[k.lower() for k in _env_list("EXCLUDED_KEYWORDS")],
        )


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Config:
    # Sources
    ka_urls: List[str]
    rss_urls: List[str]
    criteria: Criteria

    # Polling / politeness
    poll_interval_min: int
    user_agent: str
    per_request_delay_s: float
    request_jitter_s: float
    http_timeout_s: float
    max_retries: int

    # Source robustness
    enrich_detail: bool
    ka_max_pages: int
    persist_batch_size: int   # store the prime backlog incrementally in batches
    enrich_concurrency: int   # parallel detail-page fetches (1 = sequential)
    enrich_delay_s: float     # per-worker pause during enrichment (shorter than the search delay)
    content_dedup_enabled: bool  # collapse the same flat reposted under different ad-ids

    # Daily availability recheck (tags removed listings in the results table)
    recheck_enabled: bool
    recheck_interval_days: int

    # Dedup store
    json_store_path: str
    nocodb_url: Optional[str]
    nocodb_token: Optional[str]
    nocodb_table_id: Optional[str]
    nocodb_id_field: str

    # Dynamic searches table (env URLs/criteria are the fallback)
    nocodb_searches_table_id: Optional[str]

    # Results sink: full listing rows written to a dedicated NocoDB table
    nocodb_listings_table_id: Optional[str]

    # Notifications
    telegram_token: Optional[str]
    telegram_chat_id: Optional[str]
    smtp_host: Optional[str]
    smtp_port: int
    smtp_user: Optional[str]
    smtp_password: Optional[str]
    email_from: Optional[str]
    email_to: Optional[str]
    smtp_use_tls: bool
    ha_webhook_url: Optional[str]
    max_notify_per_cycle: int

    # Health
    health_path: str
    healthcheck_port: Optional[int]
    health_stale_after_min: int          # 0 = auto (2× poll interval)
    failure_alert_threshold: int
    alert_on_failures: bool

    # MCP endpoint (FastMCP) — lets Claude trigger a run on demand
    mcp_enabled: bool
    mcp_host: str
    mcp_port: int
    mcp_path: str
    mcp_auth_token: Optional[str]

    # Run-logging (Epic E)
    run_log_enabled: bool
    nocodb_runs_table_id: Optional[str]
    nocodb_run_events_table_id: Optional[str]
    run_log_retention_days: int
    run_log_jsonl_path: str
    version: str

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat_id)

    @property
    def email_enabled(self) -> bool:
        return bool(self.smtp_host and self.email_from and self.email_to)

    @property
    def nocodb_enabled(self) -> bool:
        return bool(self.nocodb_url and self.nocodb_token and self.nocodb_table_id)

    @property
    def health_stale_after_s(self) -> int:
        """Seconds with no completed cycle before /health reports unhealthy."""
        minutes = self.health_stale_after_min or (2 * self.poll_interval_min)
        return minutes * 60

    @property
    def searches_from_nocodb(self) -> bool:
        """True when searches should be read live from the NocoDB searches table."""
        return bool(self.nocodb_url and self.nocodb_token and self.nocodb_searches_table_id)

    @property
    def results_enabled(self) -> bool:
        """True when full listing rows should be written to the results table."""
        return bool(self.nocodb_url and self.nocodb_token and self.nocodb_listings_table_id)


def load_config() -> Config:
    """Read, validate, and return the runtime configuration.

    Raises :class:`SystemExit` only for fatal misconfiguration (no sources).
    Non-fatal problems (partially configured channels, inverted bounds) are
    logged as WARNINGs and the offending feature is degraded, not aborted.
    """
    cfg = Config(
        ka_urls=_env_url_list("KA_SEARCH_URLS"),
        rss_urls=_env_url_list("RSS_URLS"),
        criteria=Criteria.from_env(),
        poll_interval_min=max(30, _env_int("POLL_INTERVAL_MIN", 30)),
        user_agent=_env(
            "USER_AGENT",
            "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0",
        ),
        per_request_delay_s=_env_float("PER_REQUEST_DELAY_S") or 2.0,
        request_jitter_s=_env_float("REQUEST_JITTER_S") or 1.5,
        http_timeout_s=_env_float("HTTP_TIMEOUT_S") or 20.0,
        max_retries=_env_int("HTTP_MAX_RETRIES", 3),
        enrich_detail=_env_bool("ENRICH_DETAIL", False),
        ka_max_pages=max(1, _env_int("KA_MAX_PAGES", 20)),
        persist_batch_size=max(1, _env_int("PERSIST_BATCH_SIZE", 25)),
        enrich_concurrency=max(1, _env_int("ENRICH_CONCURRENCY", 4)),
        enrich_delay_s=_env_float("ENRICH_DELAY_S") or 0.5,
        content_dedup_enabled=_env_bool("CONTENT_DEDUP_ENABLED", True),
        recheck_enabled=_env_bool("RECHECK_ENABLED", True),
        recheck_interval_days=max(1, _env_int("RECHECK_INTERVAL_DAYS", 1)),
        json_store_path=_env("JSON_STORE_PATH", "/data/seen.json"),
        nocodb_url=_env("NOCODB_URL"),
        nocodb_token=_env("NOCODB_TOKEN"),
        nocodb_table_id=_env("NOCODB_TABLE_ID"),
        nocodb_id_field=_env("NOCODB_ID_FIELD", "listing_id"),
        nocodb_searches_table_id=_env("NOCODB_SEARCHES_TABLE_ID"),
        nocodb_listings_table_id=_env("NOCODB_LISTINGS_TABLE_ID"),
        telegram_token=_env("TELEGRAM_TOKEN"),
        telegram_chat_id=_env("TELEGRAM_CHAT_ID"),
        smtp_host=_env("SMTP_HOST"),
        smtp_port=_env_int("SMTP_PORT", 587),
        smtp_user=_env("SMTP_USER"),
        smtp_password=_env("SMTP_PASSWORD"),
        email_from=_env("EMAIL_FROM"),
        email_to=_env("EMAIL_TO"),
        smtp_use_tls=_env_bool("SMTP_USE_TLS", True),
        ha_webhook_url=_env("HA_WEBHOOK_URL"),
        max_notify_per_cycle=_env_int("MAX_NOTIFY_PER_CYCLE", 15),
        health_path=_env("HEALTH_PATH", "/data/health.json"),
        healthcheck_port=(int(_env("HEALTHCHECK_PORT")) if _env("HEALTHCHECK_PORT") else None),
        health_stale_after_min=_env_int("HEALTH_STALE_AFTER_MIN", 0),
        failure_alert_threshold=_env_int("FAILURE_ALERT_THRESHOLD", 3),
        alert_on_failures=_env_bool("ALERT_ON_REPEATED_FAILURES", True),
        mcp_enabled=_env_bool("MCP_ENABLED", False),
        mcp_host=_env("MCP_HOST", "0.0.0.0"),
        mcp_port=_env_int("MCP_PORT", 8765),
        mcp_path=_env("MCP_PATH", "/mcp"),
        mcp_auth_token=_env("MCP_AUTH_TOKEN"),
        run_log_enabled=_env_bool("RUN_LOG_ENABLED", True),
        nocodb_runs_table_id=_env("NOCODB_RUNS_TABLE_ID"),
        nocodb_run_events_table_id=_env("NOCODB_RUN_EVENTS_TABLE_ID"),
        run_log_retention_days=_env_int("RUN_LOG_RETENTION_DAYS", 30),
        run_log_jsonl_path=_env("RUN_LOG_JSONL_PATH", "/data/runs.jsonl"),
        version=_env("APP_VERSION", "dev"),
    )
    validate_config(cfg)
    return cfg


def validate_config(cfg: Config) -> None:
    """Validate config (D2). Fatal: no sources. Otherwise warn and degrade."""
    if not cfg.ka_urls and not cfg.rss_urls and not cfg.searches_from_nocodb:
        raise SystemExit(
            "FATAL: no sources configured. Set KA_SEARCH_URLS and/or RSS_URLS, "
            "or NOCODB_SEARCHES_TABLE_ID for a NocoDB-managed search list."
        )

    # Partially configured notification channels: warn and disable just that one.
    if bool(cfg.telegram_token) ^ bool(cfg.telegram_chat_id):
        missing = "TELEGRAM_CHAT_ID" if cfg.telegram_token else "TELEGRAM_TOKEN"
        log.warning(
            "Telegram partially configured (%s missing) — Telegram disabled.", missing
        )
    if cfg.smtp_host and not (cfg.email_from and cfg.email_to):
        log.warning(
            "Email partially configured (EMAIL_FROM/EMAIL_TO missing) — email disabled."
        )

    # Inverted numeric bounds: warn but keep running.
    c = cfg.criteria
    for lo, hi, name in (
        (c.min_rent, c.max_rent, "rent"),
        (c.min_rooms, c.max_rooms, "rooms"),
        (c.min_sqm, c.max_sqm, "sqm"),
    ):
        if lo is not None and hi is not None and lo > hi:
            log.warning("Inverted %s bound: min (%s) > max (%s).", name, lo, hi)

    if not cfg.telegram_enabled and not cfg.email_enabled and not cfg.ha_webhook_url:
        log.warning("No notification channel fully configured — alerts will be logged only.")

    if cfg.run_log_enabled and cfg.nocodb_enabled and not cfg.nocodb_runs_table_id:
        log.warning(
            "RUN_LOG_ENABLED but NOCODB_RUNS_TABLE_ID unset — run-logging falls back to %s.",
            cfg.run_log_jsonl_path,
        )
