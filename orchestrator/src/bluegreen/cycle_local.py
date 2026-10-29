"""Self-managed Enterprise baseline: import in place, alias swap, drop the old database."""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

from bluegreen.util import utc_now

from neo4j.exceptions import Neo4jError

from bluegreen.admin import dump_database, import_full
from bluegreen.config import Settings
from bluegreen.cypher import (
    apply_schema,
    create_database,
    database_status,
    drop_database,
    local_driver,
    node_count,
    read_snapshot_cycle,
    remove_store,
    seed_provider_settings,
    show_databases,
    verify_database,
    wait_until_online,
)
from bluegreen.errors import VerificationError
from bluegreen.generator import generate, read_manifest
from bluegreen.ledger import Ledger, open_ledger
from bluegreen.naming import db_name, prior_cycle, validate_cycle, validate_tenant
from bluegreen.redact import redact
from bluegreen.state import record_oq, save_route
from bluegreen.switch import AliasSwitch
from bluegreen.watcher import Watcher

logger = logging.getLogger("bluegreen.local")
SERVER_DATA = Path("/local-data")
SERVER_WORK = "/work"


def _rel(ledger: Ledger, path: Path) -> str:
    try:
        return path.relative_to(ledger.run_dir).as_posix()
    except ValueError:
        return str(path)


def infer_next_cycle(driver, tenant: str) -> str:
    from bluegreen.naming import cycle_number, format_cycle, next_cycle

    strategy = AliasSwitch(driver)
    current = None
    try:
        current = strategy.current_target(tenant)
    except Neo4jError:
        current = None
    prefix = f"{tenant}-c"
    if current and current.startswith(prefix):
        return next_cycle(current[len(tenant) + 1 :])
    numbers = []
    for row in show_databases(driver):
        name = row["name"]
        if name.startswith(prefix):
            try:
                numbers.append(cycle_number(name[len(tenant) + 1 :]))
            except ValueError:
                continue
    if not numbers:
        return "c0001"
    return format_cycle(max(numbers) + 1)


def run_local_cycle(
    settings: Settings,
    tenant: str,
    cycle: str | None = None,
    *,
    watch: bool = False,
    run_id: str | None = None,
    ledger: Ledger | None = None,
) -> Ledger:
    validate_tenant(tenant)
    ledger = ledger or open_ledger(settings.work_path(), run_id)
    driver = local_driver(settings)
    watcher: Watcher | None = None
    try:
        if cycle is None:
            cycle = infer_next_cycle(driver, tenant)
        validate_cycle(cycle)
        database = db_name(tenant, cycle)
        with ledger.step("generate", target="local", tenant=tenant, cycle=cycle) as details:
            manifest, skipped = generate(settings.work_path(), tenant, cycle, settings.seed, settings.scale)
            details["skipped"] = skipped
            details["nodes"] = manifest["nodes"]
            details["relationships"] = manifest["relationships"]

        with ledger.step("import", target="local", tenant=tenant, cycle=cycle) as details:
            log_path = ledger.log_dir / f"import-{database}.log"
            import_full(
                settings,
                database,
                tenant,
                cycle,
                "local.conf",
                overwrite=False,
                log_path=log_path,
            )
            details["exit_code"] = 0
            details["log"] = _rel(ledger, log_path)

        _mount(settings, driver, ledger, tenant, cycle, database)
        manifest = read_manifest(settings.work_path(), tenant, cycle)

        with ledger.step("schema", target="local", tenant=tenant, cycle=cycle):
            apply_schema(driver, database)
        with ledger.step("verify", target="local", tenant=tenant, cycle=cycle) as details:
            details.update(verify_database(driver, database, manifest, tenant, cycle))

        strategy = AliasSwitch(driver)
        if watch:
            watcher = Watcher(
                driver,
                lambda: strategy.resolve(tenant),
                ledger.run_dir / f"watcher-local-{tenant}.jsonl",
            )
            watcher.start()
            time.sleep(2)

        with ledger.step("switch", target="local", tenant=tenant, cycle=cycle) as details:
            result = strategy.switch(tenant, database, cycle)
            details["switch_mode"] = result.mode
            details["admin_path"] = result.admin_path
            details["database"] = result.database
            save_route(
                settings.work_path(),
                "local",
                tenant,
                {"database": database, "cycle": cycle, "mode": result.mode},
            )

        with ledger.step("verify_alias", target="local", tenant=tenant, cycle=cycle) as details:
            seen = read_snapshot_cycle(driver, strategy.resolve(tenant))
            details["cycle_seen"] = seen
            if seen != cycle:
                raise VerificationError(f"alias {tenant} returned cycle {seen!r}, expected {cycle!r}")

        previous = prior_cycle(cycle)
        if previous:
            previous_db = db_name(tenant, previous)
            if database_status(driver, previous_db) is not None:
                with ledger.step("db_drop", target="local", tenant=tenant, cycle=cycle) as details:
                    details["dropped"] = previous_db
                    drop_database(driver, previous_db)
        if watcher is not None:
            time.sleep(2)
        return ledger
    finally:
        if watcher is not None:
            watcher.stop()
        driver.close()


