"""Tenant, cycle, database, and alias names. Database names follow Neo4j F21."""

from __future__ import annotations

import re

# 3-32 chars: one leading letter, 1-30 middle chars, one trailing letter or digit.
_TENANT_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}[a-z0-9]$")
_CYCLE_RE = re.compile(r"^c\d{4}$")
_DB_RE = re.compile(r"^[a-z][a-z0-9.-]{1,61}[a-z0-9]$")

RESERVED_DATABASES = frozenset({"neo4j", "system"})


def validate_tenant(name: str) -> str:
    if not isinstance(name, str) or not _TENANT_RE.fullmatch(name):
        raise ValueError(
            f"invalid tenant {name!r}: lowercase, 3-32 chars, letters, digits and dashes, "
            "no underscores, and no trailing dash"
        )
    if name in RESERVED_DATABASES:
        raise ValueError(f"tenant {name!r} is reserved")
    return name


def validate_cycle(cycle: str) -> str:
    if not _CYCLE_RE.fullmatch(cycle):
        raise ValueError(f"invalid cycle {cycle!r}: expected cNNNN, for example c0001")
    return cycle


def db_name(tenant: str, cycle: str) -> str:
    validate_tenant(tenant)
    validate_cycle(cycle)
    name = f"{tenant}-{cycle}"
    if not _DB_RE.fullmatch(name) or "_" in name or name.endswith(".") or name.endswith("-"):
        raise ValueError(f"invalid database name {name!r}")
    if name in RESERVED_DATABASES:
        raise ValueError(f"database name {name!r} is reserved")
    return name


def alias_name(tenant: str) -> str:
    return validate_tenant(tenant)


def cycle_number(cycle: str) -> int:
    validate_cycle(cycle)
    return int(cycle[1:])


def format_cycle(number: int) -> str:
    if number < 1 or number > 9999:
        raise ValueError(f"cycle number {number} is out of range")
    return f"c{number:04d}"


def next_cycle(current: str | None) -> str:
    if current is None:
        return "c0001"
    return format_cycle(cycle_number(current) + 1)


def prior_cycle(cycle: str) -> str | None:
    number = cycle_number(cycle)
    if number <= 1:
        return None
    return format_cycle(number - 1)


def quote_ident(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"
