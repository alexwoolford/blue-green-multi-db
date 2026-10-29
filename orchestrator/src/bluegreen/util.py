"""Small shared helpers."""

from __future__ import annotations

import re
from datetime import datetime, timezone

_CALVER = re.compile(r"(\d{4})\.(\d{1,2})\.(\d+)")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_calver(value: str) -> tuple[int, int, int]:
    match = _CALVER.search(value or "")
    if not match:
        raise ValueError(f"not a CalVer version: {value!r}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def calver_is_newer(left: str, right: str) -> bool:
    """True when left is a newer CalVer than right. Suffixes are ignored."""
    return parse_calver(left) > parse_calver(right)
