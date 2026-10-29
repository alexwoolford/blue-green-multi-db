"""Build work/runs/<run_id>/report.md from the ledger and saved evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bluegreen.state import load_aura, load_oq
from bluegreen.watcher import summarize_watcher

_OQ = {
    "OQ-1": "v2beta1 type value for Business Critical",
    "OQ-2": "Pause/resume endpoint for MultiDB",
    "OQ-3": "Which API field signals database readiness",
    "OQ-4": "DBMS name vs API name for created databases",
    "OQ-5": "Alias commands allowed for the instance admin on Aura",
    "OQ-6": "Exact upload credential flags and env-var support",
    "OQ-7": "--additional-config honored, and the toolbox default data dir",
    "OQ-8": "Does the default neo4j database count toward the database limit?",
    "OQ-9": "Do aliases and routes survive pause/resume?",
    "OQ-10": "Does import-in-place then CREATE DATABASE mount the store?",
    "OQ-11": "Is HEAP_SIZE honored by neo4j-admin?",
}

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
    "Q3": "Optional: do pause and resume work, and do databases, aliases, and data survive them?",
    "Q4": "Does the same refresh work on self-managed Enterprise Edition?",
    "Q5": "Which Aura API calls and payloads work?",
}

# Error messages start with one of these tags so a failure can be traced to the assumption it broke.
_BLOCKERS = {
    "[instance-type]": "Aura rejected the instance type or did not enable multiple databases",
    "[pause-endpoint]": "Aura rejected the configured pause or resume endpoint",
    "[db-naming]": "the database never appeared under the expected name",
    "[alias-support]": "Aura rejected CREATE/ALTER ALIAS",
    "[upload-flags]": "neo4j-admin lacks a required upload flag",
    "[import-in-place]": "CREATE DATABASE did not mount the imported store",
    "[version-guard]": "the build tools are newer than the Aura instance",
}


def write_report(work: Path, run_id: str) -> Path:
    run_dir = work / "runs" / run_id
    ledger_path = run_dir / "ledger.jsonl"
    rows = _read_ledger(ledger_path)
    oq = load_oq(work)
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
    lines.append("## 5. Pause/resume")
    lines.append("")
    lines.extend(_pause_section(rows))
    lines.append("")
    if any(row.get("step") == "pause_request" for row in rows):
        lines.append(
            "A paused instance is billed at a discounted rate. "
            "Aura auto-resumes a paused instance after 30 days, so a plan to stay paused "
            "for the rest of a 31-day month can still include a day at the full rate. This run did not test that."
        )
        lines.append("")
    lines.append("## 6. Open questions")
    lines.append("")
    lines.append("| ID | Question | Observed | Evidence |")
    lines.append("| --- | --- | --- | --- |")
    for oq_id, question in _OQ.items():
        found = oq.get(oq_id) or {}
        answer = found.get("answer") or "not settled by this run"
        evidence = found.get("evidence") or ""
        lines.append(f"| {oq_id} | {question} | {answer} | {evidence} |")
    lines.append("")
    lines.append("## 7. Not verified by this run")
    lines.append("")
    missing = [oq_id for oq_id in _OQ if oq_id not in oq]
    if missing:
        lines.append("- Open questions with no observation: " + ", ".join(missing))
    else:
        lines.append("- Every open question has an observation above. OQ-8 is only settled if an answer was recorded.")
    if "OQ-8" not in oq:
        lines.append(
            "- OQ-8 was left open. The capacity check counts the default `neo4j` database. "
            "This run did not create databases up to the limit."
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
        "alias_supported",
        "request",
        "run_id",
    )
    return {key: state.get(key) for key in keys if key in state}


def _blocked(rows: list[dict[str, Any]], *needles: str) -> str | None:
    for row in rows:
        if row.get("ok") is not False:
            continue
        text = f"{row.get('error_msg') or ''} {row.get('error_type') or ''}"
        for needle in needles:
            if needle in text:
                return f"BLOCKED: {_BLOCKERS[needle]}"
    return None


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
        return row.get("target") == "aura" and row.get("step") in {"switch", "verify_alias", "alias_probe"}

    def is_pause(row: dict[str, Any]) -> bool:
        return row.get("step") in {"pause_done", "resume_done", "post_resume_verify"}

    def is_local(row: dict[str, Any]) -> bool:
        return row.get("target") == "local" and row.get("step") in {"verify", "verify_alias", "db_drop"}

    def is_api(row: dict[str, Any]) -> bool:
        if row.get("step") == "aura_up" and row.get("ok") is False:
            if any(later.get("step") == "aura_up" and later.get("ok") for later in rows):
                return False
        return row.get("step") in {"aura_up", "db_create_api", "pause_request", "resume_request", "db_delete_api"}

    questions = [
        ("Q1", is_upload, ("[instance-type]", "[upload-flags]", "[version-guard]", "[db-naming]")),
        ("Q2", is_switch, ("[alias-support]",)),
        ("Q3", is_pause, ("[pause-endpoint]",)),
        ("Q4", is_local, ("[import-in-place]",)),
        ("Q5", is_api, ("[instance-type]", "[pause-endpoint]")),
    ]
    rendered = []
    for qid, predicate, blockers in questions:
        # A blocker only decides the verdict when no relevant step succeeded.
        blocked = _blocked(rows, *blockers)
        if blocked and not any(predicate(row) and row.get("ok") for row in rows):
            verdict = blocked
        else:
            verdict = _verdict(rows, predicate)
        if qid == "Q3" and verdict == "not run":
            verdict = "not run (PAUSE_ITERATIONS=0)"
        rendered.append((qid, verdict, _evidence(rows, predicate)))
    return rendered


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
        # watcher-local-acme.jsonl or watcher-aura-acme.jsonl or watcher-pause-01-acme.jsonl
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


def _pause_section(rows: list[dict[str, Any]]) -> list[str]:
    pauses = [row for row in rows if row.get("step") in {"pause_request", "pause_done", "resume_done", "hold"}]
    if not pauses:
        return ["Pause/resume was not run (PAUSE_ITERATIONS=0). It is optional and not part of the blue/green pattern."]
    lines = [
        "| iteration | n_databases | dump_bytes | to_paused_s | to_running_s | aliases | note |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    by_cycle: dict[str, dict[str, Any]] = {}
    for row in rows:
        cycle = str(row.get("cycle") or "")
        if row.get("step") in {"pause_request", "pause_done", "resume_done", "post_resume_verify", "tenant_ready"} or (
            isinstance(row.get("step"), str) and str(row.get("step")).startswith("tenant_ready")
        ):
            by_cycle.setdefault(cycle, {}).setdefault(row["step"], row)
    for cycle, bucket in sorted(by_cycle.items()):
        request = bucket.get("pause_request") or {}
        paused = bucket.get("pause_done") or {}
        resumed = bucket.get("resume_done") or {}
        details = request.get("details") or {}
        note = (resumed.get("details") or {}).get("note") or ""
        lines.append(
            f"| {cycle} | {details.get('n_databases', '')} | {details.get('dump_bytes', '')} | "
            f"{paused.get('duration_s', '')} | {resumed.get('duration_s', '')} | "
            f"see post_resume_verify | {note} |"
        )
    lines.append("")
    lines.append("Status transitions are on the `pause_done` and `resume_done` ledger rows.")
    for row in rows:
        if row.get("step") in {"pause_done", "resume_done"}:
            transitions = (row.get("details") or {}).get("transitions")
            if transitions:
                lines.append("")
                lines.append(f"`{row.get('step')}` `{row.get('cycle')}`:")
                for item in transitions:
                    lines.append(f"- {item.get('at')}: {item.get('value')}")
    return lines
