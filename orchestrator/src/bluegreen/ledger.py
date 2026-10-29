"""Append-only timings ledger for one run."""

from __future__ import annotations

import json
import secrets
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from bluegreen.errors import AppError
from bluegreen.redact import redact
from bluegreen.util import utc_now


def new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(2)}"


class Ledger:
    def __init__(self, work: Path, run_id: str):
        self.work = work
        self.run_id = run_id
        self.run_dir = work / "runs" / run_id
        self.log_dir = self.run_dir / "logs"
        self.path = self.run_dir / "ledger.jsonl"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def step(
        self,
        step: str,
        *,
        target: str,
        tenant: str | None = None,
        cycle: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        details: dict[str, Any] = {}
        started = utc_now()
        t0 = time.perf_counter()
        try:
            yield details
        except Exception as exc:
            self.write(
                step=step,
                target=target,
                tenant=tenant,
                cycle=cycle,
                started_at=started,
                duration_s=time.perf_counter() - t0,
                ok=False,
                details=details,
                error=exc,
            )
            raise
        else:
            self.write(
                step=step,
                target=target,
                tenant=tenant,
                cycle=cycle,
                started_at=started,
                duration_s=time.perf_counter() - t0,
                ok=True,
                details=details,
            )

    def write(
        self,
        *,
        step: str,
        target: str,
        tenant: str | None,
        cycle: str | None,
        started_at: str,
        duration_s: float,
        ok: bool,
        details: dict[str, Any] | None = None,
        error: BaseException | None = None,
    ) -> None:
        ended = utc_now()
        row: dict[str, Any] = {
            "run_id": self.run_id,
            "target": target,
            "tenant": tenant,
            "cycle": cycle,
            "step": step,
            "started_at": started_at,
            "ended_at": ended,
            "duration_s": round(duration_s, 3),
            "ok": ok,
            "details": details or {},
        }
        if error is not None:
            row["error_type"] = type(error).__name__
            code = getattr(error, "error_code", None)
            row["error_code"] = code
            row["error_msg"] = redact(str(error))[:2000]
        line = redact(json.dumps(row, sort_keys=True, default=str))
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows


def open_ledger(work: Path, run_id: str | None) -> Ledger:
    return Ledger(work, run_id or new_run_id())
