"""Driver helpers for system commands, schema, polling, and verification."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any

from neo4j import GraphDatabase, RoutingControl
from neo4j.exceptions import ClientError, Neo4jError, ServiceUnavailable, SessionExpired, TransientError

from bluegreen.errors import VerificationError
from bluegreen.naming import quote_ident
from bluegreen.util import utc_now

_TRANSIENT = (ServiceUnavailable, SessionExpired, TransientError)
_SCHEMA_INDEXES = (
    (["Customer"], ["customerId"]),
    (["Account"], ["accountId"]),
    (["Merchant"], ["merchantId"]),
    (["Snapshot"], ["snapshotId"]),
    (["Account"], ["status"]),
)


def schema_file() -> Path:
    override = os.environ.get("SCHEMA_PATH")
    if override:
        return Path(override)
    packaged = Path("/opt/orchestrator/schema/schema.cypher")
    if packaged.exists():
        return packaged
    return Path(__file__).resolve().parents[2] / "schema" / "schema.cypher"


def connect(uri: str, user: str, password: str):
    return GraphDatabase.driver(uri, auth=(user, password), connection_timeout=30)


def local_driver(settings):
    return connect(settings.neo4j_local_uri, settings.neo4j_local_user, settings.neo4j_local_password)


def run_autocommit(driver, database: str, query: str, parameters: dict | None = None, timeout: float = 400):
    last: Exception | None = None
    for attempt in range(3):
        try:
            with driver.session(database=database) as session:
                result = session.run(query, parameters or {}, timeout=timeout)
                return list(result)
        except _TRANSIENT as exc:
            last = exc
            time.sleep(0.5 * (attempt + 1))
    assert last is not None
    raise last


def run_system(driver, query: str, parameters: dict | None = None, timeout: float = 400) -> tuple[list, str]:
    """Run a command on `system`. Prefer execute_query; on a client error, retry once in an auto-commit session.

    Returns (records, path) where path is 'execute_query' or 'autocommit'. CREATE/DROP DATABASE call
    run_autocommit directly.
    """
    try:
        result = driver.execute_query(query, parameters or {}, database_="system")
        return list(result.records), "execute_query"
    except ClientError as exc:
        try:
            records = run_autocommit(driver, "system", query, parameters, timeout=timeout)
        except Neo4jError:
            raise exc
        return records, "autocommit"


def run_read(driver, database: str, query: str, parameters: dict | None = None):
    result = driver.execute_query(
        query,
        parameters or {},
        database_=database,
        routing_=RoutingControl.READ,
    )
    return list(result.records)


def components(driver) -> dict[str, Any]:
    records = run_read(
        driver,
        "system",
        "CALL dbms.components() YIELD name, versions, edition RETURN name, versions, edition",
    )
    rows = []
    for record in records:
        rows.append(
            {
                "name": record["name"],
                "versions": list(record["versions"]) if record["versions"] is not None else [],
                "edition": record["edition"],
            }
        )
    kernel = next((row for row in rows if row["name"] == "Neo4j Kernel"), rows[0] if rows else {})
    version = ""
    versions = kernel.get("versions") or []
    if versions:
        version = str(versions[0])
    return {"version": version, "edition": kernel.get("edition"), "components": rows}


def show_databases(driver) -> list[dict[str, Any]]:
    try:
        records, _ = run_system(
            driver,
            "SHOW DATABASES YIELD name, currentStatus, aliases RETURN name, currentStatus, aliases",
            timeout=60,
        )
        rows = []
        for record in records:
            aliases = record["aliases"]
            rows.append(
                {
                    "name": record["name"],
                    "currentStatus": record["currentStatus"],
                    "aliases": list(aliases) if aliases is not None else [],
                }
            )
        return _dedupe_databases(rows)
    except Neo4jError:
        records, _ = run_system(
            driver,
            "SHOW DATABASES YIELD name, currentStatus RETURN name, currentStatus",
            timeout=60,
        )
        return _dedupe_databases(
            [
                {"name": record["name"], "currentStatus": record["currentStatus"], "aliases": []}
                for record in records
            ]
        )


def _dedupe_databases(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aura Business Critical returns one SHOW DATABASES row per cluster member."""
    merged: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = str(row["name"])
        current = merged.get(name)
        if current is None:
            merged[name] = {
                "name": name,
                "currentStatus": row["currentStatus"],
                "aliases": list(row.get("aliases") or []),
            }
            continue
        aliases = set(current["aliases"])
        aliases.update(row.get("aliases") or [])
        current["aliases"] = sorted(aliases)
        if current["currentStatus"] != "online" and row["currentStatus"] == "online":
            current["currentStatus"] = "online"
    return list(merged.values())


def database_status(driver, name: str) -> str | None:
    for row in show_databases(driver):
        if row["name"] == name:
            return row["currentStatus"]
    return None


def wait_until_online(driver, name: str, timeout_s: float, interval_s: float = 2) -> list[dict[str, str]]:
    transitions: list[dict[str, str]] = []
    deadline = time.monotonic() + timeout_s
    last = None
    while True:
        status = database_status(driver, name)
        if status != last:
            transitions.append({"at": utc_now(), "status": str(status)})
            last = status
        if status == "online":
            return transitions
        if time.monotonic() >= deadline:
            raise VerificationError(
                f"database {name} did not become online within {timeout_s:.0f}s (last status {status})"
            )
        time.sleep(interval_s)


