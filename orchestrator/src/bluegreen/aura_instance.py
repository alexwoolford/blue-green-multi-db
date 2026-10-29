"""Aura instance lifecycle, cost guardrails, and teardown."""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from bluegreen.aura_api import AuraClient, wait_for
from bluegreen.config import Settings
from bluegreen.cypher import components, connect, show_databases
from bluegreen.errors import ApiError, ConfigError, OpenQuestionError
from bluegreen.ledger import Ledger
from bluegreen.redact import add_secret, redact
from bluegreen.state import load_aura, save_aura, update_aura
from bluegreen.util import calver_is_newer, parse_calver, utc_now

logger = logging.getLogger("bluegreen.aura")


def instance_name(settings: Settings, run_id: str) -> str:
    short = run_id.split("-")[-1]
    return f"{settings.aura_instance_name_prefix}-{short}"


def _public_instance(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if "password" not in key.lower()}


_TRANSIENT_HTTP = {"500", "502", "503", "504"}


def observed_status(payload: dict[str, Any] | None) -> str:
    if not payload:
        return "missing"
    if payload.get("legacy_status"):
        return str(payload["legacy_status"])
    if payload.get("status"):
        return str(payload["status"])
    return "unknown"


def instance_observation(client: AuraClient, instance_id: str) -> str:
    """Status string for polling. A transient 5xx is not fatal; the caller keeps waiting.

    During destroy, GET by id can stay 500 after the instance has left the list.
    An id missing from the list is treated as gone.
    """
    from bluegreen.errors import ApiError

    try:
        payload = client.get_instance(instance_id, allow_missing=True)
    except ApiError as exc:
        if exc.error_code in _TRANSIENT_HTTP:
            if _absent_from_list(client, instance_id):
                return "404"
            return f"api_error:{exc.error_code}"
        raise
    if payload is None:
        return "404"
    return observed_status(payload)


def _absent_from_list(client: AuraClient, instance_id: str) -> bool:
    from bluegreen.errors import ApiError

    try:
        rows = client.list_instances()
    except ApiError:
        return False
    return not any(str(row.get("id")) == instance_id for row in rows)


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _banner(settings: Settings, name: str) -> None:
    print(
        "\n".join(
            [
                "WARNING: this creates a billed Aura Business Critical instance.",
                f"  name:   {name}",
                f"  type:   {settings.aura_instance_type}",
                f"  memory: {settings.aura_memory}",
                f"  cloud:  {settings.aura_cloud_provider}",
                f"  region: {settings.aura_region}",
                "  multi_database: true",
                "A Business Critical instance is billed while it exists, including while paused",
                "(at a discounted rate).",
                "",
            ]
        ),
        file=sys.stderr,
    )


def assert_version_guard(settings: Settings, aura_version: str) -> str:
    try:
        parse_calver(aura_version)
    except ValueError:
        note = (
            f"Aura dbms.components version {aura_version!r} is not CalVer. "
            "Aura kept returning 5.27-aura as a client-compatibility string after calendar versioning, "
            f"so NEO4J_TAG {settings.neo4j_tag} was not treated as newer. "
            "Whether the dump version is accepted is decided by the upload, not by this string."
        )
        logger.warning(note)
        return note
    if calver_is_newer(settings.neo4j_tag, aura_version):
        raise OpenQuestionError(
            f"[version-guard] NEO4J_TAG {settings.neo4j_tag} is newer than the Aura instance version "
            f"{aura_version}. Rebuild the toolbox with NEO4J_TAG <= {aura_version}."
        )
    return f"NEO4J_TAG {settings.neo4j_tag} is not newer than Aura {aura_version}"


