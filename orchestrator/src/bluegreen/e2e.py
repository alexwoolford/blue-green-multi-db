"""Full local baseline and Aura refresh."""

from __future__ import annotations

import logging

from bluegreen.aura_instance import InstanceGuard, aura_up, finish_aura_up
from bluegreen.config import Settings
from bluegreen.cycle_aura import run_aura_cycle
from bluegreen.cycle_local import run_local_cycle
from bluegreen.doctor import run_doctor
from bluegreen.ledger import Ledger, open_ledger
from bluegreen.redact import redact
from bluegreen.report import write_report
from bluegreen.state import load_aura

logger = logging.getLogger("bluegreen.e2e")


def _local_baseline_done(ledger: Ledger, tenants: list[str]) -> bool:
    rows = ledger.read()
    for tenant in tenants:
        if not any(
            row.get("ok")
            and row.get("step") == "verify_alias"
            and row.get("target") == "local"
            and row.get("tenant") == tenant
            and row.get("cycle") == "c0002"
            for row in rows
        ):
            return False
    return True


def run_e2e(settings: Settings) -> Ledger:
    settings.require_region()
    settings.require_credentials()
    existing = load_aura(settings.work_path())
    resume = bool(
        existing
        and existing.get("instance_id")
        and existing.get("password")
        and not existing.get("deleted_at")
    )
    prior_run = str(existing.get("run_id")) if resume and existing and existing.get("run_id") else None
    ledger = open_ledger(settings.work_path(), prior_run)
    guard = InstanceGuard(settings, ledger.run_id)
    guard.install()
    try:
        if resume:
            logger.warning(
                "resuming Aura instance %s on run %s instead of creating another",
                existing.get("instance_id") if existing else "",
                ledger.run_id,
            )
            if not _local_baseline_done(ledger, settings.tenant_list):
                run_doctor(settings, ledger, aura=False)
                for tenant in settings.tenant_list:
                    run_local_cycle(settings, tenant, "c0001", watch=False, ledger=ledger)
                    run_local_cycle(settings, tenant, "c0002", watch=True, ledger=ledger)
            finish_aura_up(settings, ledger)
        else:
            run_doctor(settings, ledger, aura=False)
            for tenant in settings.tenant_list:
                run_local_cycle(settings, tenant, "c0001", watch=False, ledger=ledger)
                run_local_cycle(settings, tenant, "c0002", watch=True, ledger=ledger)
            aura_up(settings, ledger)
        run_doctor(settings, ledger, aura=True)
        for tenant in settings.tenant_list:
            run_aura_cycle(settings, tenant, "c0001", watch=False, ledger=ledger)
            run_aura_cycle(settings, tenant, "c0002", watch=True, ledger=ledger)
        return ledger
    finally:
        try:
            path = write_report(settings.work_path(), ledger.run_id)
            print(f"Report: {path}")
        except Exception as exc:
            logger.error("report failed: %s", redact(str(exc)))
        guard.teardown()
