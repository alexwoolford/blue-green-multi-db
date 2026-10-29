"""Aura cycle: build a dump locally, upload it into a new database, then switch."""

from __future__ import annotations

import json
import logging
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
    drop_alias,
    read_snapshot_cycle,
    run_autocommit,
    run_read,
    run_system,
    show_databases,
    verify_database,
    wait_until_absent,
)
from bluegreen.errors import ApiError, ConfigError, OpenQuestionError, VerificationError
from bluegreen.generator import generate, read_manifest
from bluegreen.ledger import Ledger, open_ledger
from bluegreen.naming import db_name, prior_cycle, quote_ident, validate_cycle, validate_tenant
from bluegreen.redact import add_secret
from bluegreen.state import load_aura, load_routes, record_oq, save_route, update_aura
from bluegreen.switch import SwitchResult, make_switch
from bluegreen.util import utc_now
from bluegreen.watcher import Watcher

logger = logging.getLogger("bluegreen.aura.cycle")


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


def _dbms_name(state: dict[str, Any], api_name: str) -> str:
    mapping = state.get("name_map") or {}
    return str(mapping.get(api_name) or api_name)


def _remember_database(settings: Settings, state: dict[str, Any], api_name: str, db_id: str, dbms_name: str) -> None:
    databases = dict(state.get("databases") or {})
    databases[api_name] = db_id
    if dbms_name != api_name:
        databases[dbms_name] = db_id
    name_map = dict(state.get("name_map") or {})
    name_map[api_name] = dbms_name
    state["databases"] = databases
    state["name_map"] = name_map
    update_aura(settings.work_path(), databases=databases, name_map=name_map)


