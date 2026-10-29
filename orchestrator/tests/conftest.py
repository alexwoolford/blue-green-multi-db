import pytest


def pytest_collection_modifyitems(config, items):
    markexpr = config.getoption("-m") or ""
    if "local" in markexpr.split():
        return
    skip = pytest.mark.skip(reason="needs the compose stack; run pytest -m local")
    for item in items:
        if "local" in item.keywords:
            item.add_marker(skip)
