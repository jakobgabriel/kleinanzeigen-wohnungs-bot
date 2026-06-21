# flatwatch — Container & Operations Guide

Everything needed to build, configure, deploy, observe, and operate the
flatwatch container. For a feature overview see the [README](../README.md);
this document is the full reference for running it in production.

## Contents

- [1. What the container is](#1-what-the-container-is)
- [2. The image](#2-the-image)
- [3. Running it](#3-running-it)
  - [3.1 docker compose](#31-docker-compose-recommended)
  - [3.2 docker run](#32-docker-run)
  - [3.3 Synology / Portainer](#33-synology--portainer)
- [4. Persisted data & volumes](#4-persisted-data--volumes)
- [5. Environment variable reference](#5-environment-variable-reference)
- [6. NocoDB tables](#6-nocodb-tables)
- [7. Networking & the health endpoint](#7-networking--the-health-endpoint)
- [8. Lifecycle & signals](#8-lifecycle--signals)
- [9. Observability](#9-observability)
- [10. Operations](#10-operations)
- [11. Troubleshooting](#11-troubleshooting)
- [12. Security](#12-security)
- [13. Resource footprint](#13-resource-footprint)

---

## 1. What the container is

A single, long-running Python 3.12 process (`python -m app.main`) that loops
forever: every `POLL_INTERVAL_MIN` minutes it fetches each configured search,
filters listings against your criteria, deduplicates against
`NocoDB ∪ seen.json`, notifies new matches over Telegram/email/Home-Assistant,
records the full run to NocoDB, and writes a heartbeat. It needs no sidecars and
no database of its own — NocoDB (if used) is external, and the only local state
is a small `/data` volume.

```
┌─ flatwatch container ───────────────────────────────────────┐
│  python -m app.main  (poll loop, PID 1)                      │
│    fetch → filter → dedup → enrich → notify → persist → log  │
│  health HTTP server (optional, :8080)                        │
│                                                              │
│  /data  ── seen.json · health.json · runs.jsonl  (volume)    │
└──────────────────────────────────────────────────────────────┘
        │ outbound HTTPS                    │ outbound
        ▼                                   ▼
  Kleinanzeigen / RSS              NocoDB · Telegram · SMTP · HA
```

There is exactly one process; if it exits, the container exits, and Docker's
`restart: unless-stopped` brings it back. The loop itself never dies on a bad
cycle — errors are caught, logged, recorded, and the loop continues.

---

## 2. The image

| Property | Value |
|---|---|
| Base | `python:3.12-slim` |
| Architectures | `linux/amd64`, `linux/arm64` (Synology ARM NAS supported) |
| Entry point | `python -m app.main` |
| Working dir | `/app` |
| Runtime deps | `requests`, `beautifulsoup4`, `feedparser` (see `requirements.txt`) |
| Exposed port | `8080` (health endpoint, optional) |
| Declared volume | `/data` |
| Healthcheck | built-in `curl` against `/health` every 5 min |

`build-essential` is installed only to compile feedparser's `sgmllib3k` sdist and
is purged in the same layer, keeping the image small.

### Build

```bash
# Local single-arch build, stamping the git sha into run records:
docker build -t flatwatch:latest --build-arg APP_VERSION=$(git rev-parse --short HEAD) .

# Multi-arch build + push (amd64 + arm64):
docker buildx build --platform linux/amd64,linux/arm64 \
  --build-arg APP_VERSION=$(git rev-parse --short HEAD) \
  -t youruser/flatwatch:latest --push .
```

`APP_VERSION` flows into the `version` column of every `flatwatch_runs` row, so
you can tell which build produced a given run. CI builds both architectures on
every push (`.github/workflows/ci.yml`).

---

## 3. Running it

### 3.1 docker compose (recommended)

```bash
cp .env.example .env      # fill in sources, criteria, channels
docker compose up --build -d
docker compose logs -f flatwatch
```

The bundled `docker-compose.yml` sets a named `flatwatch-data` volume, the health
port, and `restart: unless-stopped`. It has **no `env_file`** — config is mapped
via `${VAR}` interpolation, and Compose auto-loads a local `.env` for that, so a
local `.env` is picked up but never required. Override the build version with
`APP_VERSION=$(git rev-parse --short HEAD) docker compose build`. The same file is
used for Portainer (§3.3).

### 3.2 docker run

```bash
docker run -d --name flatwatch --restart unless-stopped \
  --env-file .env \
  -e HEALTHCHECK_PORT=8080 \
  -p 8080:8080 \
  -v flatwatch-data:/data \
  flatwatch:latest
```

### 3.3 Synology / Portainer (Git repository stack)

No image is published, so the simplest path is a **Repository stack**: Portainer
clones this repo and builds the image on the NAS. `docker-compose.yml` has no
`env_file` and maps the full config surface via `${VAR}` interpolation, so the
Portainer *Environment variables* panel is what configures the container.

1. **Stacks → Add stack**, name it `flatwatch`. Build method: **Repository**.
2. Fill in the repository:
   - **Repository URL:** `https://github.com/jakobgabriel/kleinanzeigen-wohnungs-bot`
   - **Repository reference:** `refs/heads/main`
   - **Compose path:** `docker-compose.yml` (or `docker-compose.portainer.yml` —
     they're identical, so existing stacks pointing at either keep working)
   - *Authentication:* only needed if the repo is private (use a GitHub PAT).
3. **Environment variables → Advanced mode:** paste your filled-in
   `.env.example` contents (`KEY=VALUE` lines). Set at least one source and one
   notification channel — plus the NocoDB ids if you use it. Blank/omitted vars
   fall back to the app defaults.
4. **Deploy the stack.** The first build compiles dependencies (a couple of
   minutes), then it's cached. An ARM NAS needs nothing special — the
   `python:3.12-slim` base is multi-arch and builds for your CPU automatically.
5. **Verify** in *Logs*: `flatwatch starting: …` then, after the first cycle,
   `cycle_complete … status=success`. Hit `http://<nas-ip>:8080/health` → `200`
   with JSON; Portainer shows the container `healthy`.
6. **Updating:** open the stack → **Pull and redeploy** (re-clones + rebuilds), or
   enable Portainer's GitOps auto-update (polling or webhook) to redeploy on new
   commits. The `flatwatch-data` volume persists across redeploys, so you won't
   get duplicate alerts.

> The first cycle **primes silently** — it records existing listings as seen
> without notifying — so you will not get a backlog of alerts on first boot.

**Troubleshooting:** if the container exits with `FATAL: no sources configured`,
the panel variables didn't reach the container — make sure they're set under
*Environment variables* (Advanced mode) and redeploy.

---

## 4. Persisted data & volumes

Everything that must survive a restart lives under the `/data` volume. Mount it;
without it, dedup state is lost on recreation and you may get duplicate alerts.

| Path | Written by | Purpose | Safe to delete? |
|---|---|---|---|
| `/data/seen.json` | dedup store | JSON fallback set of notified listing ids | Deleting re-primes silently next boot (no re-spam), but loses history. |
| `/data/health.json` | heartbeat | Last-cycle stats snapshot | Yes — recreated each cycle. |
| `/data/runs.jsonl` | run-logging | Offline buffer of runs when NocoDB is down | Yes — but you lose un-replayed run logs. |

Paths are configurable (`JSON_STORE_PATH`, `HEALTH_PATH`, `RUN_LOG_JSONL_PATH`)
but default under `/data`. Each file is written atomically (`*.tmp` + `os.replace`)
so a crash mid-write cannot corrupt it.

**Backup:** copy `seen.json` (the only stateful file that matters). When NocoDB
is the dedup store, the durable record is in NocoDB and `seen.json` is just a
local mirror.

---

## 5. Environment variable reference

All configuration is via environment variables (12-factor). Annotated in
[`.env.example`](../.env.example); the complete reference follows.

### Sources

| Variable | Default | Notes |
|---|---|---|
| `KA_SEARCH_URLS` | — | Kleinanzeigen search-result URLs, comma- or newline-separated. |
| `RSS_URLS` | — | RSS/Atom feed URLs, comma- or newline-separated. |

At least one source is required — either an env URL above **or** a configured
`NOCODB_SEARCHES_TABLE_ID`. With none of them, the container exits at boot with a
clear FATAL message.

### Criteria (all optional; a blank bound is not applied)

| Variable | Default | Notes |
|---|---|---|
| `MIN_RENT` / `MAX_RENT` | — | Kaltmiete EUR bounds. |
| `MIN_ROOMS` / `MAX_ROOMS` | — | Room-count bounds (accepts `2,5`). |
| `MIN_SQM` / `MAX_SQM` | — | Living-area bounds. |
| `REQUIRED_KEYWORDS` | — | Comma-separated; **all** must appear in the listing text. |
| `EXCLUDED_KEYWORDS` | — | Comma-separated; **any** match rejects the listing. |

A listing whose attribute can't be parsed (`None`) is **never** disqualified by a
numeric bound — the filter biases toward false positives over missed flats.

### Polling & politeness

| Variable | Default | Notes |
|---|---|---|
| `POLL_INTERVAL_MIN` | `30` | Minutes between cycles; **clamped up to a 30-min floor**. |
| `USER_AGENT` | a Firefox UA | Sent on every request. |
| `PER_REQUEST_DELAY_S` | `2.0` | Base delay between requests within a cycle. |
| `REQUEST_JITTER_S` | `1.5` | Random 0–N seconds added to each delay. |
| `HTTP_TIMEOUT_S` | `20` | Per-request timeout. |
| `HTTP_MAX_RETRIES` | `3` | Retries on 5xx/timeout (2s, 4s, 8s backoff); **403 is never retried**. |
| `KA_MAX_PAGES` | `20` | Walk Kleinanzeigen result pages (`seite:2,3,…`) up to this many, stopping early at the first empty or repeated page. `1` = first page only. |
| `ENRICH_DETAIL` | `false` | Fetch KA detail pages to fill missing price/rooms/sqm, then re-filter. |

### Dedup store

| Variable | Default | Notes |
|---|---|---|
| `JSON_STORE_PATH` | `/data/seen.json` | JSON fallback dedup file. |
| `NOCODB_URL` | — | NocoDB base URL, e.g. `https://nocodb.mynas.local`. |
| `NOCODB_TOKEN` | — | `xc-token` API token. |
| `NOCODB_TABLE_ID` | — | Table id of the seen-listings table. |
| `NOCODB_ID_FIELD` | `listing_id` | Column holding the dedup key. |

NocoDB is optional: without it, dedup uses `seen.json` only. With it, a listing
is new only if absent from **both**; NocoDB failures degrade to JSON, logged,
non-fatal.

### Dynamic searches (optional)

| Variable | Default | Notes |
|---|---|---|
| `NOCODB_SEARCHES_TABLE_ID` | — | If set, searches (URL + per-search criteria) are read from NocoDB at the start of every cycle. Falls back to env URLs + global criteria. |

### Results table (optional)

| Variable | Default | Notes |
|---|---|---|
| `NOCODB_LISTINGS_TABLE_ID` | — | If set, a full row per matching listing (price/rooms/sqm/location/description/first_seen) is written to this NocoDB table. Browse everything in NocoDB even with notifications off; a fresh deploy captures the current backlog. Best-effort, never blocks a cycle. |

### Notifications (each channel independent; partial config disables just it)

| Variable | Default | Notes |
|---|---|---|
| `TELEGRAM_TOKEN` | — | Bot token. Both token + chat id required to enable. |
| `TELEGRAM_CHAT_ID` | — | Target chat id. |
| `SMTP_HOST` | — | Enables email together with `EMAIL_FROM`/`EMAIL_TO`. |
| `SMTP_PORT` | `587` | |
| `SMTP_USER` / `SMTP_PASSWORD` | — | Optional auth. |
| `EMAIL_FROM` / `EMAIL_TO` | — | Required for email. |
| `SMTP_USE_TLS` | `true` | STARTTLS. |
| `HA_WEBHOOK_URL` | — | Optional Home Assistant webhook; receives a JSON POST per listing. |
| `MAX_NOTIFY_PER_CYCLE` | `15` | Above this, send N individually + one summary message (still marks all seen). |

### Health

| Variable | Default | Notes |
|---|---|---|
| `HEALTH_PATH` | `/data/health.json` | Heartbeat file path. |
| `HEALTHCHECK_PORT` | — (compose sets `8080`) | When set, serves `GET /health`. Unset disables the HTTP server (file still written). |
| `HEALTH_STALE_AFTER_MIN` | `0` (auto = 2× poll interval) | `/health` returns 503 if no cycle completes within this window. |
| `FAILURE_ALERT_THRESHOLD` | `3` | Consecutive failed cycles before `/health` is 503 and an alert is sent. |
| `ALERT_ON_REPEATED_FAILURES` | `true` | Send a Telegram/email alert on a sustained outage and on recovery. |

### Run-logging

| Variable | Default | Notes |
|---|---|---|
| `RUN_LOG_ENABLED` | `true` | Master toggle; `false` requires no run tables. |
| `NOCODB_RUNS_TABLE_ID` | — | `flatwatch_runs` table id. |
| `NOCODB_RUN_EVENTS_TABLE_ID` | — | `flatwatch_run_events` table id. |
| `RUN_LOG_RETENTION_DAYS` | `30` | Prune runs older than this on startup; `0` disables pruning. |
| `RUN_LOG_JSONL_PATH` | `/data/runs.jsonl` | Offline buffer, replayed when NocoDB returns. |

### Misc

| Variable | Default | Notes |
|---|---|---|
| `APP_VERSION` | `dev` | Image/git provenance, stored on each run record (usually set via build arg). |

### Boot validation (what's fatal vs. degraded)

| Condition | Behavior |
|---|---|
| No source (no env URLs and no searches table) | **FATAL** — exit, with message. |
| Telegram token without chat id (or vice-versa) | WARNING — Telegram disabled. |
| `SMTP_HOST` without `EMAIL_FROM`/`EMAIL_TO` | WARNING — email disabled. |
| Inverted bound (e.g. `MIN_RENT > MAX_RENT`) | WARNING — keeps running. |
| No channel fully configured | WARNING — matches are logged only. |

---

## 6. NocoDB tables

flatwatch uses NocoDB's **v2 records API** (`/api/v2/tables/{tableId}/records`)
with an `xc-token`. Up to four tables, all optional. Exact columns/types are
documented in the [README](../README.md#nocodb-setup):

| Table | Env var | Role |
|---|---|---|
| seen listings | `NOCODB_TABLE_ID` | Dedup store (primary; JSON is the fallback). |
| `flatwatch_searches` | `NOCODB_SEARCHES_TABLE_ID` | Live search config (URL + per-search criteria). |
| `flatwatch_listings` | `NOCODB_LISTINGS_TABLE_ID` | Full row per matching listing (results view). |
| `flatwatch_runs` | `NOCODB_RUNS_TABLE_ID` | One row per cycle. |
| `flatwatch_run_events` | `NOCODB_RUN_EVENTS_TABLE_ID` | One row per lifecycle phase. |

On startup the dedup and searches tables are reachability-checked; failures log a
clear WARNING and fall back (JSON / env) rather than aborting.

---

## 7. Networking & the health endpoint

**Outbound** (required): HTTPS to Kleinanzeigen and your RSS hosts; plus, if
configured, NocoDB, `api.telegram.org`, your SMTP server, and the HA webhook. No
inbound connectivity is needed for the core function.

**Inbound** (optional): when `HEALTHCHECK_PORT` is set the container serves:

```
GET /health   →   200 (healthy) | 503 (alive but failing) application/json
{
  "status": "success",          // success | partial | failed | starting
  "healthy": true,              // false → HTTP 503
  "last_cycle": "2026-06-20T20:29:24.475911+00:00",
  "last_success_at": "2026-06-20T20:29:24.475911+00:00",
  "consecutive_failures": 0,
  "duration_ms": 6,
  "sources_polled": 2,
  "fetched": 37,
  "filtered": 9,
  "new_count": 1,
  "notified": 1,
  "errors": 0
}
```

The endpoint returns **503** (not just 200) when the service is *alive but
failing* — either no cycle has completed within `HEALTH_STALE_AFTER_MIN` (a wedged
or stuck loop) or `consecutive_failures` has reached `FAILURE_ALERT_THRESHOLD`
(e.g. selectors drifted, all sources blocked). This is what makes Docker's
HEALTHCHECK and external monitors able to catch a silently-failing container,
rather than seeing a permanent green. On crossing the failure threshold the
service also sends one alert (and a recovery note) if
`ALERT_ON_REPEATED_FAILURES` is on.

`GET /health`, `/healthz`, and `/` all return the snapshot; anything else is 404.
The container's `HEALTHCHECK` curls this every 5 minutes, so `docker ps` shows
`healthy`/`unhealthy`. Map the port (`-p 8080:8080`) only if you want to scrape
it from the host or a monitor.

---

## 8. Lifecycle & signals

| Signal | Effect |
|---|---|
| `SIGTERM` / `SIGINT` | Graceful stop: finish the current cycle, then exit 0. (Sent by `docker stop`.) |
| `SIGUSR1` | Run one cycle **immediately**, recorded as `trigger=manual`. |

```bash
docker kill -s USR1 flatwatch     # trigger an on-demand poll now
docker stop flatwatch             # graceful shutdown (SIGTERM)
```

The loop sleeps in 1-second slices, so shutdown and manual triggers are honored
within ~1s rather than waiting out the full interval.

**Run triggers** recorded in `flatwatch_runs.trigger`: `startup_prime` (the first
silent run), `scheduled` (normal interval runs), `manual` (SIGUSR1).

---

## 9. Observability

Three complementary layers:

1. **Per-cycle log line** (A4) — one greppable summary, ideal for Loki/Grafana:
   ```
   cycle_complete sources_polled=2 fetched=37 filtered=9 new=1 notified=1 errors=0 status=success duration_ms=4213
   ```
2. **Heartbeat** (`health.json` + `/health`) — the latest cycle's stats at a glance.
3. **Run-logging to NocoDB** — the full lifecycle of every cycle: one
   `flatwatch_runs` row plus ordered `flatwatch_run_events` (`run_start` →
   `fetch_source*` → `filter` → `dedup` → `notify_item*` → `persist` → `run_end`,
   or a `run_error` on failure). Buffered in memory and flushed once per cycle
   (≤ 1 run insert + 1 batch insert); if NocoDB is down it's appended to
   `runs.jsonl` and replayed later. Point a Grafana panel at these tables (see
   the [README](../README.md#grafana-panel)).

Logs go to stdout/stderr at INFO; view with `docker compose logs -f flatwatch`.

---

## 10. Operations

**First run** primes silently — no alerts, all current listings marked seen.

**Updating the image:**
```bash
docker compose pull   # or rebuild: docker compose build --build-arg APP_VERSION=$(git rev-parse --short HEAD)
docker compose up -d
```
The `/data` volume (and NocoDB) persist, so no duplicate alerts after an update.

**Changing searches/criteria:**
- *Env mode:* edit `.env`, then `docker compose up -d` to recreate the container.
- *NocoDB mode:* edit the `flatwatch_searches` rows — picked up on the **next
  cycle, no restart**. Use `docker kill -s USR1 flatwatch` to apply immediately.

**Run-log retention:** old runs are pruned on startup per `RUN_LOG_RETENTION_DAYS`
(default 30; `0` disables). Pruning failure is non-fatal.

**Reset dedup history:** stop the container, delete `seen.json` (and clear the
NocoDB seen table if used), restart. The next run re-primes silently.

---

## 11. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Container exits immediately, `FATAL: no sources configured` | Set `KA_SEARCH_URLS`/`RSS_URLS` or `NOCODB_SEARCHES_TABLE_ID`. |
| Log: `0 cards parsed — selectors may be stale` | Kleinanzeigen markup changed. Patch the `KA_*_SELECTOR` constants at the top of `app/sources.py`; the saved fixture under `tests/fixtures/` helps. |
| Log: `Source blocked (403)` | You're rate-limited/blocked. Increase `POLL_INTERVAL_MIN`/delays; 403 is intentionally not retried. RSS is the durable backbone. |
| No notifications, but `new=N` in logs | No channel fully configured (matches are logged only), or a channel failed — check the `notify_item` events / logs. |
| Duplicate alerts after restart | `/data` not persisted — mount the volume. |
| Log: `NocoDB unreachable, degrading to JSON` | NocoDB down/misconfigured; dedup continues on `seen.json`. Verify `NOCODB_URL`/`NOCODB_TOKEN`/table ids. |
| Searches edits not taking effect | Only applies in NocoDB mode; confirm `NOCODB_SEARCHES_TABLE_ID` is set and rows have `enabled` checked. Wait one cycle or send `SIGUSR1`. |
| `docker ps` shows `unhealthy` | `HEALTHCHECK_PORT` not set, or the process is stuck — check logs. |
| Email fails with TLS errors | Try `SMTP_USE_TLS=false` for plain/465 setups, or correct `SMTP_PORT`. |

---

## 12. Security

- **No secrets in the image.** All credentials come from env/`.env` at runtime;
  keep `.env` out of git (it's gitignored).
- **Tokens:** the NocoDB `xc-token`, Telegram bot token, and SMTP password are
  the sensitive values — scope the NocoDB token to just the flatwatch tables.
- **Least privilege:** the container needs only outbound network; do not publish
  the health port to the internet (bind it to the LAN or leave it unmapped).
- **Polite scraping** is enforced (≥ 30-min floor, jitter, realistic UA) — keep
  it that way to avoid blocks and to stay a good citizen.

---

## 13. Resource footprint

A single lightweight Python process: typically tens of MB of RAM and negligible
CPU, spiking only briefly each cycle during fetch/parse. Disk usage is a few KB
of JSON under `/data` (run-log backlog grows only while NocoDB is unreachable).
Suitable for a low-power NAS. The work is I/O-bound, not CPU-bound; one container
handles many searches comfortably within the poll interval.
