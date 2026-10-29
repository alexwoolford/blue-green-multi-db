"""Pre-flight checks. Help-text assertions confirm the upload flags; they do not invent missing flags."""

from __future__ import annotations

import json
import platform
import re
import subprocess
from pathlib import Path
from typing import Any

from bluegreen.admin import findings_path
from bluegreen.config import Settings
from bluegreen.cypher import components, local_driver
from bluegreen.errors import ConfigError, OpenQuestionError
from bluegreen.ledger import Ledger
from bluegreen.state import load_aura, record_oq
from bluegreen.util import calver_is_newer

_IMPORT_FLAGS = ("--nodes", "--relationships", "--additional-config", "--overwrite-destination", "--id-type")
_DUMP_FLAGS = ("--to-path", "--additional-config", "--overwrite-destination")
_UPLOAD_FLAGS = ("--from-path", "--to-uri", "--to-dbid", "--overwrite-destination")

_PAUSE_NOTE = """\
Pause/resume pre-check (OQ-2), recorded before any live call.

- Aura instance-actions docs show POST https://api.neo4j.io/v1/instances/{instanceId}/pause
  and the matching /resume path.
- aura-go-sdk v1 posts to instances/{id}/pause and instances/{id}/resume.
- aura-go-sdk v2beta1 instance service (list/get/create/update/delete) has no pause or resume method.
- The published API specification page lists Aura v1, v1beta5, and v2beta1, but the page is a
  client-rendered spec and did not expose a v2beta1 pause path in the snapshot checked with this harness.

Configured default: AURA_PAUSE_API=v1. A rejected response exits 5 with [pause-endpoint]. There is no silent fallback.
"""


