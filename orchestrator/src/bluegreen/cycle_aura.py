"""Aura cycle: build a dump locally, upload it into a new database, then switch."""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any

from neo4j.exceptions import Neo4jError

from bluegreen.admin import dump_database, import_full, upload_database
from bluegreen.aura_api import AuraClient
from bluegreen.config import Settings
from bluegreen.cypher import (
    apply_schema,
    connect,
    database_status,
    read_snapshot_cycle,
    remove_store,
    verify_database,
    wait_until_absent,
    wait_until_online,
)
from bluegreen.errors import ApiError, ConfigError, VerificationError
from bluegreen.generator import generate, read_manifest
from bluegreen.ledger import Ledger, open_ledger
from bluegreen.naming import db_name, next_cycle, validate_cycle, validate_tenant
from bluegreen.redact import add_secret
from bluegreen.state import load_aura, load_routes, save_route, update_aura
from bluegreen.switch import AliasSwitch, SwitchResult
from bluegreen.util import utc_now
from bluegreen.watcher import Watcher


def _rel(ledger: Ledger, path: Path) -> str:
    try:
        return path.relative_to(ledger.run_dir).as_posix()
    except ValueError:
        return str(path)


def _require_state(settings: Settings) -> dict[str, Any]:
    state = load_aura(settings.work_path())
    if not state or not state.get("instance_id") or not state.get("password") or not state.get("connection_url"):
        raise ConfigError("No live Aura instance in work/state/aura.json. Run `bg aura up --yes` first.")
    add_secret(str(state.get("password")))
    return state


def _client(settings: Settings, state: dict[str, Any]) -> AuraClient:
    client = AuraClient(settings)
    if state.get("org_id") and state.get("project_id"):
        client.org_id = str(state["org_id"])
        client.project_id = str(state["project_id"])
    return client


def _driver(state: dict[str, Any]):
    return connect(str(state["connection_url"]), str(state.get("username") or "neo4j"), str(state["password"]))


def _find_db_id(client: AuraClient, state: dict[str, Any], name: str) -> str | None:
    known = (state.get("databases") or {}).get(name)
    if known:
        return str(known)
    for row in client.list_databases(str(state["instance_id"])):
        if row.get("name") == name and row.get("id"):
            return str(row["id"])
    return None


