import json

import pytest

from bluegreen.config import Settings
from bluegreen.report import write_report
from bluegreen.watcher import summarize_watcher

RUN_ID = "20261028-140000-7f3a"


def _read(ts: str, cycle: str | None, ok: bool = True) -> dict:
    return {
        "ts": ts,
        "probe": "raw",
        "cycle_seen": cycle,
        "latency_ms": 200.0,
        "ok": ok,
        "error_code": None,
        "error_msg": None if ok else "boom",
    }


def _ledger_row(step: str, cycle: str, ended_at: str) -> dict:
    return {
        "run_id": RUN_ID,
        "step": step,
        "target": "aura",
        "tenant": "acme",
        "cycle": cycle,
        "ok": True,
        "started_at": ended_at,
        "ended_at": ended_at,
        "duration_s": 1.0,
    }


def _write_run(tmp_path, ledger_rows: list[dict], reads: list[dict]):
    run_dir = tmp_path / "runs" / RUN_ID
    run_dir.mkdir(parents=True)
    (run_dir / "ledger.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in ledger_rows), encoding="utf-8"
    )
    watcher = run_dir / "watcher-aura-acme.jsonl"
    watcher.write_text("".join(json.dumps(row) + "\n" for row in reads), encoding="utf-8")
    return watcher


def test_pause_defaults_to_disabled(monkeypatch):
    monkeypatch.delenv("PAUSE_ITERATIONS", raising=False)
    assert Settings(_env_file=None).pause_iterations == 0
    with pytest.raises(ValueError):
        Settings(_env_file=None, pause_iterations=-1)


def test_summary_gap_and_no_old_after_new(tmp_path):
    reads = [
        _read("2026-10-28T14:34:01.500Z", "c0001"),
        _read("2026-10-28T14:34:02.554Z", "c0001"),
        _read("2026-10-28T14:34:03.768Z", "c0002"),
        _read("2026-10-28T14:34:04.100Z", "c0002"),
    ]
    path = tmp_path / "watcher-aura-acme.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in reads), encoding="utf-8")
    summary = summarize_watcher(
        path, switch_at="2026-10-28T14:34:03.000Z", new_cycle="c0002", old_cycle="c0001"
    )
    assert summary["last_old_cycle_at"] == "2026-10-28T14:34:02.554Z"
    assert summary["first_new_cycle_at"] == "2026-10-28T14:34:03.768Z"
    assert summary["gap_s"] == pytest.approx(1.214)
    assert summary["mixed_reads_after_first_new"] == 0
    assert summary["failed_reads"] == 0


def test_summary_counts_old_reads_after_new(tmp_path):
    reads = [
        _read("2026-10-28T14:00:00.000Z", "c0001"),
        _read("2026-10-28T14:00:01.000Z", "c0002"),
        _read("2026-10-28T14:00:02.000Z", "c0001"),
        _read("2026-10-28T14:00:03.000Z", None, ok=False),
    ]
    path = tmp_path / "w.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in reads), encoding="utf-8")
    summary = summarize_watcher(
        path, switch_at="2026-10-28T14:00:00.500Z", new_cycle="c0002", old_cycle="c0001"
    )
    assert summary["last_old_cycle_at"] == "2026-10-28T14:00:00.000Z"
    assert summary["mixed_reads_after_first_new"] == 1
    assert summary["failed_reads"] == 1


def test_report_uses_the_switch_seen_by_the_watcher(tmp_path):
    ledger_rows = [
        # Bootstrap switch, before the watcher started. It must not be picked.
        _ledger_row("switch", "c0001", "2026-10-28T14:30:00.000Z"),
        _ledger_row("switch", "c0002", "2026-10-28T14:34:03.000Z"),
    ]
    reads = [
        _read("2026-10-28T14:34:01.500Z", "c0001"),
        _read("2026-10-28T14:34:02.554Z", "c0001"),
        _read("2026-10-28T14:34:03.768Z", "c0002"),
    ]
    _write_run(tmp_path, ledger_rows, reads)
    text = write_report(tmp_path, RUN_ID).read_text(encoding="utf-8")
    assert '"old_cycle": "c0001"' in text
    assert '"new_cycle": "c0002"' in text
    assert '"last_old_cycle_at": "2026-10-28T14:34:02.554Z"' in text
    assert '"gap_s": 1.214' in text


def test_report_marks_pause_as_not_run(tmp_path):
    _write_run(tmp_path, [_ledger_row("switch", "c0002", "2026-10-28T14:34:03.000Z")], [])
    text = write_report(tmp_path, RUN_ID).read_text(encoding="utf-8")
    assert "not run (PAUSE_ITERATIONS=0)" in text
    assert "Pause/resume was not run" in text
    assert "billed at a discounted rate" not in text
