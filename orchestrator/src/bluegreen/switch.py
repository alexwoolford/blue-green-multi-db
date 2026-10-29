"""Alias cutover. Clients keep using the same database name: the alias."""

from __future__ import annotations

from dataclasses import dataclass

from bluegreen.cypher import run_system
from bluegreen.naming import quote_ident


@dataclass
class SwitchResult:
    mode: str
    database: str
    admin_path: str | None = None
    previous: str | None = None


class AliasSwitch:
    def __init__(self, driver):
        self.driver = driver

    def current_target(self, tenant: str) -> str | None:
        records, _ = run_system(self.driver, "SHOW ALIASES FOR DATABASE", timeout=60)
        for record in records:
            name = record["name"] if "name" in record.keys() else None
            database = record["database"] if "database" in record.keys() else None
            if name == tenant:
                return database
        return None

    def switch(self, tenant: str, new_db: str) -> SwitchResult:
        current = self.current_target(tenant)
        alias = quote_ident(tenant)
        target = quote_ident(new_db)
        if current is None:
            query = f"CREATE ALIAS {alias} FOR DATABASE {target}"
        else:
            query = f"ALTER ALIAS {alias} SET DATABASE TARGET {target}"
        _, path = run_system(self.driver, query, timeout=60)
        return SwitchResult(mode="alias", database=new_db, admin_path=path, previous=current)

    def resolve(self, tenant: str) -> str:
        return tenant
