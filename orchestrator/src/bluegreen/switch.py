"""Alias and registry cutovers. Clients keep using the same database name when aliases work."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from neo4j.exceptions import Neo4jError

from bluegreen.cypher import run_autocommit, run_read, run_system
from bluegreen.naming import quote_ident
from bluegreen.redact import redact


@dataclass
class SwitchResult:
    mode: str
    database: str
    admin_path: str | None = None
    alias_error_code: str | None = None
    alias_error_msg: str | None = None


class SwitchStrategy(Protocol):
    def current_target(self, tenant: str) -> str | None: ...

    def switch(self, tenant: str, new_db: str, cycle: str) -> SwitchResult: ...

    def resolve(self, tenant: str) -> str: ...


def _error_code(exc: Neo4jError) -> str:
    gql = getattr(exc, "gql_status", None)
    if gql:
        return str(gql)
    return str(getattr(exc, "code", "") or "")


def _alias_unsupported(exc: Neo4jError) -> bool:
    text = f"{getattr(exc, 'code', '')} {exc}".lower()
    markers = (
        "not supported",
        "unsupported",
        "permission",
        "unauthorized",
        "forbidden",
        "not allowed",
        "security",
        "auth",
    )
    return any(marker in text for marker in markers)


class AliasSwitch:
    def __init__(self, driver):
        self.driver = driver
        self._yield_ok: bool | None = None

    def current_target(self, tenant: str) -> str | None:
        alias = tenant
        if self._yield_ok is not False:
            try:
                records, _ = run_system(
                    self.driver,
                    "SHOW ALIASES FOR DATABASE YIELD name, database WHERE name = $alias "
                    "RETURN name, database",
                    {"alias": alias},
                    timeout=60,
                )
                self._yield_ok = True
                for record in records:
                    if record["name"] == alias:
                        return record["database"]
            except Neo4jError:
                self._yield_ok = False
        records, _ = run_system(self.driver, "SHOW ALIASES FOR DATABASE", timeout=60)
        for record in records:
            name = record["name"] if "name" in record.keys() else None
            database = record["database"] if "database" in record.keys() else None
            if name == alias:
                return database
        return None

    def switch(self, tenant: str, new_db: str, cycle: str) -> SwitchResult:
        del cycle
        current = self.current_target(tenant)
        alias = quote_ident(tenant)
        target = quote_ident(new_db)
        if current is None:
            query = f"CREATE ALIAS {alias} FOR DATABASE {target}"
        else:
            query = f"ALTER ALIAS {alias} SET DATABASE TARGET {target}"
        _, path = run_system(self.driver, query, timeout=60)
        return SwitchResult(mode="alias", database=new_db, admin_path=path)

    def resolve(self, tenant: str) -> str:
        return tenant


class RegistrySwitch:
    def __init__(self, driver):
        self.driver = driver
        self._constraint_ready = False

    def _ensure_constraint(self) -> None:
        if self._constraint_ready:
            return
        run_autocommit(
            self.driver,
            "neo4j",
            "CREATE CONSTRAINT tenant_route_tenant IF NOT EXISTS "
            "FOR (n:TenantRoute) REQUIRE n.tenant IS UNIQUE",
            timeout=120,
        )
        self._constraint_ready = True

    def current_target(self, tenant: str) -> str | None:
        self._ensure_constraint()
        rows = run_read(
            self.driver,
            "neo4j",
            "MATCH (r:TenantRoute {tenant: $tenant}) RETURN r.database AS database",
            {"tenant": tenant},
        )
        if not rows or rows[0]["database"] is None:
            return None
        return rows[0]["database"]

    def switch(self, tenant: str, new_db: str, cycle: str) -> SwitchResult:
        self._ensure_constraint()
        run_autocommit(
            self.driver,
            "neo4j",
            "MERGE (r:TenantRoute {tenant: $tenant}) "
            "SET r.database = $database, r.cycle = $cycle, r.switchedAt = datetime()",
            {"tenant": tenant, "database": new_db, "cycle": cycle},
            timeout=60,
        )
        return SwitchResult(mode="registry", database=new_db, admin_path="autocommit")

    def resolve(self, tenant: str) -> str:
        target = self.current_target(tenant)
        if not target:
            raise RuntimeError(f"no TenantRoute for {tenant}")
        return target


class AutoSwitch:
    """Try an alias. Fall back to the registry only on a permission or support error."""

    def __init__(self, driver):
        self.alias = AliasSwitch(driver)
        self.registry = RegistrySwitch(driver)
        self._forced: SwitchStrategy | None = None

    def current_target(self, tenant: str) -> str | None:
        if isinstance(self._forced, RegistrySwitch):
            return self.registry.current_target(tenant)
        try:
            return self.alias.current_target(tenant)
        except Neo4jError as exc:
            if _alias_unsupported(exc):
                self._forced = self.registry
                return self.registry.current_target(tenant)
            raise

    def switch(self, tenant: str, new_db: str, cycle: str) -> SwitchResult:
        if isinstance(self._forced, RegistrySwitch):
            return self.registry.switch(tenant, new_db, cycle)
        try:
            return self.alias.switch(tenant, new_db, cycle)
        except Neo4jError as exc:
            if not _alias_unsupported(exc):
                raise
            self._forced = self.registry
            result = self.registry.switch(tenant, new_db, cycle)
            result.alias_error_code = _error_code(exc)
            result.alias_error_msg = redact(str(exc))
            return result

    def resolve(self, tenant: str) -> str:
        if isinstance(self._forced, RegistrySwitch):
            return self.registry.resolve(tenant)
        return self.alias.resolve(tenant)


def make_switch(driver, mode: str, alias_supported: bool | None):
    if mode == "registry" or (mode == "auto" and alias_supported is False):
        return RegistrySwitch(driver)
    if mode == "alias" or (mode == "auto" and alias_supported is True):
        return AliasSwitch(driver)
    return AutoSwitch(driver)
