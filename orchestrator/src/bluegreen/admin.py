"""neo4j-admin wrappers. Flag spellings come from doctor findings when present."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from bluegreen.config import Settings
from bluegreen.errors import AdminError
from bluegreen.redact import redact

_DEFAULT_FLAGS = {
    "from_path": "--from-path",
    "to_uri": "--to-uri",
    "to_dbid": "--to-dbid",
    "overwrite": "--overwrite-destination",
    "to_user": "--to-user",
    "to_password": "--to-password",
    "additional_config": "--additional-config",
    "id_type": "--id-type",
    "nodes": "--nodes",
    "relationships": "--relationships",
    "to_path": "--to-path",
    "schema": "--schema",
}


def findings_path(work: Path) -> Path:
    return work / "doctor" / "findings.json"


def load_findings(work: Path) -> dict[str, Any]:
    path = findings_path(work)
    if not path.exists():
        return {}
    import json

    return json.loads(path.read_text(encoding="utf-8"))


def _flags(findings: dict[str, Any]) -> dict[str, str]:
    merged = dict(_DEFAULT_FLAGS)
    custom = findings.get("flags") or {}
    for key, value in custom.items():
        if isinstance(value, str) and value:
            merged[key] = value
    return merged


def _opt(flag: str, value: str) -> str:
    return f"{flag}={value}"


def run_admin(
    args: list[str],
    *,
    log_path: Path,
    env: dict[str, str],
    step: str,
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    rendered = redact(" ".join(args))
    with log_path.open("w", encoding="utf-8") as handle:
        handle.write(f"# {rendered}\n")
        handle.flush()
        completed = subprocess.run(
            args,
            stdout=handle,
            stderr=subprocess.STDOUT,
            env=env,
            check=False,
        )
    text = log_path.read_text(encoding="utf-8", errors="replace")
    cleaned = redact(text)
    if cleaned != text:
        log_path.write_text(cleaned, encoding="utf-8")
    if completed.returncode != 0:
        tail = "\n".join(cleaned.splitlines()[-50:])
        sys.stderr.write(tail + "\n")
        raise AdminError(step, completed.returncode, str(log_path), tail)


def _env(settings: Settings, findings: dict[str, Any]) -> dict[str, str]:
    env = os.environ.copy()
    heap_var = findings.get("heap_env") or "HEAP_SIZE"
    env[str(heap_var)] = settings.admin_heap_size
    return env


def import_args(
    settings: Settings,
    database: str,
    tenant: str,
    cycle: str,
    conf_name: str,
    *,
    overwrite: bool,
) -> list[str]:
    findings = load_findings(settings.work_path())
    flags = _flags(findings)
    data = settings.work_path() / "data" / tenant / cycle
    conf = settings.conf_dir() / conf_name
    args = [
        "neo4j-admin",
        "database",
        "import",
        "full",
        database,
        _opt(flags["additional_config"], str(conf)),
        _opt(flags["id_type"], "string"),
        _opt(flags["nodes"], f"Customer={data / 'customers.csv.gz'}"),
        _opt(flags["nodes"], f"Account={data / 'accounts.csv.gz'}"),
        _opt(flags["nodes"], f"Merchant={data / 'merchants.csv.gz'}"),
        _opt(flags["nodes"], f"Snapshot={data / 'snapshot.csv'}"),
        _opt(flags["relationships"], f"OWNS={data / 'owns.csv.gz'}"),
        _opt(flags["relationships"], f"SENT={data / 'sent.csv.gz'}"),
        _opt(flags["relationships"], f"PAID={data / 'paid.csv.gz'}"),
    ]
    if overwrite:
        args.append(_opt(flags["overwrite"], "true"))
    if settings.import_with_schema and findings.get("has_schema"):
        from bluegreen.cypher import schema_file

        args.append(_opt(flags["schema"], str(schema_file())))
    return args


def import_full(
    settings: Settings,
    database: str,
    tenant: str,
    cycle: str,
    conf_name: str,
    *,
    overwrite: bool,
    log_path: Path,
) -> None:
    findings = load_findings(settings.work_path())
    run_admin(
        import_args(settings, database, tenant, cycle, conf_name, overwrite=overwrite),
        log_path=log_path,
        env=_env(settings, findings),
        step="import",
    )


def dump_database(
    settings: Settings,
    database: str,
    conf_name: str,
    dest: Path,
    log_path: Path,
) -> int:
    findings = load_findings(settings.work_path())
    flags = _flags(findings)
    dest.mkdir(parents=True, exist_ok=True)
    conf = settings.conf_dir() / conf_name
    args = [
        "neo4j-admin",
        "database",
        "dump",
        database,
        _opt(flags["additional_config"], str(conf)),
        _opt(flags["to_path"], str(dest)),
        _opt(flags["overwrite"], "true"),
    ]
    run_admin(args, log_path=log_path, env=_env(settings, findings), step="dump")
    dump_file = dest / f"{database}.dump"
    if not dump_file.exists():
        matches = list(dest.glob("*.dump"))
        if len(matches) == 1:
            dump_file = matches[0]
        else:
            raise AdminError("dump", 0, str(log_path), f"dump file not found in {dest}")
    return dump_file.stat().st_size


def upload_database(
    settings: Settings,
    database: str,
    dump_dir: Path,
    *,
    uri: str,
    db_id: str,
    user: str,
    password: str,
    log_path: Path,
) -> None:
    from bluegreen.redact import add_secret

    add_secret(password)
    findings = load_findings(settings.work_path())
    flags = _flags(findings)
    env = _env(settings, findings)
    args = [
        "neo4j-admin",
        "database",
        "upload",
        database,
        _opt(flags["from_path"], str(dump_dir)),
        _opt(flags["to_uri"], uri),
        _opt(flags["to_dbid"], db_id),
        _opt(flags["overwrite"], "true"),
    ]
    password_env = findings.get("upload_password_env")
    user_env = findings.get("upload_user_env")
    if password_env:
        env[str(password_env)] = password
        if user_env:
            env[str(user_env)] = user
        elif flags.get("to_user"):
            args.append(_opt(flags["to_user"], user))
    else:
        if flags.get("to_user"):
            args.append(_opt(flags["to_user"], user))
        args.append(_opt(flags["to_password"], password))
    run_admin(args, log_path=log_path, env=env, step="upload")