def run_aura_cycle(
    settings: Settings,
    tenant: str,
    cycle: str | None = None,
    *,
    watch: bool = False,
    run_id: str | None = None,
    ledger: Ledger | None = None,
) -> Ledger:
    validate_tenant(tenant)
    settings.require_credentials()
    state = _require_state(settings)
    ledger = ledger or open_ledger(settings.work_path(), run_id)
    if cycle is None:
        routes = load_routes(settings.work_path()).get("aura", {})
        current_cycle = (routes.get(tenant) or {}).get("cycle")
        cycle = next_cycle(current_cycle) if current_cycle else "c0001"
    validate_cycle(cycle)
    database = db_name(tenant, cycle)
    client = _client(settings, state)
    driver = _driver(state)
    watcher: Watcher | None = None
    try:
        strategy = AliasSwitch(driver)
        with ledger.step("generate", target="aura", tenant=tenant, cycle=cycle) as details:
            manifest, skipped = generate(settings.work_path(), tenant, cycle, settings.seed, settings.scale)
            details["skipped"] = skipped
            details["nodes"] = manifest["nodes"]

        with ledger.step("import", target="aura", tenant=tenant, cycle=cycle) as details:
            log_path = ledger.log_dir / f"import-{database}.log"
            import_full(
                settings,
                database,
                tenant,
                cycle,
                "build.conf",
                overwrite=True,
                log_path=log_path,
            )
            details["exit_code"] = 0
            details["log"] = _rel(ledger, log_path)

        dump_dir = settings.work_path() / "dumps" / tenant / cycle
        with ledger.step("dump", target="aura", tenant=tenant, cycle=cycle) as details:
            log_path = ledger.log_dir / f"dump-{database}.log"
            details["dump_bytes"] = dump_database(settings, database, "build.conf", dump_dir, log_path)
            details["log"] = _rel(ledger, log_path)
            details["exit_code"] = 0

        with ledger.step("db_create_api", target="aura", tenant=tenant, cycle=cycle) as details:
            created = client.create_database(str(state["instance_id"]), database)
            db_id = str(created.get("id") or "")
            if not db_id:
                raise ApiError(f"create database response had no id: {list(created)}", error_code="shape")
            details["db_id"] = db_id
            details["api_name"] = created.get("name", database)
            details["response_keys"] = sorted(created)
        state["databases"] = {**(state.get("databases") or {}), database: db_id}
        update_aura(settings.work_path(), databases=state["databases"])

        with ledger.step("db_ready", target="aura", tenant=tenant, cycle=cycle) as details:
            details["transitions"] = wait_until_online(driver, database, timeout_s=10 * 60, interval_s=5)
            details["status"] = "online"

        _upload_guard(strategy, tenant, database)

        with ledger.step("upload", target="aura", tenant=tenant, cycle=cycle) as details:
            log_path = ledger.log_dir / f"upload-{database}.log"
            upload_database(
                settings,
                database,
                dump_dir,
                uri=str(state["connection_url"]),
                db_id=db_id,
                user=str(state.get("username") or "neo4j"),
                password=str(state["password"]),
                log_path=log_path,
            )
            details["db_id"] = db_id
            details["dump_bytes"] = (dump_dir / f"{database}.dump").stat().st_size
            details["exit_code"] = 0
            details["log"] = _rel(ledger, log_path)

        dump_bytes = int((dump_dir / f"{database}.dump").stat().st_size)
        timeout_s = max(30 * 60, dump_bytes / 1_000_000 * 60)
        with ledger.step("upload_online", target="aura", tenant=tenant, cycle=cycle) as details:
            details["timeout_s"] = timeout_s
            details["transitions"] = _wait_snapshot(driver, database, cycle, timeout_s)

        manifest = read_manifest(settings.work_path(), tenant, cycle)
        with ledger.step("schema", target="aura", tenant=tenant, cycle=cycle):
            apply_schema(driver, database)
        with ledger.step("verify", target="aura", tenant=tenant, cycle=cycle) as details:
            details.update(verify_database(driver, database, manifest, tenant, cycle))

        if watch:
            watcher = Watcher(
                driver,
                lambda: strategy.resolve(tenant),
                ledger.run_dir / f"watcher-aura-{tenant}.jsonl",
            )
            watcher.start()
            time.sleep(3)

        with ledger.step("switch", target="aura", tenant=tenant, cycle=cycle) as details:
            result: SwitchResult = strategy.switch(tenant, database)
            details["switch_mode"] = result.mode
            details["admin_path"] = result.admin_path
            details["database"] = result.database
            details["previous"] = result.previous
            save_route(
                settings.work_path(),
                "aura",
                tenant,
                {
                    "database": database,
                    "cycle": cycle,
                    "db_id": db_id,
                    "mode": result.mode,
                },
            )

        with ledger.step("verify_alias", target="aura", tenant=tenant, cycle=cycle) as details:
            resolved = strategy.resolve(tenant)
            seen = read_snapshot_cycle(driver, resolved)
            details["resolved"] = resolved
            details["cycle_seen"] = seen
            if seen != cycle:
                raise VerificationError(
                    f"{tenant} via {resolved} returned cycle {seen!r}, expected {cycle!r}"
                )

        # Delete what the alias pointed at before the switch, but only a database this harness named.
        previous = result.previous
        if previous and previous != database and previous.startswith(f"{tenant}-c"):
            previous_id = _find_db_id(client, state, previous)
            if previous_id and previous_id != db_id:
                with ledger.step("db_delete_api", target="aura", tenant=tenant, cycle=cycle) as details:
                    details["db_id"] = previous_id
                    details["name"] = previous
                    client.delete_database(str(state["instance_id"]), previous_id)
                    _wait_api_gone(client, str(state["instance_id"]), previous_id)
                    wait_until_absent(driver, previous, timeout_s=10 * 60)

        with ledger.step("cleanup", target="aura", tenant=tenant, cycle=cycle) as details:
            remove_store(Path("/build-data"), database)
            details["removed_store"] = database
            if not settings.keep_dumps:
                shutil.rmtree(dump_dir, ignore_errors=True)
                details["removed_dump"] = True
            else:
                details["removed_dump"] = False
        if watcher is not None:
            time.sleep(2)
        return ledger
    finally:
        if watcher is not None:
            watcher.stop()
        driver.close()
        client.close()


def _wait_snapshot(driver, database: str, cycle: str, timeout_s: float) -> list[dict[str, str]]:
    deadline = time.monotonic() + timeout_s
    transitions: list[dict[str, str]] = []
    last = None
    while True:
        status = database_status(driver, database)
        seen = None
        if status == "online":
            try:
                seen = read_snapshot_cycle(driver, database)
            except Neo4jError:
                seen = None
        observed = f"{status}:{seen}"
        if observed != last:
            transitions.append({"at": utc_now(), "value": observed})
            last = observed
        if status == "online" and seen == cycle:
            return transitions
        if time.monotonic() >= deadline:
            raise VerificationError(
                f"{database} did not serve Snapshot cycle {cycle} within {timeout_s:.0f}s (last {observed})"
            )
        time.sleep(10)


def _wait_api_gone(client: AuraClient, instance_id: str, database_id: str) -> None:
    deadline = time.monotonic() + 10 * 60
    while True:
        rows = client.list_databases(instance_id)
        if not any(str(row.get("id")) == database_id for row in rows):
            return
        if time.monotonic() >= deadline:
            raise VerificationError(f"database id {database_id} was still listed after 10 minutes")
        time.sleep(5)


def _upload_guard(strategy: AliasSwitch, tenant: str, database: str) -> None:
    """The upload overwrites its target, so never upload into the database the alias serves."""
    try:
        current = strategy.current_target(tenant)
    except Neo4jError as exc:
        raise VerificationError(f"refusing to upload: could not read alias target for {tenant}") from exc
    if current == database:
        raise VerificationError(f"refusing to upload: alias {tenant} already points at {database}")
