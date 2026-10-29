"""Strip configured secrets from text that might be logged or written to a run."""

from __future__ import annotations

import os

_EXTRA: list[str] = []
_ENV_KEYS = ("AURA_CLIENT_SECRET", "NEO4J_LOCAL_PASSWORD")


def add_secret(value: str | None) -> None:
    if value and len(value) >= 6 and value not in _EXTRA:
        _EXTRA.append(value)


class RedactFormatter:
    """Logging formatter that runs the formatted line through redact()."""

    def __init__(self, fmt: str):
        import logging

        self._formatter = logging.Formatter(fmt)

    def format(self, record) -> str:
        return redact(self._formatter.format(record))


def redact(text: str) -> str:
    if not text:
        return text
    for key in _ENV_KEYS:
        secret = os.environ.get(key)
        if secret and len(secret) >= 6:
            text = text.replace(secret, "***")
    for secret in _EXTRA:
        text = text.replace(secret, "***")
    return text