def wait_until_absent(driver, name: str, timeout_s: float, interval_s: float = 2) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        if database_status(driver, name) is None:
            return
        if time.monotonic() >= deadline:
            raise VerificationError(f"database {name} was still present after {timeout_s:.0f}s")
        time.sleep(interval_s)


def schema_statements() -> list[str]:
    text = schema_file().read_text(encoding="utf-8")
    statements = []
    for chunk in text.split(";"):
        lines = [line for line in chunk.splitlines() if line.strip() and not line.strip().startswith("//")]
        statement = "\n".join(lines).strip()
        if statement:
            statements.append(statement)
    return statements


def apply_schema(driver, database: str) -> None:
    for statement in schema_statements():
        run_autocommit(driver, database, statement, timeout=120)
    run_autocommit(driver, database, "CALL db.awaitIndexes(600)", timeout=700)


def _as_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return tuple()
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value)


def _index_key(labels: Any, properties: Any) -> tuple[tuple[str, ...], tuple[str, ...]]:
    return _as_tuple(labels), _as_tuple(properties)


def verify_database(driver, database: str, manifest: dict[str, Any], tenant: str, cycle: str) -> dict[str, Any]:
    snap_count = run_read(driver, database, "MATCH (s:Snapshot) RETURN count(s) AS n")
    n_snap = int(snap_count[0]["n"]) if snap_count else 0
    if n_snap != 1:
        raise VerificationError(f"{database}: expected exactly 1 Snapshot, found {n_snap}")
    row = run_read(
        driver,
        database,
        "MATCH (s:Snapshot) RETURN s.tenant AS tenant, s.cycle AS cycle",
    )[0]
    if row["tenant"] != tenant or row["cycle"] != cycle:
        raise VerificationError(
            f"{database}: Snapshot has tenant={row['tenant']!r} cycle={row['cycle']!r}, "
            f"expected {tenant!r} {cycle!r}"
        )

    nodes = manifest["nodes"]
    rels = manifest["relationships"]
    for label, expected in nodes.items():
        found = run_read(driver, database, f"MATCH (n:{quote_ident(label)}) RETURN count(n) AS n")
        actual = int(found[0]["n"])
        if actual != int(expected):
            raise VerificationError(f"{database}: {label} count {actual} != manifest {expected}")
    for rel_type, expected in rels.items():
        found = run_read(
            driver,
            database,
            f"MATCH ()-[r:{quote_ident(rel_type)}]->() RETURN count(r) AS n",
        )
        actual = int(found[0]["n"])
        if actual != int(expected):
            raise VerificationError(f"{database}: {rel_type} count {actual} != manifest {expected}")

    indexes = run_read(
        driver,
        database,
        "SHOW INDEXES YIELD labelsOrTypes, properties, state RETURN labelsOrTypes, properties, state",
    )
    online = {
        _index_key(record["labelsOrTypes"], record["properties"])
        for record in indexes
        if str(record["state"]).upper() == "ONLINE"
    }
    missing = []
    for labels, props in _SCHEMA_INDEXES:
        key = (tuple(labels), tuple(props))
        if key not in online:
            missing.append(key)
    if missing:
        raise VerificationError(f"{database}: indexes not ONLINE: {missing}")

    started = time.perf_counter()
    sample = run_read(
        driver,
        database,
        "MATCH (c:Customer)-[:OWNS]->(a:Account)-[:PAID]->(m:Merchant) "
        "RETURN c.customerId AS customer, a.accountId AS account, m.merchantId AS merchant LIMIT 5",
    )
    latency_ms = (time.perf_counter() - started) * 1000
    if not sample:
        raise VerificationError(f"{database}: 2-hop traversal returned no rows")
    return {
        "snapshot_ok": True,
        "sample_rows": len(sample),
        "sample_latency_ms": round(latency_ms, 3),
    }


def read_snapshot_cycle(driver, database: str) -> str | None:
    rows = run_read(driver, database, "MATCH (s:Snapshot) RETURN s.cycle AS cycle")
    if not rows:
        return None
    return rows[0]["cycle"]


def node_count(driver, database: str) -> int:
    rows = run_read(driver, database, "MATCH (n) RETURN count(n) AS n")
    return int(rows[0]["n"]) if rows else 0


def create_database(driver, name: str, wait_seconds: int = 300) -> None:
    query = f"CREATE DATABASE {quote_ident(name)} WAIT {wait_seconds} SECONDS"
    run_autocommit(driver, "system", query, timeout=wait_seconds + 60)


def drop_database(driver, name: str, wait_seconds: int = 300) -> None:
    query = f"DROP DATABASE {quote_ident(name)} IF EXISTS WAIT {wait_seconds} SECONDS"
    run_autocommit(driver, "system", query, timeout=wait_seconds + 60)


def drop_alias(driver, name: str) -> None:
    run_system(driver, f"DROP ALIAS {quote_ident(name)} IF EXISTS FOR DATABASE", timeout=60)


def remove_store(root: Path, database: str) -> None:
    for sub in ("databases", "transactions"):
        path = root / sub / database
        if path.exists():
            shutil.rmtree(path)
