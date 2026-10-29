import pytest

from bluegreen.config import Settings
from bluegreen.errors import ConfigError
from bluegreen.naming import (
    alias_name,
    db_name,
    next_cycle,
    prior_cycle,
    quote_ident,
    validate_tenant,
)


def test_accepts_acme():
    assert validate_tenant("acme") == "acme"
    assert db_name("acme", "c0002") == "acme-c0002"
    assert alias_name("acme") == "acme"
    assert quote_ident("acme-c0002") == "`acme-c0002`"


def test_rejects_underscore_and_trailing_dash():
    with pytest.raises(ValueError):
        validate_tenant("acme_corp")
    with pytest.raises(ValueError):
        validate_tenant("acme-")
    with pytest.raises(ValueError):
        validate_tenant("acme.")
    with pytest.raises(ValueError):
        validate_tenant("Neo4j")
    with pytest.raises(ValueError):
        validate_tenant("neo4j")
    with pytest.raises(ValueError):
        validate_tenant("ab")


def test_length_bounds():
    assert validate_tenant("abc") == "abc"
    assert validate_tenant("a" + ("b" * 30) + "c") == "a" + ("b" * 30) + "c"
    with pytest.raises(ValueError):
        validate_tenant("a" + ("b" * 31) + "c")


def test_cycles():
    assert next_cycle(None) == "c0001"
    assert next_cycle("c0002") == "c0003"
    assert prior_cycle("c0001") is None
    assert prior_cycle("c0002") == "c0001"
    with pytest.raises(ValueError):
        next_cycle("c9999")
    with pytest.raises(ValueError):
        db_name("acme", "cycle-2")


def test_capacity_check_counts_neo4j():
    tenants = ",".join(f"t{i:02d}" for i in range(6))
    with pytest.raises((ValueError, ConfigError)):
        Settings(
            tenants=tenants,
            aura_memory="1GB",
            aura_client_id="cid",
            aura_client_secret="csecret",
            neo4j_tag="2026.10.1",
        )
