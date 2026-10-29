"""Pause and resume the whole MultiDB instance and time how tenants come back."""

from __future__ import annotations

import logging
import time
from typing import Any

from neo4j.exceptions import Neo4jError

from bluegreen.aura_api import wait_for
from bluegreen.aura_instance import instance_observation
from bluegreen.config import Settings
from bluegreen.cycle_aura import _client, _driver, _require_state
from bluegreen.cypher import read_snapshot_cycle, show_databases, verify_database
from bluegreen.errors import ApiError, OpenQuestionError, VerificationError
from bluegreen.generator import read_manifest
from bluegreen.ledger import Ledger
from bluegreen.state import load_routes, record_oq
from bluegreen.switch import make_switch
from bluegreen.watcher import Watcher

logger = logging.getLogger("bluegreen.pause")


def _latest_dump_bytes(rows: list[dict[str, Any]]) -> int:
    latest: dict[str, int] = {}
    for row in rows:
        if row.get("step") == "dump" and row.get("ok") and row.get("tenant"):
            value = (row.get("details") or {}).get("dump_bytes")
            if isinstance(value, int):
                latest[str(row["tenant"])] = value
    return sum(latest.values())


def run_pause_timing(settings: Settings, ledger: Ledger, iterations: int | None = None) -> None:
    state = _require_state(settings)
    count = iterations or settings.pause_iterations or 1
    client = _client(settings, state)
    driver = _driver(state)
    try:
        strategy = make_switch(driver, settings.switch_mode, state.get("alias_supported"))
        routes = load_routes(settings.work_path()).get("aura", {})
        tenants = settings.tenant_list
        for tenant in tenants:
            if not routes.get(tenant) and strategy.current_target(tenant) is None:
                raise VerificationError(f"{tenant} has no alias or route; pause timing needs a live tenant")

        for iteration in range(1, count + 1):
            databases = [row for row in show_databases(driver) if row["name"] != "system"]
            before_targets = {}
            for tenant in tenants:
                try:
                    before_targets[tenant] = strategy.current_target(tenant)
                except Neo4jError as exc:
                    before_targets[tenant] = f"error:{exc}"
            watchers = []
            for tenant in tenants:
                watchers.append(
                    Watcher(
                        driver,
                        lambda tenant=tenant: strategy.resolve(tenant),
                        ledger.run_dir / f"watcher-pause-{iteration}-{tenant}.jsonl",
                        interval_s=1.0,
                        probes=("raw",),
                    )
                )
            for watcher in watchers:
                watcher.start()
            try:
                with ledger.step("pause_request", target="aura", cycle=f"i{iteration:02d}") as details:
                    details["n_databases"] = len(databases)
                    details["dump_bytes"] = _latest_dump_bytes(ledger.read())
                    details["iteration"] = iteration
                    try:
                        response = client.pause_instance(str(state["instance_id"]))
                    except ApiError as exc:
                        raise OpenQuestionError(
                            "[pause-endpoint] the configured pause endpoint was rejected. "
                            f"AURA_PAUSE_API={settings.aura_pause_api}. Not falling back.\n{exc}"
                        ) from exc
                    details["http_status"] = response.status_code

                with ledger.step("pause_done", target="aura", cycle=f"i{iteration:02d}") as details:

                    def status() -> str:
                        return instance_observation(client, str(state["instance_id"]))

                    details["transitions"] = wait_for(
                        status, lambda value: value == "paused", 60 * 60, 15, "pause"
                    )

                with ledger.step("hold", target="aura", cycle=f"i{iteration:02d}") as details:
                    time.sleep(settings.pause_hold_seconds)
                    details["seconds"] = settings.pause_hold_seconds

                with ledger.step("resume_request", target="aura", cycle=f"i{iteration:02d}") as details:
                    try:
                        response = client.resume_instance(str(state["instance_id"]))
                    except ApiError as exc:
                        raise OpenQuestionError(
                            "[pause-endpoint] the configured resume endpoint was rejected. "
                            f"AURA_PAUSE_API={settings.aura_pause_api}. Not falling back.\n{exc}"
                        ) from exc
                    details["http_status"] = response.status_code

                resume_mark = time.monotonic()
                with ledger.step("resume_done", target="aura", cycle=f"i{iteration:02d}") as details:
                    transitions = wait_for(
                        status, lambda value: value == "running", 90 * 60, 15, "resume"
                    )
                    details["transitions"] = transitions
                    resume_mark = time.monotonic()
                    if details.get("duration_s", 0) > 30 * 60 or _span(transitions) > 30 * 60:
                        details["note"] = (
                            "Resume exceeded 30 minutes. Aura documents that resume can take a few "
                            "hours. This harness cannot see the cause."
                        )

                aliases_intact = True
                data_intact = True
                for tenant in tenants:
                    route = routes.get(tenant) or {}
                    cycle = route.get("cycle")
                    with ledger.step(f"tenant_ready:{tenant}", target="aura", tenant=tenant, cycle=cycle) as details:
                        seen = _wait_readable(driver, strategy, tenant, resume_mark)
                        details["cycle_seen"] = seen["cycle"]
                        details["seconds_after_running"] = seen["seconds_after_running"]
                        if cycle and seen["cycle"] != cycle:
                            data_intact = False
                            raise VerificationError(
                                f"{tenant} snapshot cycle changed across pause: {seen['cycle']} != {cycle}"
                            )
                    target = strategy.current_target(tenant)
                    if target != before_targets.get(tenant):
                        aliases_intact = False
                    if cycle and target:
                        manifest = read_manifest(settings.work_path(), tenant, cycle)
                        with ledger.step("post_resume_verify", target="aura", tenant=tenant, cycle=cycle) as details:
                            details.update(verify_database(driver, target, manifest, tenant, cycle))
                            details["alias_target"] = target
                            details["aliases_intact"] = target == before_targets.get(tenant)
                record_oq(
                    settings.work_path(),
                    "OQ-9",
                    "aliases/routes and data survived pause/resume"
                    if aliases_intact and data_intact
                    else "pause/resume did not preserve aliases or data",
                    f"aliases_intact={aliases_intact} data_intact={data_intact} targets={before_targets}",
                )
            finally:
                for watcher in watchers:
                    watcher.stop()
        record_oq(
            settings.work_path(),
            "OQ-2",
            f"pause/resume used AURA_PAUSE_API={settings.aura_pause_api}",
            "v1 paths POST /v1/instances/{id}/pause and /resume"
            if settings.aura_pause_api == "v1"
            else f"custom templates {settings.aura_pause_path_template} {settings.aura_resume_path_template}",
        )
    finally:
        driver.close()
        client.close()


def _wait_readable(driver, strategy, tenant: str, running_at: float) -> dict[str, Any]:
    deadline = time.monotonic() + 10 * 60
    while True:
        try:
            database = strategy.resolve(tenant)
            cycle = read_snapshot_cycle(driver, database)
            if cycle:
                return {
                    "cycle": cycle,
                    "seconds_after_running": round(time.monotonic() - running_at, 3),
                }
        except Exception as exc:
            logger.info("tenant %s not readable yet: %s", tenant, exc)
        if time.monotonic() >= deadline:
            raise VerificationError(f"{tenant} was not readable within 10 minutes of running")
        time.sleep(2)


def _span(transitions: list[dict[str, str]]) -> float:
    if len(transitions) < 2:
        return 0.0
    from datetime import datetime

    start = datetime.fromisoformat(transitions[0]["at"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(transitions[-1]["at"].replace("Z", "+00:00"))
    return (end - start).total_seconds()