def _find_db_id(client: AuraClient, state: dict[str, Any], api_name: str) -> str | None:
    known = (state.get("databases") or {}).get(api_name)
    if known:
        return str(known)
    for row in client.list_databases(str(state["instance_id"])):
        if row.get("name") == api_name and row.get("id"):
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
        from bluegreen.naming import next_cycle

        cycle = next_cycle(current_cycle) if current_cycle else "c0001"
    validate_cycle(cycle)
    database = db_name(tenant, cycle)
    client = _client(settings, state)
    driver = _driver(state)
    watcher: Watcher | None = None
    try:
        strategy = make_switch(driver, settings.switch_mode, state.get("alias_supported"))
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

        before = {row["name"] for row in show_databases(driver)}
        with ledger.step("db_create_api", target="aura", tenant=tenant, cycle=cycle) as details:
            created = client.create_database(str(state["instance_id"]), database)
            db_id = str(created.get("id") or "")
            if not db_id:
                raise ApiError(f"create database response had no id: {list(created)}", error_code="shape")
            details["db_id"] = db_id
            details["api_name"] = created.get("name", database)
            details["response_keys"] = sorted(created)

        with ledger.step("db_ready", target="aura", tenant=tenant, cycle=cycle) as details:
            dbms_name, ready_rows = _wait_ready(driver, database, before, timeout_s=10 * 60)
            details["dbms_name"] = dbms_name
            details["status"] = "online"
            try:
                remote = client.get_database(str(state["instance_id"]), db_id)
            except ApiError as exc:
                remote = {"error": str(exc)}
            details["api_database"] = remote
            status_fields = {
                key: value
                for key, value in remote.items()
                if "status" in key.lower() or key.lower() in {"state", "phase"}
            }
            record_oq(
                settings.work_path(),
                "OQ-3",
                "SHOW DATABASES currentStatus=online is authoritative; "
                f"API status fields: {status_fields or 'none'}",
                f"GET database response keys: {sorted(remote)}",
            )

        if not state.get("naming_checked"):
            with ledger.step("discover_naming", target="aura", tenant=tenant, cycle=cycle) as details:
                rows = ready_rows
                details["databases"] = rows
                session_ok = False
                session_error = None
                try:
                    run_read(driver, database, "RETURN 1 AS ok")
                    session_ok = True
                except Neo4jError as exc:
                    session_error = str(exc)[:500]
                matches = dbms_name == database and session_ok
                details["session_ok"] = session_ok
                details["session_error"] = session_error
                details["dbms_name"] = dbms_name
                record_oq(
                    settings.work_path(),
                    "OQ-4",
                    "DBMS name matches the API name" if matches else f"DBMS name is {dbms_name}",
                    f"session(database={database!r}) ok={session_ok}",
                )
                update_aura(settings.work_path(), naming_checked=True)
                state["naming_checked"] = True
        _remember_database(settings, state, database, db_id, dbms_name)

        _upload_guard(strategy, settings, state, tenant, db_id, dbms_name)

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
            details["transitions"] = _wait_snapshot(driver, dbms_name, cycle, timeout_s)

        manifest = read_manifest(settings.work_path(), tenant, cycle)
        with ledger.step("schema", target="aura", tenant=tenant, cycle=cycle):
            apply_schema(driver, dbms_name)
        with ledger.step("verify", target="aura", tenant=tenant, cycle=cycle) as details:
            details.update(verify_database(driver, dbms_name, manifest, tenant, cycle))

        if watch:
            watcher = Watcher(
                driver,
                lambda: strategy.resolve(tenant),
                ledger.run_dir / f"watcher-aura-{tenant}.jsonl",
            )
            watcher.start()
            time.sleep(3)

        with ledger.step("switch", target="aura", tenant=tenant, cycle=cycle) as details:
            result: SwitchResult = strategy.switch(tenant, dbms_name, cycle)
            details["switch_mode"] = result.mode
            details["admin_path"] = result.admin_path
            details["database"] = result.database
            if result.alias_error_code:
                details["alias_error_code"] = result.alias_error_code
                details["alias_error_msg"] = result.alias_error_msg
            save_route(
                settings.work_path(),
                "aura",
                tenant,
                {
                    "database": dbms_name,
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

        previous = prior_cycle(cycle)
        if previous:
            previous_name = db_name(tenant, previous)
            previous_id = _find_db_id(client, state, previous_name)
            if previous_id and previous_id != db_id:
                with ledger.step("db_delete_api", target="aura", tenant=tenant, cycle=cycle) as details:
                    details["db_id"] = previous_id
                    details["name"] = previous_name
                    client.delete_database(str(state["instance_id"]), previous_id)
                    _wait_api_gone(client, str(state["instance_id"]), previous_id)
                    previous_dbms = _dbms_name(state, previous_name)
                    wait_until_absent(driver, previous_dbms, timeout_s=10 * 60)

        with ledger.step("cleanup", target="aura", tenant=tenant, cycle=cycle) as details:
            from bluegreen.cypher import remove_store

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


def _wait_ready(driver, database: str, before: set[str], timeout_s: float) -> tuple[str, list[dict[str, Any]]]:
    deadline = time.monotonic() + timeout_s
    allow_rename_after = time.monotonic() + 60
    last_rows: list[dict[str, Any]] = []
    while True:
        last_rows = show_databases(driver)
        by_name = {row["name"]: row for row in last_rows}
        row = by_name.get(database)
        if row and row["currentStatus"] == "online":
            return database, last_rows
        if time.monotonic() >= allow_rename_after:
            newcomers = [
                name
                for name, item in by_name.items()
                if name not in before and item["currentStatus"] == "online" and name not in {"system", "neo4j"}
            ]
            if len(newcomers) == 1 and database not in by_name:
                logger.warning("[db-naming] DBMS name %s differs from API name %s", newcomers[0], database)
                return newcomers[0], last_rows
        if time.monotonic() >= deadline:
            names = sorted(by_name)
            raise OpenQuestionError(
                f"[db-naming] database {database} did not appear as online within {timeout_s:.0f}s. "
                f"SHOW DATABASES names: {names}"
            )
        time.sleep(5)


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


def _upload_guard(strategy, settings: Settings, state: dict[str, Any], tenant: str, db_id: str, dbms_name: str) -> None:
    current = None
    try:
        current = strategy.current_target(tenant)
    except Neo4jError as exc:
        logger.info("alias lookup before upload failed: %s", exc)
    route = (load_routes(settings.work_path()).get("aura") or {}).get(tenant) or {}
    current_id = (state.get("databases") or {}).get(current) if current else None
    if current == dbms_name or route.get("db_id") == db_id or current_id == db_id:
        raise VerificationError(
            f"refusing to upload: alias or route for {tenant} already points at database id {db_id} ({dbms_name})"
        )


def alias_probe(settings: Settings, ledger: Ledger, *, probe_delete_aliased: bool = False) -> None:
    """Check that aliases work on Aura: flip an alias between scratch databases probe-a and probe-b."""
    state = _require_state(settings)
    client = _client(settings, state)
    driver = _driver(state)
    created: list[tuple[str, str]] = []
    try:
        with ledger.step("alias_probe", target="aura") as details:
            evidence: dict[str, Any] = {}
            try:
                drop_alias(driver, "probe")
            except Neo4jError:
                pass
            for name, cycle in (("probe-a", "a"), ("probe-b", "b")):
                before = {row["name"] for row in show_databases(driver)}
                payload = client.create_database(str(state["instance_id"]), name)
                db_id = str(payload.get("id") or "")
                created.append((name, db_id))
                dbms_name, _ = _wait_ready(driver, name, before, timeout_s=10 * 60)
                run_autocommit(
                    driver,
                    dbms_name,
                    "CREATE (:Snapshot {cycle: $cycle})",
                    {"cycle": cycle},
                    timeout=60,
                )
                evidence[name] = {"db_id": db_id, "dbms_name": dbms_name}
            alias_error = None
            try:
                run_system(driver, f"CREATE ALIAS {quote_ident('probe')} FOR DATABASE {quote_ident('probe-a')}", timeout=60)
                seen = read_snapshot_cycle(driver, "probe")
                evidence["read_a"] = seen
                run_system(
                    driver,
                    f"ALTER ALIAS {quote_ident('probe')} SET DATABASE TARGET {quote_ident('probe-b')}",
                    timeout=60,
                )
                seen_b = read_snapshot_cycle(driver, "probe")
                evidence["read_b"] = seen_b
                records, _ = run_system(driver, "SHOW ALIASES FOR DATABASE", timeout=60)
                evidence["aliases"] = [
                    {"name": record["name"], "database": record["database"]}
                    for record in records
                    if "name" in record.keys()
                ]
                supported = seen == "a" and seen_b == "b"
            except Neo4jError as exc:
                alias_error = {
                    "code": getattr(exc, "gql_status", None) or getattr(exc, "code", None),
                    "message": str(exc)[:800],
                }
                supported = False
                evidence["alias_error"] = alias_error
            if probe_delete_aliased and supported:
                probe_b_id = evidence["probe-b"]["db_id"]
                try:
                    client.delete_database(str(state["instance_id"]), probe_b_id)
                    evidence["delete_aliased"] = "deleted"
                except ApiError as exc:
                    evidence["delete_aliased"] = f"blocked: {exc}"[:500]
                records, _ = run_system(driver, "SHOW ALIASES FOR DATABASE", timeout=60)
                evidence["aliases_after_delete"] = [
                    {"name": record["name"], "database": record["database"]}
                    for record in records
                    if "name" in record.keys() and record["name"] == "probe"
                ]
            details["evidence"] = evidence
            details["alias_supported"] = supported
            update_aura(
                settings.work_path(),
                alias_supported=supported,
                alias_probe=evidence,
            )
            record_oq(
                settings.work_path(),
                "OQ-5",
                "alias commands work for the instance admin" if supported else "alias commands failed",
                json.dumps(alias_error or evidence.get("read_b"), default=str)[:500],
            )
            if not supported and settings.switch_mode == "alias":
                raise OpenQuestionError(
                    "[alias-support] SWITCH_MODE=alias but CREATE/ALTER ALIAS failed on Aura. "
                    f"Evidence: {alias_error}"
                )
    finally:
        try:
            drop_alias(driver, "probe")
        except Neo4jError as exc:
            logger.info("drop alias probe: %s", exc)
        for name, db_id in created:
            if not db_id:
                continue
            try:
                client.delete_database(str(state["instance_id"]), db_id)
            except ApiError as exc:
                logger.info("delete %s (%s): %s", name, db_id, exc)
        driver.close()
        client.close()