def aura_up(settings: Settings, ledger: Ledger) -> dict[str, Any]:
    settings.require_region()
    settings.require_credentials()
    name = instance_name(settings, ledger.run_id)
    body = {
        "name": name,
        "cloud_provider": settings.aura_cloud_provider,
        "region": settings.aura_region,
        "type": settings.aura_instance_type,
        "memory": settings.aura_memory,
        "multi_database": True,
    }
    _banner(settings, name)
    client = AuraClient(settings)
    try:
        client.resolve_org_project()
        with ledger.step("aura_up", target="aura") as details:
            details["request"] = body
            try:
                data = client.create_instance(body)
            except ApiError as exc:
                if exc.error_code and exc.error_code[:1] == "4":
                    raise OpenQuestionError(
                        "[instance-type] create instance was rejected. The type value was "
                        f"{settings.aura_instance_type!r}. Not trying another type.\n{exc}"
                    ) from exc
                raise
            password = data.get("password")
            add_secret(str(password) if password else None)
            instance_id = str(data.get("id") or "")
            if not instance_id:
                raise ApiError("create instance response had no id", error_code="shape")
            state = {
                "instance_id": instance_id,
                "connection_url": data.get("connection_url"),
                "username": data.get("username") or "neo4j",
                "password": password,
                "default_database_id": data.get("default_database_id") or data.get("default_database"),
                "created_at": data.get("created_at") or utc_now(),
                "run_id": ledger.run_id,
                "name": name,
                "request": body,
                "multi_database": data.get("multi_database"),
                "create_response": _public_instance(data),
                "databases": {},
                "name_map": {},
                "alias_supported": None,
                "org_id": client.org_id,
                "project_id": client.project_id,
            }
            try:
                save_aura(settings.work_path(), state)
            except Exception:
                logger.error("state file write failed; deleting instance %s", instance_id)
                try:
                    client.delete_instance(instance_id)
                except Exception as delete_exc:
                    logger.error("also failed to delete %s: %s", instance_id, redact(str(delete_exc)))
                raise
            if data.get("multi_database") is not True:
                client.delete_instance(instance_id)
                state.pop("password", None)
                state["deleted_at"] = utc_now()
                state["delete_reason"] = "multi_database was not true"
                save_aura(settings.work_path(), state)
                raise OpenQuestionError(
                    "[instance-type] create response multi_database was not true. "
                    f"Observed keys: {sorted(_public_instance(data))}. Instance {instance_id} was deleted."
                )
            details["instance_id"] = instance_id
            details["multi_database"] = data.get("multi_database")
            details["connection_url"] = data.get("connection_url")

            transitions = _wait_running(client, instance_id)
            details["transitions"] = transitions
            _connect_and_record(settings, client, state, details)
            save_aura(settings.work_path(), state)
        return state
    finally:
        client.close()


def _wait_running(client: AuraClient, instance_id: str) -> list[dict[str, str]]:
    def status() -> str:
        value = instance_observation(client, instance_id)
        if value == "404":
            raise ApiError(
                f"instance {instance_id} is gone while waiting for it to become running",
                error_code="404",
            )
        return value

    return wait_for(status, lambda value: value == "running", 30 * 60, 15, "instance status")


def finish_aura_up(settings: Settings, ledger: Ledger) -> dict[str, Any]:
    """Finish startup for an instance that was already created and saved."""
    state = load_aura(settings.work_path())
    if not state or not state.get("instance_id") or not state.get("password"):
        raise ConfigError("No live Aura instance in work/state/aura.json.")
    add_secret(str(state.get("password")))
    instance_id = str(state["instance_id"])
    client = AuraClient(settings)
    if state.get("org_id") and state.get("project_id"):
        client.org_id = str(state["org_id"])
        client.project_id = str(state["project_id"])
    try:
        with ledger.step("aura_up", target="aura") as details:
            details["resumed"] = True
            details["instance_id"] = instance_id
            details["connection_url"] = state.get("connection_url")
            details["multi_database"] = state.get("multi_database")
            details["transitions"] = _wait_running(client, instance_id)
            _connect_and_record(settings, client, state, details)
            save_aura(settings.work_path(), state)
        return state
    finally:
        client.close()


