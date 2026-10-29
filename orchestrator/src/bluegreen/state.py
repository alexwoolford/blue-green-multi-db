"""JSON state under work/state, always mode 0600."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def state_dir(work: Path) -> Path:
    path = work / "state"
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return raw


def aura_path(work: Path) -> Path:
    return state_dir(work) / "aura.json"


def routes_path(work: Path) -> Path:
    return state_dir(work) / "routes.json"


def load_aura(work: Path) -> dict[str, Any] | None:
    return read_json(aura_path(work))


def save_aura(work: Path, payload: dict[str, Any]) -> None:
    write_json(aura_path(work), payload)


def update_aura(work: Path, **changes: Any) -> dict[str, Any]:
    current = load_aura(work) or {}
    current.update(changes)
    save_aura(work, current)
    return current


def load_routes(work: Path) -> dict[str, Any]:
    return read_json(routes_path(work)) or {"local": {}, "aura": {}}


def save_route(work: Path, target: str, tenant: str, route: dict[str, Any]) -> None:
    routes = load_routes(work)
    routes.setdefault(target, {})
    routes[target][tenant] = route
    write_json(routes_path(work), routes)
