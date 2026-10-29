"""Full local cycle. Run inside the toolbox: pytest -m local."""

import json
from pathlib import Path

import pytest

from bluegreen.config import load_settings
from bluegreen.cypher import drop_alias, drop_database, local_driver, remove_store, show_databases
from bluegreen.cycle_local import run_local_cycle
from bluegreen.ledger import open_ledger
from bluegreen.naming import db_name

pytestmark = pytest.mark.local


def _cleanup(settings, tenants: tuple[str, ...]) -> None:
    driver = local_driver(settings)
    try:
        for tenant in tenants:
            try:
                drop_alias(driver, tenant)
            except Exception:
                pass
            for cycle in ("c0001", "c0002", "c0003"):
                database = db_name(tenant, cycle)
                try:
                    drop_database(driver, database, wait_seconds=120)
                except Exception:
                    pass
                remove_store(Path("/local-data"), database)
    finally:
        driver.close()


def test_two_tenants_refresh_and_managed_watcher_is_clean():
    settings = load_settings().model_copy(update={"scale": 0.05, "seed": 7})
    tenants = ("qa1", "qa2")
    _cleanup(settings, tenants)
    ledger = open_ledger(settings.work_path(), None)
    try:
        for tenant in tenants:
            run_local_cycle(settings, tenant, "c0001", watch=False, ledger=ledger)
            run_local_cycle(settings, tenant, "c0002", watch=True, ledger=ledger)
        driver = local_driver(settings)
        try:
            databases = {row["name"]: row for row in show_databases(driver)}
            for tenant in tenants:
                assert db_name(tenant, "c0001") not in databases
                assert databases[db_name(tenant, "c0002")]["currentStatus"] == "online"
                aliases = databases[db_name(tenant, "c0002")]["aliases"]
                assert tenant in aliases
        finally:
            driver.close()
        for tenant in tenants:
            path = ledger.run_dir / f"watcher-local-{tenant}.jsonl"
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            managed = [row for row in rows if row["probe"] == "managed"]
            assert managed, f"no managed probes for {tenant}"
            failed = [row for row in managed if not row["ok"]]
            assert failed == [], failed
    finally:
        _cleanup(settings, tenants)