def _mount(settings: Settings, driver, ledger: Ledger, tenant: str, cycle: str, database: str) -> None:
    reason = None
    create_started = utc_now()
    create_t0 = time.perf_counter()
    try:
        create_database(driver, database)
        create_s = time.perf_counter() - create_t0
        online_started = utc_now()
        online_t0 = time.perf_counter()
        transitions = wait_until_online(driver, database, timeout_s=300)
        online_s = time.perf_counter() - online_t0
        if node_count(driver, database) == 0:
            reason = "[import-in-place] CREATE DATABASE came online with an empty store"
        else:
            ledger.write(
                step="db_create",
                target="local",
                tenant=tenant,
                cycle=cycle,
                started_at=create_started,
                duration_s=create_s,
                ok=True,
                details={"local_mode": "inplace"},
            )
            ledger.write(
                step="db_online",
                target="local",
                tenant=tenant,
                cycle=cycle,
                started_at=online_started,
                duration_s=online_s,
                ok=True,
                details={"transitions": transitions},
            )
            record_oq(
                settings.work_path(),
                "OQ-10",
                "import in place then CREATE DATABASE mounted the store",
                f"{database} node count was non-zero after CREATE DATABASE",
            )
            return
    except Neo4jError as exc:
        reason = redact(str(exc))

    logger.warning("in-place mount failed for %s: %s", database, reason)
    try:
        drop_database(driver, database)
    except Neo4jError as exc:
        logger.info("drop after failed mount: %s", redact(str(exc)))
    remove_store(SERVER_DATA, database)

    with ledger.step("import", target="local", tenant=tenant, cycle=cycle) as details:
        details["local_mode"] = "seed_uri"
        log_path = ledger.log_dir / f"import-{database}-build.log"
        import_full(
            settings,
            database,
            tenant,
            cycle,
            "build.conf",
            overwrite=True,
            log_path=log_path,
        )
        details["log"] = _rel(ledger, log_path)
    dump_dir = settings.work_path() / "dumps" / tenant / cycle
    with ledger.step("dump", target="local", tenant=tenant, cycle=cycle) as details:
        log_path = ledger.log_dir / f"dump-{database}.log"
        details["dump_bytes"] = dump_database(settings, database, "build.conf", dump_dir, log_path)
        details["log"] = _rel(ledger, log_path)
        details["local_mode"] = "seed_uri"

    seed_uri = f"file://{SERVER_WORK}/dumps/{tenant}/{cycle}/{database}.dump"
    with ledger.step("db_create", target="local", tenant=tenant, cycle=cycle) as details:
        details["local_mode"] = "seed_uri"
        details["reason"] = reason
        details["seed_uri"] = seed_uri
        try:
            create_database(driver, database, seed_uri=seed_uri)
        except Neo4jError as exc:
            providers = seed_provider_settings(driver)
            details["seed_settings"] = providers
            record_oq(
                settings.work_path(),
                "OQ-10",
                "in-place mount failed and seedURI was refused",
                f"{reason}; seedURI error: {redact(str(exc))}; settings: {providers}",
            )
            raise
    with ledger.step("db_online", target="local", tenant=tenant, cycle=cycle):
        wait_until_online(driver, database, timeout_s=300)
    record_oq(
        settings.work_path(),
        "OQ-10",
        "in-place mount failed; seedURI fallback was used",
        reason or "",
    )


def local_status(settings: Settings) -> None:
    driver = local_driver(settings)
    try:
        print("DATABASES")
        print(f"{'name':<24} {'status':<12} aliases")
        for row in show_databases(driver):
            aliases = ",".join(row["aliases"])
            print(f"{row['name']:<24} {row['currentStatus']:<12} {aliases}")
        print("ALIASES")
        from bluegreen.cypher import run_system

        records, _ = run_system(driver, "SHOW ALIASES FOR DATABASE", timeout=60)
        if not records:
            print("(none)")
            return
        for record in records:
            keys = list(record.keys())
            name = record["name"] if "name" in keys else ""
            database = record["database"] if "database" in keys else ""
            print(f"{name}\t{database}")
    finally:
        driver.close()


def remove_build_store(database: str) -> None:
    remove_store(Path("/build-data"), database)
    shutil.rmtree(Path("/build-data") / "databases" / database, ignore_errors=True)