def _connect_and_record(settings: Settings, client: AuraClient, state: dict[str, Any], details: dict[str, Any]) -> None:
    driver = connect(str(state["connection_url"]), str(state["username"]), str(state["password"]))
    try:
        info = components(driver)
        databases = show_databases(driver)
    finally:
        driver.close()
    state["aura_version"] = info.get("version")
    state["aura_edition"] = info.get("edition")
    details["components"] = info
    details["databases"] = databases
    details["version_guard"] = assert_version_guard(settings, str(info.get("version") or ""))
    from bluegreen.state import record_oq

    record_oq(
        settings.work_path(),
        "OQ-1",
        f"create succeeded with type={settings.aura_instance_type}",
        f"instance {state['instance_id']} multi_database={state.get('multi_database')}",
    )


def aura_down(settings: Settings, ledger: Ledger | None = None) -> None:
    state = load_aura(settings.work_path())
    if not state or not state.get("instance_id"):
        print("No Aura instance is recorded in work/state/aura.json.")
        return
    if state.get("deleted_at"):
        print(f"Instance {state['instance_id']} is already marked deleted.")
        return
    settings.require_credentials()
    instance_id = str(state["instance_id"])
    client = AuraClient(settings)
    if state.get("org_id") and state.get("project_id"):
        client.org_id = str(state["org_id"])
        client.project_id = str(state["project_id"])
    try:
        def _do() -> None:
            from bluegreen.errors import ApiError

            def current() -> str:
                value = instance_observation(client, instance_id)
                if value == "404" or value.startswith("api_error:"):
                    return value
                try:
                    client.delete_instance(instance_id)
                except ApiError as exc:
                    if exc.error_code not in _TRANSIENT_HTTP | {"404", "409"}:
                        raise
                return value

            transitions = wait_for(current, lambda value: value == "404", 30 * 60, 15, "instance delete")
            if ledger is not None:
                # caller may already be inside a step; stash on the state instead
                state["delete_transitions"] = transitions

        if ledger is None:
            _do()
        else:
            with ledger.step("aura_down", target="aura") as details:
                _do()
                details["instance_id"] = instance_id
                details["transitions"] = state.get("delete_transitions")
        state.pop("password", None)
        state["deleted_at"] = utc_now()
        save_aura(settings.work_path(), state)
        print(f"Deleted Aura instance {instance_id}.")
    finally:
        client.close()


def aura_reap(settings: Settings, *, yes: bool) -> None:
    settings.require_credentials()
    client = AuraClient(settings)
    try:
        client.resolve_org_project()
        rows = client.list_instances()
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=settings.aura_max_runtime_minutes)
        prefix = settings.aura_instance_name_prefix
        victims: list[dict[str, Any]] = []
        print(f"Instances with name prefix {prefix!r}:")
        for row in rows:
            name = str(row.get("name") or "")
            if not name.startswith(prefix):
                continue
            created = _parse_time(row.get("created_at"))
            age = "unknown" if created is None else created.isoformat()
            print(f"  id={row.get('id')} name={name} created_at={age} status={row.get('status') or row.get('legacy_status')}")
            if created is not None and created < cutoff:
                victims.append(row)
            elif created is None:
                print("    skipped: created_at missing, not deleting")
        if not victims:
            print("Nothing to reap.")
            return
        if not yes:
            raise ConfigError("Re-run with --yes to delete the instances listed above.")
        for row in victims:
            instance_id = str(row["id"])
            print(f"Deleting {instance_id} ({row.get('name')})")
            client.delete_instance(instance_id)
            wait_for(
                lambda instance_id=instance_id: (
                    "404" if client.get_instance(instance_id, allow_missing=True) is None else "present"
                ),
                lambda value: value == "404",
                30 * 60,
                15,
                f"reap {instance_id}",
            )
    finally:
        client.close()