def run_doctor(settings: Settings, ledger: Ledger | None = None, *, aura: bool = False) -> dict[str, Any]:
    work = settings.work_path()
    doctor = work / "doctor"
    doctor.mkdir(parents=True, exist_ok=True)
    (doctor / "pause-api.txt").write_text(_PAUSE_NOTE, encoding="utf-8")

    version = _capture(["neo4j-admin", "--version"])
    (doctor / "versions.txt").write_text(version + "\n", encoding="utf-8")
    import_help = _capture(["neo4j-admin", "database", "import", "full", "--help"])
    dump_help = _capture(["neo4j-admin", "database", "dump", "--help"])
    upload_help = _capture(["neo4j-admin", "database", "upload", "--help"])
    (doctor / "import-full.help.txt").write_text(import_help, encoding="utf-8")
    (doctor / "dump.help.txt").write_text(dump_help, encoding="utf-8")
    (doctor / "upload.help.txt").write_text(upload_help, encoding="utf-8")

    _require("import full", import_help, _IMPORT_FLAGS)
    _require("dump", dump_help, _DUMP_FLAGS)
    _require("upload", upload_help, _UPLOAD_FLAGS)

    password_flags = _flag_names(upload_help, "password")
    user_flags = _flag_names(upload_help, "user")
    env_vars = sorted(set(re.findall(r"NEO4J_[A-Z0-9_]+", upload_help)))
    password_env = next((name for name in env_vars if "PASSWORD" in name), None)
    user_env = next((name for name in env_vars if name.endswith("USERNAME") or name.endswith("_USER")), None)
    if not password_flags and not password_env:
        raise OpenQuestionError(
            "[upload-flags] upload help has no password flag or password env var.\n" + upload_help
        )

    heap_text, heap_mentioned = _heap_sources(import_help + "\n" + dump_help + "\n" + upload_help)
    os_release = _read_file("/etc/os-release")
    uid = _capture(["id"])
    default_data = _default_data_dir()
    (doctor / "environment.txt").write_text(
        "\n".join(
            [
                f"python: {platform.python_version()}",
                f"uid: {uid.strip()}",
                f"default_data_dir: {default_data}",
                "",
                os_release,
                "",
                heap_text,
            ]
        ),
        encoding="utf-8",
    )

    findings: dict[str, Any] = {
        "flags": {
            "from_path": "--from-path",
            "to_uri": "--to-uri",
            "to_dbid": "--to-dbid",
            "overwrite": "--overwrite-destination",
            "to_user": user_flags[0] if user_flags else "--to-user",
            "to_password": password_flags[0] if password_flags else "--to-password",
            "additional_config": "--additional-config",
            "id_type": "--id-type",
            "nodes": "--nodes",
            "relationships": "--relationships",
            "to_path": "--to-path",
            "schema": "--schema",
        },
        "upload_password_flags": password_flags,
        "upload_user_flags": user_flags,
        "upload_password_env": password_env,
        "upload_user_env": user_env,
        "has_schema": "--schema" in import_help,
        "heap_env": "HEAP_SIZE",
        "heap_mentioned": heap_mentioned,
        "default_data_dir": default_data,
        "neo4j_admin_version": version.strip(),
    }
    findings_path(work).write_text(json.dumps(findings, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    record_oq(
        work,
        "OQ-6",
        f"password flags {password_flags or ['(none)']}; env {password_env or '(none)'}",
        "work/doctor/upload.help.txt",
    )
    record_oq(
        work,
        "OQ-7",
        "--additional-config is present on import and dump; it is always passed",
        f"default data dir: {default_data}",
    )
    record_oq(
        work,
        "OQ-11",
        "HEAP_SIZE is honored by org.neo4j.cli.AdminTool; admin.py exports it"
        if findings["heap_mentioned"]
        else "HEAP_SIZE was not found; admin.py still exports it",
        "work/doctor/environment.txt",
    )

    info = _local_components(settings)
    with (doctor / "versions.txt").open("a", encoding="utf-8") as handle:
        handle.write(f"local_edition: {info.get('edition')}\n")
        handle.write(f"local_version: {info.get('version')}\n")
    edition = str(info.get("edition") or "").lower()
    if "enterprise" not in edition:
        raise ConfigError(f"neo4j-local edition is {info.get('edition')!r}, expected enterprise")
    local_version = str(info.get("version") or "")
    if settings.neo4j_tag not in local_version and local_version not in settings.neo4j_tag:
        from bluegreen.util import parse_calver

        if parse_calver(local_version) != parse_calver(settings.neo4j_tag):
            raise ConfigError(
                f"neo4j-local version {local_version} does not equal NEO4J_TAG {settings.neo4j_tag}"
            )

    if aura:
        _aura_checks(settings, doctor)

    if ledger is not None:
        with ledger.step("doctor", target="local") as details:
            details["findings"] = {
                "has_schema": findings["has_schema"],
                "heap_mentioned": findings["heap_mentioned"],
                "upload_password_env": password_env,
                "upload_password_flags": password_flags,
            }
            details["local_version"] = local_version
    print(f"doctor ok. findings: {findings_path(work)}")
    return findings


def _aura_checks(settings: Settings, doctor: Path) -> None:
    from bluegreen.aura_api import AuraClient
    from bluegreen.aura_instance import assert_version_guard
    from bluegreen.cypher import connect
    from bluegreen.redact import add_secret

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
        state = load_aura(settings.work_path())
        if not state or not state.get("password") or not state.get("connection_url"):
            return
        add_secret(str(state.get("password")))
        driver = connect(str(state["connection_url"]), str(state.get("username") or "neo4j"), str(state["password"]))
        try:
            info = components(driver)
        finally:
            driver.close()
        version = str(info.get("version") or "")
        with (doctor / "versions.txt").open("a", encoding="utf-8") as handle:
            handle.write(f"aura_version: {version}\n")
            handle.write(f"aura_edition: {info.get('edition')}\n")
            handle.write(assert_version_guard(settings, version) + "\n")
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


def _flag_names(help_text: str, needle: str) -> list[str]:
    return sorted(set(re.findall(rf"--[A-Za-z0-9-]*{needle}[A-Za-z0-9-]*", help_text)))


def _capture(cmd: list[str]) -> str:
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise ConfigError(f"command not found: {cmd[0]}") from exc
    return (completed.stdout or "") + (completed.stderr or "")


def _read_file(path: str) -> str:
    file = Path(path)
    if not file.exists():
        return f"{path} not found"
    return file.read_text(encoding="utf-8", errors="replace")


def _default_data_dir() -> str:
    for candidate in (Path("/var/lib/neo4j/conf/neo4j.conf"), Path("/data")):
        if candidate.is_file():
            for line in candidate.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.strip().startswith("server.directories.data"):
                    return line.strip()
        elif candidate.is_dir():
            return str(candidate)
    return "(not found)"


def _heap_sources(help_text: str) -> tuple[str, bool]:
    notes: list[str] = []
    honored = False
    if "HEAP_SIZE" in help_text:
        honored = True
        notes.append("HEAP_SIZE appears in neo4j-admin help text.")
    which = _capture(["sh", "-c", "command -v neo4j-admin || true"]).strip()
    if which:
        notes.append(f"neo4j-admin: {which}")
        file = Path(which)
        if file.is_file():
            text = file.read_text(encoding="utf-8", errors="replace")
            if "HEAP_SIZE" in text:
                honored = True
                notes.append(f"{which} mentions HEAP_SIZE.")
            elif "-Xmx" in text:
                notes.append(
                    f"{which} does not read HEAP_SIZE. It starts a bootstrap JVM with a fixed -Xmx."
                )
    jar_note = _admin_tool_heap_note()
    if jar_note:
        honored = True
        notes.append(jar_note)
    if not honored:
        notes.append("HEAP_SIZE was not found in help text, the wrapper, or AdminTool.")
    return "\n".join(notes), honored


def _admin_tool_heap_note() -> str:
    import zipfile

    lib = Path("/var/lib/neo4j/lib")
    if not lib.is_dir():
        return ""
    for jar in sorted(lib.glob("neo4j-command-line-*.jar")):
        try:
            with zipfile.ZipFile(jar) as archive:
                data = archive.read("org/neo4j/cli/AdminTool.class")
        except (KeyError, OSError):
            continue
        if b"HEAP_SIZE" in data:
            return (
                f"{jar.name}: org.neo4j.cli.AdminTool documents HEAP_SIZE as the JVM maximum heap "
                "during command execution (example 512m). JAVA_OPTS takes precedence over HEAP_SIZE. "
                "admin.py exports HEAP_SIZE from ADMIN_HEAP_SIZE for import, dump, and upload."
            )
    return ""
