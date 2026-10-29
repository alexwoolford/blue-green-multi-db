"""Pre-flight checks. Help-text assertions confirm the neo4j-admin flags; they do not invent missing flags."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from bluegreen.config import Settings
from bluegreen.cypher import components, local_driver
from bluegreen.errors import ConfigError, OpenQuestionError
from bluegreen.ledger import Ledger

_IMPORT_FLAGS = ("--nodes", "--relationships", "--additional-config", "--overwrite-destination", "--id-type")
_DUMP_FLAGS = ("--to-path", "--additional-config", "--overwrite-destination")
# --to-dbid (added in 2026.07) targets one database on a multi-database instance.
# admin.upload_database passes credentials in NEO4J_USERNAME and NEO4J_PASSWORD.
_UPLOAD_FLAGS = ("--from-path", "--to-uri", "--to-dbid", "--overwrite-destination", "NEO4J_USERNAME", "NEO4J_PASSWORD")


def run_doctor(settings: Settings, ledger: Ledger | None = None, *, aura: bool = False) -> None:
    doctor = settings.work_path() / "doctor"
    doctor.mkdir(parents=True, exist_ok=True)

    version = _capture(["neo4j-admin", "--version"])
    (doctor / "versions.txt").write_text(version + "\n", encoding="utf-8")
    for name, command, flags in (
        ("import-full", ["neo4j-admin", "database", "import", "full", "--help"], _IMPORT_FLAGS),
        ("dump", ["neo4j-admin", "database", "dump", "--help"], _DUMP_FLAGS),
        ("upload", ["neo4j-admin", "database", "upload", "--help"], _UPLOAD_FLAGS),
    ):
        help_text = _capture(command)
        (doctor / f"{name}.help.txt").write_text(help_text, encoding="utf-8")
        _require(name, help_text, flags)

    info = _local_components(settings)
    with (doctor / "versions.txt").open("a", encoding="utf-8") as handle:
        handle.write(f"local_edition: {info.get('edition')}\n")
        handle.write(f"local_version: {info.get('version')}\n")
    edition = str(info.get("edition") or "").lower()
    if "enterprise" not in edition:
        raise ConfigError(f"neo4j-local edition is {info.get('edition')!r}, expected enterprise")
    local_version = str(info.get("version") or "")
    if local_version != settings.neo4j_tag:
        raise ConfigError(f"neo4j-local version {local_version} does not equal NEO4J_TAG {settings.neo4j_tag}")

    if aura:
        _aura_checks(settings, doctor)

    if ledger is not None:
        with ledger.step("doctor", target="local") as details:
            details["local_version"] = local_version
    print(f"doctor ok. output: {doctor}")


def _aura_checks(settings: Settings, doctor: Path) -> None:
    from bluegreen.aura_api import AuraClient

    settings.require_credentials()
    client = AuraClient(settings)
    try:
        org, project = client.resolve_org_project()
        payload = client.list_instances_v1()
        (doctor / "aura-smoke.json").write_text(
            json.dumps({"org_id": org, "project_id": project, "v1_instances_type": type(payload).__name__}, indent=2)
            + "\n",
            encoding="utf-8",
        )
    finally:
        client.close()


def _local_components(settings: Settings) -> dict[str, Any]:
    try:
        driver = local_driver(settings)
    except Exception as exc:
        raise ConfigError(f"could not connect to neo4j-local at {settings.neo4j_local_uri}: {exc}") from exc
    try:
        return components(driver)
    finally:
        driver.close()


def _require(command: str, help_text: str, flags: tuple[str, ...]) -> None:
    missing = [flag for flag in flags if flag not in help_text]
    if missing:
        raise OpenQuestionError(
            f"[upload-flags] `{command}` help is missing {missing}.\n{help_text}"
        )


def _capture(cmd: list[str]) -> str:
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise ConfigError(f"command not found: {cmd[0]}") from exc
    return (completed.stdout or "") + (completed.stderr or "")
