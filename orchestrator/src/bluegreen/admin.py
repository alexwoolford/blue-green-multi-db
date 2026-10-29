"""neo4j-admin wrappers. `bg doctor` checks that these flags exist before they are used."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from bluegreen.config import Settings
from bluegreen.errors import AdminError
from bluegreen.redact import add_secret, redact


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


def _env(settings: Settings) -> dict[str, str]:
    env = os.environ.copy()
    env["HEAP_SIZE"] = settings.admin_heap_size
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
    data = settings.work_path() / "data" / tenant / cycle
    conf = settings.conf_dir() / conf_name
    args = [
        "neo4j-admin",
        "database",
        "import",
        "full",
        database,
        f"--additional-config={conf}",
        "--id-type=string",
        f"--nodes=Customer={data / 'customers.csv.gz'}",
        f"--nodes=Account={data / 'accounts.csv.gz'}",
        f"--nodes=Merchant={data / 'merchants.csv.gz'}",
        f"--nodes=Snapshot={data / 'snapshot.csv'}",
        f"--relationships=OWNS={data / 'owns.csv.gz'}",
        f"--relationships=SENT={data / 'sent.csv.gz'}",
        f"--relationships=PAID={data / 'paid.csv.gz'}",
    ]
    if overwrite:
        args.append("--overwrite-destination=true")
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
    run_admin(
        import_args(settings, database, tenant, cycle, conf_name, overwrite=overwrite),
        log_path=log_path,
        env=_env(settings),
        step="import",
    )


def dump_database(
    settings: Settings,
    database: str,
    conf_name: str,
    dest: Path,
    log_path: Path,
) -> int:
    dest.mkdir(parents=True, exist_ok=True)
    conf = settings.conf_dir() / conf_name
    args = [
        "neo4j-admin",
        "database",
        "dump",
        database,
        f"--additional-config={conf}",
        f"--to-path={dest}",
        "--overwrite-destination=true",
    ]
    run_admin(args, log_path=log_path, env=_env(settings), step="dump")
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
    add_secret(password)
    # Credentials go in the environment (documented defaults of --to-user/--to-password), not on the command line.
    env = _env(settings)
    env["NEO4J_USERNAME"] = user
    env["NEO4J_PASSWORD"] = password
    args = [
        "neo4j-admin",
        "database",
        "upload",
        database,
        f"--from-path={dump_dir}",
        f"--to-uri={uri}",
        f"--to-dbid={db_id}",
        "--overwrite-destination=true",
    ]
    run_admin(args, log_path=log_path, env=env, step="upload")
