# flatwatch

A self-hosted alerting service that polls **Kleinanzeigen** and **RSS** property
feeds, filters listings against your criteria, deduplicates them, and pushes new
matches to **Telegram** and **email** — within one poll cycle (≤ 30 min), with
zero duplicate alerts across restarts. Runs as a single Docker container
(Synology / Portainer friendly).

The full lifecycle of every poll cycle is recorded to **NocoDB** so you can audit
exactly what each run did — which sources answered, what was filtered, what was
new, what notified, and where anything went wrong.

---

## How it works

```
poll loop (app/main.py)
  └─ fetch   sources.py   Kleinanzeigen scraper + RSS parser  (retry/backoff, 403-aware)
  └─ filter  filters.py   criteria match; a None attribute never disqualifies
  └─ dedup   store.py     new only if absent from NocoDB ∪ JSON
  └─ notify  notify.py    Telegram + email + (optional) Home Assistant, independent failure
  └─ persist store.py     write the id to NocoDB and JSON
  └─ log     runlog.py    one run record + ordered phase events → NocoDB (best-effort)
```

- **First startup primes silently** — existing listings are marked seen, nothing
  is sent, so you get no backlog spam.
- **Resilient by design** — one bad cycle never kills the loop (catch-log-continue),
  a 403 is treated as a block (not retried), and run-logging is observational:
  it never blocks or breaks a notification or dedup.

---

## Quick start

```bash
cp .env.example .env       # then edit .env
docker compose up --build -d
docker compose logs -f flatwatch
```

Minimum viable config in `.env`:

```ini
KA_SEARCH_URLS=https://www.kleinanzeigen.de/s-wohnung-mieten/berlin/c203l3331
MAX_RENT=1400
MIN_ROOMS=2
TELEGRAM_TOKEN=123456:abc
TELEGRAM_CHAT_ID=987654
```

At least one of `KA_SEARCH_URLS` / `RSS_URLS` is required; with no notification
channel configured, matches are only logged.

### Multiple search areas (per-source overrides)

Encode each area in its own Kleinanzeigen search URL and list them all — the
area lives in the URL, while the global criteria (`MAX_RENT`, `MIN_ROOMS`, …)
apply to every source:

```ini
KA_SEARCH_URLS=
  https://www.kleinanzeigen.de/s-wohnung-mieten/berlin/c203l3331,
  https://www.kleinanzeigen.de/s-wohnung-mieten/potsdam/c203l3640
```

(Comma- or newline-separated.) This covers per-area searching without per-source
rent overrides; the same `Criteria` is matched against listings from all URLs.

### Detail-page enrichment (optional)

Kleinanzeigen search cards sometimes omit the price, room count, or area. With
`ENRICH_DETAIL=true`, a *new* listing missing any of those has its detail page
fetched once (spaced by `PER_REQUEST_DELAY_S`) to fill the gaps, after which it
is re-checked against the criteria — so a flat that turns out to be over budget
is dropped rather than alerted. Already-seen listings are never re-fetched.
Default is `false` (no detail fetches), keeping scraping minimal.

### Run locally (without Docker)

```bash
pip install -r requirements-dev.txt
python -m app.main
```

---

## Configuration

Every setting is an environment variable; see [`.env.example`](.env.example) for
the annotated list. Highlights:

| Variable | Default | Purpose |
|---|---|---|
| `KA_SEARCH_URLS` / `RSS_URLS` | — | Sources (comma- or newline-separated). |
| `MIN_RENT`/`MAX_RENT`/`MIN_ROOMS`/`MAX_ROOMS`/`MIN_SQM`/`MAX_SQM` | — | Numeric bounds; blank = unconstrained. |
| `REQUIRED_KEYWORDS` / `EXCLUDED_KEYWORDS` | — | Case-insensitive keyword filters. |
| `POLL_INTERVAL_MIN` | `30` | Poll interval; clamped to a 30-minute floor. |
| `HTTP_MAX_RETRIES` | `3` | Retries on 5xx/timeout (2s, 4s, 8s); 403 never retried. |
| `NOCODB_URL` / `NOCODB_TOKEN` / `NOCODB_TABLE_ID` | — | Dedup store (falls back to JSON). |
| `TELEGRAM_TOKEN` / `TELEGRAM_CHAT_ID` | — | Telegram channel (both required). |
| `SMTP_*` / `EMAIL_FROM` / `EMAIL_TO` | — | Email channel. |
| `HA_WEBHOOK_URL` | — | Optional Home Assistant webhook. |
| `MAX_NOTIFY_PER_CYCLE` | `15` | Cap before batching into a summary message. |
| `HEALTHCHECK_PORT` | `8080` | `GET /health` endpoint; unset disables it. |
| `RUN_LOG_ENABLED` | `true` | Toggle run-logging entirely. |
| `NOCODB_RUNS_TABLE_ID` / `NOCODB_RUN_EVENTS_TABLE_ID` | — | Run-log tables. |
| `RUN_LOG_RETENTION_DAYS` | `30` | Prune older runs on startup; `0` disables. |
| `RUN_LOG_JSONL_PATH` | `/data/runs.jsonl` | Offline buffer, replayed when NocoDB returns. |

Config is validated on boot: no sources → exit; a partially configured channel
(e.g. `TELEGRAM_TOKEN` without `TELEGRAM_CHAT_ID`) or an inverted bound → WARNING
and graceful degradation.

