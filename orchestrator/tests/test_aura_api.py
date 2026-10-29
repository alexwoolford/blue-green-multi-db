import logging

import httpx
import pytest

from bluegreen.aura_api import AuraClient
from bluegreen.config import Settings
from bluegreen.errors import ApiError, ConfigError


def _settings(**kwargs) -> Settings:
    values = {
        "aura_client_id": "test-client",
        "aura_client_secret": "test-secret-value",
        "aura_api_base": "https://api.neo4j.io",
        "aura_region": "europe-west1",
        "aura_org_id": "",
        "aura_project_id": "",
        "tenants": "acme",
        "neo4j_tag": "2026.10.1",
        "aura_memory": "2GB",
        "neo4j_local_password": "change-me-local",
    }
    values.update(kwargs)
    return Settings(**values)


def test_token_is_cached(respx_mock):
    token = respx_mock.post("https://api.neo4j.io/oauth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
    )
    instances = respx_mock.get("https://api.neo4j.io/v1/instances").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    client = AuraClient(_settings())
    client.list_instances_v1()
    client.list_instances_v1()
    assert token.call_count == 1
    assert instances.call_count == 2


def test_short_token_refreshes_before_expiry(respx_mock):
    token = respx_mock.post("https://api.neo4j.io/oauth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 60})
    )
    respx_mock.get("https://api.neo4j.io/v1/instances").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    client = AuraClient(_settings())
    client.list_instances_v1()
    client.list_instances_v1()
    assert token.call_count == 2


def test_refresh_once_on_expired_token(respx_mock):
    token = respx_mock.post("https://api.neo4j.io/oauth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
    )
    respx_mock.get("https://api.neo4j.io/v1/instances").mock(
        side_effect=[
            httpx.Response(403, json={"error": "invalid_token", "error_description": "expired"}),
            httpx.Response(200, json={"data": []}),
        ]
    )
    client = AuraClient(_settings())
    assert client.list_instances_v1() == {"data": []}
    assert token.call_count == 2


def test_second_403_is_not_retried_again(respx_mock):
    respx_mock.post("https://api.neo4j.io/oauth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
    )
    respx_mock.get("https://api.neo4j.io/v1/instances").mock(
        return_value=httpx.Response(403, json={"error": "invalid_token"})
    )
    client = AuraClient(_settings())
    with pytest.raises(ApiError) as caught:
        client.list_instances_v1()
    assert caught.value.error_code == "403"


def test_retry_after_zero_on_429(respx_mock):
    respx_mock.post("https://api.neo4j.io/oauth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
    )
    instances = respx_mock.get("https://api.neo4j.io/v1/instances").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}, json={"error": "slow"}),
            httpx.Response(200, json={"data": [{"id": "abc"}]}),
        ]
    )
    client = AuraClient(_settings())
    payload = client.list_instances_v1()
    assert payload["data"][0]["id"] == "abc"
    assert instances.call_count == 2


def test_logs_do_not_include_secrets(respx_mock, caplog):
    caplog.set_level(logging.INFO, logger="bluegreen.aura")
    respx_mock.post("https://api.neo4j.io/oauth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
    )
    respx_mock.get("https://api.neo4j.io/v1/instances").mock(
        return_value=httpx.Response(
            200,
            json={"data": [], "password": "super-secret-password"},
        )
    )
    client = AuraClient(_settings())
    client.list_instances_v1()
    text = caplog.text
    assert "test-secret-value" not in text
    assert "super-secret-password" not in text
    assert "POST /oauth/token 200" in text


def test_ambiguous_org_does_not_pick(respx_mock):
    respx_mock.post("https://api.neo4j.io/oauth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
    )
    respx_mock.get("https://api.neo4j.io/v2beta1/organizations").mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": "o1", "name": "one"}, {"id": "o2", "name": "two"}]},
        )
    )
    client = AuraClient(_settings())
    with pytest.raises(ConfigError):
        client.resolve_org_project()


def test_configured_project_skips_project_discovery(respx_mock):
    respx_mock.post("https://api.neo4j.io/oauth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
    )
    respx_mock.get("https://api.neo4j.io/v2beta1/organizations").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "org-1", "name": "only"}]})
    )
    client = AuraClient(_settings(aura_project_id="proj-set"))
    assert client.resolve_org_project() == ("org-1", "proj-set")


def test_configured_project_identifies_one_org(respx_mock):
    respx_mock.post("https://api.neo4j.io/oauth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
    )
    respx_mock.get("https://api.neo4j.io/v2beta1/organizations").mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": "o1", "name": "one"}, {"id": "o2", "name": "two"}]},
        )
    )
    respx_mock.get("https://api.neo4j.io/v2beta1/organizations/o1/projects").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "other", "name": "other"}]})
    )
    respx_mock.get("https://api.neo4j.io/v2beta1/organizations/o2/projects").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "proj-set", "name": "mine"}]})
    )
    client = AuraClient(_settings(aura_project_id="proj-set"))
    assert client.resolve_org_project() == ("o2", "proj-set")


def test_single_org_and_project(respx_mock):
    respx_mock.post("https://api.neo4j.io/oauth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
    )
    respx_mock.get("https://api.neo4j.io/v2beta1/organizations").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "org-1", "name": "only"}]})
    )
    respx_mock.get("https://api.neo4j.io/v2beta1/organizations/org-1/projects").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "proj-1", "name": "only"}]})
    )
    client = AuraClient(_settings())
    assert client.resolve_org_project() == ("org-1", "proj-1")