def aura_status(settings: Settings) -> None:
    state = load_aura(settings.work_path()) or {}
    if not state.get("instance_id"):
        print("No instance in work/state/aura.json")
        return
    settings.require_credentials()
    client = AuraClient(settings)
    if state.get("org_id"):
        client.org_id = str(state["org_id"])
        client.project_id = str(state["project_id"])
    driver = None
    try:
        payload = client.get_instance(str(state["instance_id"]), allow_missing=True)
        print(f"instance_id: {state['instance_id']}")
        print(f"name:        {state.get('name')}")
        print(f"api_status:  {observed_status(payload)}")
        if payload is None:
            print("Instance is gone (404).")
            return
        print("api_databases:")
        for row in client.list_databases(str(state["instance_id"])):
            safe = _public_instance(row)
            print(f"  {safe}")
        if state.get("password") and state.get("connection_url"):
            add_secret(str(state.get("password")))
            driver = connect(str(state["connection_url"]), str(state.get("username") or "neo4j"), str(state["password"]))
            print("SHOW DATABASES:")
            for row in show_databases(driver):
                print(f"  {row['name']}\t{row['currentStatus']}\taliases={row['aliases']}")
        from bluegreen.state import load_routes

        print(f"routes: {load_routes(settings.work_path()).get('aura', {})}")
        print(f"alias_supported: {state.get('alias_supported')}")
    finally:
        if driver is not None:
            driver.close()
        client.close()


class InstanceGuard:
    """SIGINT, SIGTERM, and a runtime watchdog all tear the instance down once."""

    def __init__(self, settings: Settings, run_id: str):
        self.settings = settings
        self.run_id = run_id
        self._done = False
        self._interrupts = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def install(self) -> None:
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)
        self._thread = threading.Thread(target=self._watchdog, name="aura-watchdog", daemon=True)
        self._thread.start()

    def _on_signal(self, signum: int, frame: object) -> None:
        del signum, frame
        with self._lock:
            self._interrupts += 1
            count = self._interrupts
        state = load_aura(self.settings.work_path()) or {}
        instance_id = state.get("instance_id", "(unknown)")
        if count >= 2:
            print(
                f"WARNING: second interrupt. Teardown aborted. Instance {instance_id} may still be billed.",
                file=sys.stderr,
            )
            os._exit(130)
        print(f"Interrupt received. Tearing down instance {instance_id}.", file=sys.stderr)
        try:
            self.teardown()
        finally:
            os._exit(130)

    def teardown(self) -> None:
        with self._lock:
            if self._done:
                return
            self._done = True
        self._stop.set()
        state = load_aura(self.settings.work_path()) or {}
        instance_id = state.get("instance_id")
        if self.settings.aura_keep_instance:
            print(
                "LOUD: AURA_KEEP_INSTANCE=true, so the instance was NOT deleted. "
                f"Instance {instance_id} is still billed, including while paused (at a discounted rate).",
                file=sys.stderr,
            )
            return
        if not instance_id or state.get("deleted_at"):
            return
        if state.get("run_id") not in (None, self.run_id):
            logger.warning("state run_id %s does not match %s; not deleting", state.get("run_id"), self.run_id)
            return
        try:
            aura_down(self.settings)
        except Exception as exc:
            print(redact(f"teardown failed for {instance_id}: {exc}"), file=sys.stderr)
            print(f"WARNING: instance {instance_id} may still be billed.", file=sys.stderr)

    def _watchdog(self) -> None:
        while not self._stop.wait(5):
            state = load_aura(self.settings.work_path()) or {}
            if state.get("run_id") != self.run_id or state.get("deleted_at") or not state.get("created_at"):
                continue
            created = _parse_time(state.get("created_at"))
            if created is None:
                continue
            deadline = created + timedelta(minutes=self.settings.aura_max_runtime_minutes)
            if datetime.now(timezone.utc) >= deadline:
                logger.warning(
                    "watchdog: AURA_MAX_RUNTIME_MINUTES (%s) elapsed since %s; tearing down",
                    self.settings.aura_max_runtime_minutes,
                    state.get("created_at"),
                )
                self.teardown()
                os._exit(3)