---

## NocoDB setup

flatwatch talks to NocoDB's **v2 records API** (`/api/v2/tables/{tableId}/records`)
using an `xc-token`. Create the tables below and paste each table's id into the
matching env var. NocoDB is optional — without it, dedup uses the JSON file and
run-logging buffers to `runs.jsonl`.

### Table: seen listings (dedup) — `NOCODB_TABLE_ID`

| Column | Type | Notes |
|---|---|---|
| `listing_id` | SingleLineText | Stable dedup key (`source:native_id`). Matches `NOCODB_ID_FIELD`. |
| `title` | SingleLineText | Optional, written on notify. |
| `url` | URL / SingleLineText | Optional. |
| `source` | SingleLineText | Optional (`kleinanzeigen` / `rss`). |

On startup flatwatch verifies this table is reachable and that the id field
exists; on failure it logs an actionable WARNING and falls back to JSON.

### Table: `flatwatch_runs` — `NOCODB_RUNS_TABLE_ID`

One row per poll cycle.

| Column | Type |
|---|---|
| `run_id` | SingleLineText (uuid4, primary) |
| `started_at` | SingleLineText / DateTime (ISO8601) |
| `finished_at` | SingleLineText / DateTime |
| `duration_ms` | Number |
| `status` | SingleLineText (`success` / `partial` / `failed`) |
| `trigger` | SingleLineText (`scheduled` / `startup_prime` / `manual`) |
| `sources_polled` | Number |
| `fetched` | Number |
| `filtered` | Number |
| `new` | Number |
| `notified` | Number |
| `errors` | Number |
| `error_summary` | LongText |
| `host` | SingleLineText |
| `version` | SingleLineText |

### Table: `flatwatch_run_events` — `NOCODB_RUN_EVENTS_TABLE_ID`

One row per lifecycle phase within a run.

| Column | Type |
|---|---|
| `event_id` | SingleLineText (uuid4, primary) |
| `run_id` | SingleLineText (FK → `flatwatch_runs.run_id`) |
| `seq` | Number (order within the run) |
| `phase` | SingleLineText (see phases below) |
| `source` | SingleLineText (nullable) |
| `level` | SingleLineText (`info` / `warning` / `error`) |
| `message` | LongText |
| `count` | Number (nullable) |
| `elapsed_ms` | Number (since run start) |
| `at` | SingleLineText / DateTime (ISO8601) |

**Lifecycle phases, in order:** `run_start` → `fetch_start` → `fetch_source` (per
source) → `fetch_done` → `filter` → `dedup` → `notify_start` → `notify_item` (per
listing) → `notify_done` → `persist` → `run_end`. On an unhandled exception a
`run_error` event captures the phase + traceback summary, the run is closed
`failed` (or `partial` if some sources/notifications succeeded), and the loop
continues.

> Run + events are buffered in memory during a cycle and flushed once at the end
> (≤ 1 run insert + 1 batch insert). If NocoDB is unreachable they are appended to
> `runs.jsonl` and replayed on the next successful connection.

### Grafana panel

Point a Grafana NocoDB / REST datasource (or a SQL datasource against NocoDB's
backing database) at these tables:

- **Runs over time** — table/time-series of `flatwatch_runs` by `started_at`,
  colored by `status`; graph `new` and `notified`.
- **Cycle health** — `duration_ms` and `errors` per run.
- **Per-phase drill-down** — filter `flatwatch_run_events` by `run_id`, order by
  `seq`, to replay exactly what a cycle did.

---

## On-demand cycle (manual trigger)

Send `SIGUSR1` to run one cycle immediately instead of waiting for the next
interval — the run is recorded with `trigger=manual`:

```bash
docker kill -s USR1 flatwatch          # or: kill -USR1 <pid>
```

`SIGTERM`/`SIGINT` still finish the current cycle and exit cleanly.

## Health

After every cycle a heartbeat is written to `HEALTH_PATH` (`/data/health.json`)
with the last-cycle stats. When `HEALTHCHECK_PORT` is set, `GET /health` returns
`200` + JSON `{status, last_cycle, new_count, ...}`; the container `HEALTHCHECK`
uses it. Each completed cycle also logs one greppable summary line:

```
cycle_complete sources_polled=2 fetched=37 filtered=9 new=1 notified=1 errors=0 status=success duration_ms=4213
```

---

## Development

```bash
pip install -r requirements-dev.txt
pytest                       # all unit tests, zero network I/O
pytest --cov=app --cov-report=term-missing
```

A saved Kleinanzeigen HTML fixture lives in `tests/fixtures/` so the parser is
tested against real markup shapes. Kleinanzeigen CSS selectors are named
constants at the top of `app/sources.py` — if the markup drifts, patch them there.

---

## Project layout

```
app/
  config.py    env-driven settings + Criteria
  models.py    Listing, German number parsing, stable_id
  sources.py   Kleinanzeigen scraper + RSS parser (retry/backoff, selector constants)
  filters.py   criteria matching (None never disqualifies)
  store.py     NocoDB + JSON union dedup
  notify.py    Telegram + email + Home Assistant, independent failure
  runlog.py    run-lifecycle logging → NocoDB (buffered, best-effort, JSONL fallback)
  health.py    heartbeat file + /health endpoint
  main.py      poll loop, silent prime, signal handling
tests/         pytest suite mirroring app/ (+ HTML fixture)
```
