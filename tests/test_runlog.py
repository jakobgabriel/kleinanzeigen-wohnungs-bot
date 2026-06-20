"""Tests for run-logging lifecycle, flush, fallback, and status (E1-E4)."""

import json

import requests

from app.runlog import Run, RunEvent, RunLogger, RunRecord
from tests.conftest import make_config


class FakeResp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        return self._payload


class FakeNoco:
    def __init__(self, down=False):
        self.down = down
        self.run_inserts = []
        self.event_batches = []

    def post(self, url, headers=None, data=None, timeout=None):
        if self.down:
            raise requests.ConnectionError("down")
        payload = json.loads(data)
        if "run_events" in url or "events" in url:
            self.event_batches.append(payload)
        else:
            self.run_inserts.append(payload)
        return FakeResp(200, {})

    def get(self, *a, **k):
        return FakeResp(200, {"list": []})

    def delete(self, *a, **k):
        return FakeResp(200, {})


def _runlog_config(tmp_path, **kw):
    return make_config(
        run_log_jsonl_path=str(tmp_path / "runs.jsonl"),
        nocodb_url="https://noco.example",
        nocodb_token="tok",
        nocodb_runs_table_id="runs",
        nocodb_run_events_table_id="events",
        **kw,
    )


# ----- E1: lifecycle instrumentation --------------------------------------- #
def test_run_start_creates_record_and_event(tmp_path):
    rl = RunLogger(make_config(), session=FakeNoco())
    run = rl.start(trigger="scheduled")
    assert run.record.run_id
    assert run.record.started_at
    assert run.events[0].phase == "run_start"
    assert run.events[0].seq == 1


def test_events_have_monotonic_seq(tmp_path):
    rl = RunLogger(make_config(), session=FakeNoco())
    run = rl.start()
    run.event("fetch_start")
    run.event("fetch_done", count=3)
    seqs = [e.seq for e in run.events]
    assert seqs == list(range(1, len(seqs) + 1))
    assert all(e.elapsed_ms >= 0 for e in run.events)


def test_finish_populates_totals(tmp_path):
    rl = RunLogger(_runlog_config(tmp_path), session=FakeNoco())
    run = rl.start()
    rec = run.finish(sources_polled=2, fetched=10, filtered=5, new=2, notified=2)
    assert rec.finished_at and rec.duration_ms is not None
    assert (rec.sources_polled, rec.fetched, rec.filtered, rec.new, rec.notified) == (2, 10, 5, 2, 2)
    assert run.events[-1].phase == "run_end"


# ----- E2: flush + fallback ------------------------------------------------ #
def test_flush_to_nocodb(tmp_path):
    noco = FakeNoco()
    rl = RunLogger(_runlog_config(tmp_path), session=noco)
    run = rl.start()
    run.event("filter", count=1)
    run.finish(sources_polled=1, fetched=1, filtered=1, new=0, notified=0)
    assert len(noco.run_inserts) == 1
    assert len(noco.event_batches) == 1  # single batch insert
    assert len(noco.event_batches[0]) == len(run.events)


def test_flush_falls_back_to_jsonl_when_nocodb_down(tmp_path):
    noco = FakeNoco(down=True)
    cfg = _runlog_config(tmp_path)
    rl = RunLogger(cfg, session=noco)
    run = rl.start()
    run.finish(sources_polled=1, fetched=0, filtered=0, new=0, notified=0)
    lines = open(cfg.run_log_jsonl_path).read().splitlines()
    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert "run" in obj and "events" in obj


def test_replay_backlog_then_truncate(tmp_path):
    cfg = _runlog_config(tmp_path)
    # First run with NocoDB down -> buffered to jsonl.
    rl_down = RunLogger(cfg, session=FakeNoco(down=True))
    rl_down.start().finish(sources_polled=1)
    assert open(cfg.run_log_jsonl_path).read().strip()
    # Next run with NocoDB up -> replays backlog and truncates the file.
    noco = FakeNoco()
    rl_up = RunLogger(cfg, session=noco)
    rl_up.start().finish(sources_polled=1)
    import os
    assert not os.path.exists(cfg.run_log_jsonl_path)
    assert len(noco.run_inserts) == 2  # replayed run + current run


def test_run_logging_disabled_writes_nothing(tmp_path):
    noco = FakeNoco()
    cfg = _runlog_config(tmp_path, run_log_enabled=False)
    rl = RunLogger(cfg, session=noco)
    rl.start().finish(sources_polled=1)
    assert noco.run_inserts == []
    import os
    assert not os.path.exists(cfg.run_log_jsonl_path)


def test_flush_never_raises(tmp_path, caplog):
    class Boom(FakeNoco):
        def post(self, *a, **k):
            raise RuntimeError("kaboom")

    cfg = _runlog_config(tmp_path)
    rl = RunLogger(cfg, session=Boom())
    # Should swallow and fall back; must not raise.
    rl.start().finish(sources_polled=1)


# ----- E3: status classification ------------------------------------------- #
def test_status_success(tmp_path):
    rl = RunLogger(_runlog_config(tmp_path), session=FakeNoco())
    rec = rl.start().finish(sources_polled=2)
    assert rec.status == "success"


def test_status_partial_when_some_sources_fail(tmp_path):
    rl = RunLogger(_runlog_config(tmp_path), session=FakeNoco())
    run = rl.start()
    run.source_failed("http://a", "403", blocked=True)
    rec = run.finish(sources_polled=2)
    assert rec.status == "partial"
    assert "http://a" in rec.error_summary


def test_status_failed_when_all_sources_fail(tmp_path):
    rl = RunLogger(_runlog_config(tmp_path), session=FakeNoco())
    run = rl.start()
    run.source_failed("http://a", "boom")
    run.source_failed("http://b", "boom")
    rec = run.finish(sources_polled=2)
    assert rec.status == "failed"


def test_capture_error_sets_event(tmp_path):
    rl = RunLogger(_runlog_config(tmp_path), session=FakeNoco())
    run = rl.start()
    try:
        raise ValueError("explode")
    except ValueError as exc:
        run.capture_error("run_cycle", exc)
    rec = run.finish(status="failed", sources_polled=1)
    assert rec.status == "failed"
    assert any(e.phase == "run_error" and "explode" in e.message for e in run.events)


# ----- E4: pruning --------------------------------------------------------- #
def test_prune_disabled_when_retention_zero(tmp_path):
    noco = FakeNoco()
    rl = RunLogger(_runlog_config(tmp_path, run_log_retention_days=0), session=noco)
    rl.prune()  # no-op, must not raise


def test_trigger_recorded(tmp_path):
    rl = RunLogger(_runlog_config(tmp_path), session=FakeNoco())
    run = rl.start(trigger="startup_prime")
    assert run.record.trigger == "startup_prime"
