"""Client-side probes during a cutover."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from neo4j import RoutingControl
from neo4j.exceptions import Neo4jError

from bluegreen.redact import redact
from bluegreen.util import utc_now

_QUERY = "MATCH (s:Snapshot) RETURN s.cycle AS c"


class Watcher:
    def __init__(self, driver, resolve, path: Path):
        self.driver = driver
        self.resolve = resolve
        self.path = path
        self.interval_s = 0.25
        self.probes = ("managed", "raw")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._run, name=f"watcher-{self.path.name}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)

    def _run(self) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            while not self._stop.is_set():
                for probe in self.probes:
                    handle.write(json.dumps(self._probe(probe), sort_keys=True) + "\n")
                    handle.flush()
                self._stop.wait(self.interval_s)

    def _probe(self, probe: str) -> dict:
        started = time.perf_counter()
        cycle_seen = None
        ok = False
        error_code = None
        error_msg = None
        try:
            database = self.resolve()
            if probe == "managed":
                result = self.driver.execute_query(
                    _QUERY,
                    database_=database,
                    routing_=RoutingControl.READ,
                )
                records = list(result.records)
            else:
                with self.driver.session(database=database) as session:
                    records = list(session.run(_QUERY))
            if records:
                cycle_seen = records[0]["c"]
                ok = True
            else:
                error_msg = "no Snapshot"
        except Neo4jError as exc:
            error_code = getattr(exc, "gql_status", None) or getattr(exc, "code", None)
            error_code = str(error_code) if error_code else None
            error_msg = redact(str(exc))[:500]
        except Exception as exc:
            error_msg = redact(f"{type(exc).__name__}: {exc}")[:500]
        return {
            "ts": utc_now(),
            "probe": probe,
            "cycle_seen": cycle_seen,
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            "ok": ok,
            "error_code": error_code,
            "error_msg": error_msg,
        }


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((pct / 100) * (len(ordered) - 1)))))
    return round(ordered[index], 3)


def summarize_watcher(
    path: Path,
    *,
    switch_at: str | None,
    new_cycle: str | None,
    old_cycle: str | None,
) -> dict:
    if not path.exists():
        return {"file": str(path), "rows": 0}
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    first_new = None
    last_old = None
    mixed = 0
    seen_new = False
    raw_errors: list[dict] = []
    before: list[float] = []
    after: list[float] = []
    for row in rows:
        cycle = row.get("cycle_seen")
        if new_cycle and cycle == new_cycle and first_new is None:
            first_new = row["ts"]
            seen_new = True
        if old_cycle and cycle == old_cycle and not seen_new:
            last_old = row["ts"]
        if seen_new and old_cycle and cycle == old_cycle:
            mixed += 1
        if switch_at and row.get("probe") == "raw" and not row.get("ok"):
            if _within(row["ts"], switch_at, 10):
                raw_errors.append({"ts": row["ts"], "error_code": row.get("error_code"), "error_msg": row.get("error_msg")})
        if switch_at and row.get("ok"):
            if row["ts"] < switch_at:
                before.append(float(row["latency_ms"]))
            elif row["ts"] > switch_at:
                after.append(float(row["latency_ms"]))
    gap_s = None
    if first_new and last_old:
        gap_s = round(_seconds_between(last_old, first_new), 3)
    return {
        "file": path.name,
        "rows": len(rows),
        "failed_reads": sum(1 for row in rows if not row.get("ok")),
        "old_cycle": old_cycle,
        "new_cycle": new_cycle,
        "last_old_cycle_at": last_old,
        "first_new_cycle_at": first_new,
        "gap_s": gap_s,
        "mixed_reads_after_first_new": mixed,
        "raw_errors_around_switch": len(raw_errors),
        "raw_error_codes": sorted({item["error_code"] or "" for item in raw_errors}),
        "managed_errors": sum(1 for row in rows if row.get("probe") == "managed" and not row.get("ok")),
        "latency_before_ms": {"p50": _percentile(before, 50), "p99": _percentile(before, 99)},
        "latency_after_ms": {"p50": _percentile(after, 50), "p99": _percentile(after, 99)},
    }


def _seconds_between(start: str, end: str) -> float:
    from datetime import datetime

    left = datetime.fromisoformat(start.replace("Z", "+00:00"))
    right = datetime.fromisoformat(end.replace("Z", "+00:00"))
    return (right - left).total_seconds()


def _within(ts: str, center: str, seconds: float) -> bool:
    return abs(_seconds_between(center, ts)) <= seconds
