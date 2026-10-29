"""Build work/runs/<run_id>/report.md from the ledger and saved evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bluegreen.state import load_aura
from bluegreen.watcher import summarize_watcher

_STEP_COLUMNS = (
    "generate",
    "import",
    "dump",
    "db_create",
    "db_create_api",
    "db_ready",
    "upload",
    "upload_online",
    "schema",
    "verify",
    "switch",
    "db_drop",
    "db_delete_api",
)

_QUESTIONS = {
    "Q1": "Does `neo4j-admin database upload --to-dbid` load a dump into one specific database on Aura?",
    "Q2": "Can a database alias switch a tenant to a new database on Aura without client changes?",
    "Q3": "Does the same refresh work on self-managed Enterprise Edition?",
    "Q4": "Which Aura API calls and payloads work?",
}


def write_report(work: Path, run_id: str) -> Path:
    run_dir = work / "runs" / run_id
    ledger_path = run_dir / "ledger.jsonl"
    rows = _read_ledger(ledger_path)
    aura = load_aura(work) or {}
    public_aura = {key: value for key, value in aura.items() if key != "password"}
    lines: list[str] = []
    lines.append(f"# Blue/green run {run_id}")
    lines.append("")
    lines.append("## 1. Run summary")
    lines.append("")
    lines.append(f"- Run ID: `{run_id}`")
    versions = work / "doctor" / "versions.txt"
    if versions.exists():
        lines.append("- Versions:")
        lines.append("")
        lines.append("```")
        lines.append(versions.read_text(encoding="utf-8").rstrip())
        lines.append("```")
    if public_aura:
        lines.append("- Aura instance (secrets removed):")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(_summary_aura(public_aura), indent=2, sort_keys=True, default=str))
        lines.append("```")
    tenants = sorted({row["tenant"] for row in rows if row.get("tenant")})
    lines.append(f"- Tenants seen in the ledger: {', '.join(tenants) if tenants else '(none)'}")
    lines.append("")
    lines.append("## 2. Answers")
    lines.append("")
    lines.append("| ID | Question | Verdict | Evidence |")
    lines.append("| --- | --- | --- | --- |")
    for qid, verdict, evidence in _answers(rows):
        lines.append(f"| {qid} | {_QUESTIONS[qid]} | {verdict} | {evidence} |")
    lines.append("")
    lines.append("## 3. Cycle timings")
    lines.append("")
    lines.extend(_timing_tables(rows))
    lines.append("")
    lines.append("## 4. Cutover")
    lines.append("")
    lines.extend(_cutover(run_dir, rows))
    lines.append("")
    lines.append("## 5. Not verified by this run")
    lines.append("")
    lines.append(
        "- Whether the default `neo4j` database counts toward the database limit. "
        "The capacity check counts it. This run did not create databases up to the limit."
    )
    skipped = [row for row in rows if row.get("ok") is False]
    if skipped:
        lines.append("- Failed ledger steps:")
        for row in skipped:
            lines.append(
                f"  - {row.get('target')} {row.get('tenant') or '-'} {row.get('cycle') or '-'} "
                f"{row.get('step')}: {row.get('error_type')} {row.get('error_msg', '')[:240]}"
            )
    if not rows:
        lines.append("- The ledger is empty.")
    lines.append("")
    lines.append("Interpretation is marked as interpretation. Timings are the ledger's `duration_s` values.")
    lines.append("")
    path = run_dir / "report.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def latest_run_id(work: Path) -> str:
    runs = work / "runs"
    if not runs.exists():
        raise FileNotFoundError(f"no runs under {runs}")
    names = sorted(path.name for path in runs.iterdir() if (path / "ledger.jsonl").exists())
    if not names:
        raise FileNotFoundError(f"no ledger under {runs}")
    return names[-1]


def _read_ledger(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _summary_aura(state: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "instance_id",
        "name",
        "connection_url",
        "multi_database",
        "aura_version",
        "aura_edition",
        "created_at",
        "deleted_at",
        "request",
        "run_id",
    )
    return {key: state.get(key) for key in keys if key in state}


def _verdict(rows: list[dict[str, Any]], predicate) -> str:
    matched = [row for row in rows if predicate(row)]
    if not matched:
        return "not run"
    oks = [row.get("ok") for row in matched]
    if all(oks):
        return "PASS"
    if any(oks):
        return "PARTIAL"
    return "FAIL"


def _evidence(rows: list[dict[str, Any]], predicate, limit: int = 6) -> str:
    hits = [row for row in rows if predicate(row)]
    if not hits:
        return "no ledger rows"
    parts = []
    for row in hits[:limit]:
        parts.append(
            f"{row.get('step')}:{row.get('tenant') or '-'}:{row.get('cycle') or '-'}:{'ok' if row.get('ok') else 'fail'}"
        )
    return ", ".join(parts)


def _answers(rows: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    def is_upload(row: dict[str, Any]) -> bool:
        return row.get("target") == "aura" and row.get("step") in {"upload", "verify"}

    def is_switch(row: dict[str, Any]) -> bool:
        return row.get("target") == "aura" and row.get("step") in {"switch", "verify_alias"}

    def is_local(row: dict[str, Any]) -> bool:
        return row.get("target") == "local" and row.get("step") in {"verify", "verify_alias", "db_drop"}

    def is_api(row: dict[str, Any]) -> bool:
        if row.get("step") == "aura_up" and row.get("ok") is False:
            if any(later.get("step") == "aura_up" and later.get("ok") for later in rows):
                return False
        return row.get("step") in {"aura_up", "db_create_api", "db_delete_api"}

    questions = [("Q1", is_upload), ("Q2", is_switch), ("Q3", is_local), ("Q4", is_api)]
    return [(qid, _verdict(rows, predicate), _evidence(rows, predicate)) for qid, predicate in questions]


def _timing_tables(rows: list[dict[str, Any]]) -> list[str]:
    grouped: dict[tuple[str, str, str], dict[str, float]] = {}
    for row in rows:
        if not row.get("tenant") or not row.get("cycle"):
            continue
        key = (str(row.get("target")), str(row.get("tenant")), str(row.get("cycle")))
        bucket = grouped.setdefault(key, {})
        step = str(row.get("step"))
        bucket[step] = float(row.get("duration_s") or 0)
        bucket["__total__"] = bucket.get("__total__", 0.0) + float(row.get("duration_s") or 0)
    if not grouped:
        return ["No per-tenant cycle steps were recorded."]
    header = "| target | tenant | cycle | " + " | ".join(_STEP_COLUMNS) + " | total |"
    rule = "| --- | --- | --- | " + " | ".join("---" for _ in _STEP_COLUMNS) + " | --- |"
    lines = [header, rule]
    for key in sorted(grouped):
        bucket = grouped[key]
        cells = [f"{bucket.get(step, 0):.3f}" if step in bucket else "" for step in _STEP_COLUMNS]
        lines.append(
            f"| {key[0]} | {key[1]} | {key[2]} | " + " | ".join(cells) + f" | {bucket.get('__total__', 0):.3f} |"
        )
    return lines


def _cutover(run_dir: Path, rows: list[dict[str, Any]]) -> list[str]:
    files = sorted(run_dir.glob("watcher-*.jsonl")) if run_dir.exists() else []
    if not files:
        return ["No watcher files."]
    lines = []
    for path in files:
        # watcher-local-acme.jsonl or watcher-aura-acme.jsonl
        parts = path.stem.split("-")
        target = parts[1] if len(parts) > 1 else ""
        tenant = parts[-1] if parts else ""
        reads = _watcher_rows(path)
        switch = _watched_switch(rows, target, tenant, reads)
        switch_at = switch.get("ended_at") if switch else None
        summary = summarize_watcher(
            path,
            switch_at=switch_at,
            new_cycle=switch.get("cycle") if switch else None,
            old_cycle=_cycle_before(reads, switch_at) if switch_at else None,
        )
        lines.append(f"### `{path.name}`")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(summary, indent=2, sort_keys=True))
        lines.append("```")
        lines.append("")
    return lines


def _watcher_rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _watched_switch(
    rows: list[dict[str, Any]], target: str, tenant: str, reads: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """The switch for this tenant that finished while the watcher was reading, if any."""
    if not reads:
        return None
    first, last = reads[0]["ts"], reads[-1]["ts"]
    during = [
        row
        for row in rows
        if row.get("step") == "switch"
        and row.get("ok")
        and row.get("target") == target
        and row.get("tenant") == tenant
        and row.get("ended_at")
        and first <= row["ended_at"] <= last
    ]
    return during[-1] if during else None


def _cycle_before(reads: list[dict[str, Any]], switch_at: str) -> str | None:
    """The cycle the alias served before the switch: the first successful read's cycle."""
    for row in reads:
        if row["ts"] >= switch_at:
            break
        if row.get("ok") and row.get("cycle_seen"):
            return str(row["cycle_seen"])
    return None

