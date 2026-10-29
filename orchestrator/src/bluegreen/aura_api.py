"""Aura API client. Every path lives in PATHS so a wrong guess is easy to correct."""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Callable

import httpx

from bluegreen.config import Settings
from bluegreen.errors import ApiError, ConfigError
from bluegreen.redact import redact
from bluegreen.util import utc_now

logger = logging.getLogger("bluegreen.aura")

# Paths from the Aura API docs (https://neo4j.com/docs/aura/api/overview/) and the multiple-databases
# docs, all exercised by the live run. Do not add paths that have not been checked against a live instance.
PATHS = {
    "token": "/oauth/token",
    "organizations": "/v2beta1/organizations",
    "projects": "/v2beta1/organizations/{organization_id}/projects",
    "v1_instances": "/v1/instances",
    "instances": "/v2beta1/organizations/{organization_id}/projects/{project_id}/instances",
    "instance": "/v2beta1/organizations/{organization_id}/projects/{project_id}/instances/{instance_id}",
    "databases": (
        "/v2beta1/organizations/{organization_id}/projects/{project_id}/instances/{instance_id}/databases"
    ),
    "database": (
        "/v2beta1/organizations/{organization_id}/projects/{project_id}"
        "/instances/{instance_id}/databases/{database_id}"
    ),
}

_RETRY_STATUS = {429, 502, 503, 504}
_BACKOFF = (1, 2, 4, 8, 16)


def unwrap(payload: Any) -> Any:
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def format_path(name: str, **values: str) -> str:
    return PATHS[name].format(**values)


class AuraClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._token: str | None = None
        self._token_deadline = 0.0
        self._http = httpx.Client(base_url=settings.aura_api_base.rstrip("/"), timeout=30.0)
        self.org_id = settings.aura_org_id.strip()
        self.project_id = settings.aura_project_id.strip()

    def close(self) -> None:
        self._http.close()

    def _delay(self, base: float) -> float:
        if base <= 0:
            return 0.0
        return base * (0.5 + random.random() / 2)

    def _log(self, method: str, path: str, status: int, started: float, headers: httpx.Headers) -> None:
        request_id = headers.get("x-request-id") or headers.get("request-id") or ""
        logger.info(
            "%s %s %s %.3fs %s",
            method,
            path,
            status,
            time.perf_counter() - started,
            request_id,
        )

    def _send(self, method: str, path: str, *, json_body: Any = None, auth: bool) -> httpx.Response:
        headers = {}
        if auth:
            if not self._token:
                raise ApiError("missing bearer token", error_code="no_token")
            headers["Authorization"] = f"Bearer {self._token}"
        started = time.perf_counter()
        response = self._http.request(method, path, json=json_body, headers=headers)
        self._log(method, path, response.status_code, started, response.headers)
        return response

    def _with_retries(self, send: Callable[[], httpx.Response]) -> httpx.Response:
        last: httpx.Response | None = None
        for attempt in range(5):
            try:
                response = send()
            except httpx.TransportError as exc:
                if attempt == 4:
                    raise ApiError(redact(str(exc)), error_code="transport") from exc
                time.sleep(self._delay(_BACKOFF[attempt]))
                continue
            if response.status_code in _RETRY_STATUS and attempt < 4:
                last = response
                retry_after = response.headers.get("Retry-After")
                if retry_after and retry_after.replace(".", "", 1).isdigit():
                    delay = float(retry_after)
                else:
                    delay = self._delay(_BACKOFF[attempt])
                time.sleep(delay)
                continue
            return response
        assert last is not None
        return last

    @staticmethod
    def _token_rejected(response: httpx.Response) -> bool:
        if response.status_code != 403:
            return False
        text = response.text.lower()
        return "invalid_token" in text or "expired" in text

    def _fetch_token(self) -> None:
        started = time.perf_counter()
        try:
            response = self._http.post(
                PATHS["token"],
                data={"grant_type": "client_credentials"},
                auth=(self.settings.aura_client_id, self.settings.aura_client_secret),
            )
        except httpx.TransportError as exc:
            raise ApiError(redact(str(exc)), error_code="transport") from exc
        self._log("POST", PATHS["token"], response.status_code, started, response.headers)
        if response.status_code != 200:
            raise ApiError(
                f"token request failed: {redact(response.text)[:500]}",
                error_code=str(response.status_code),
            )
        body = response.json()
        token = body.get("access_token")
        if not token:
            raise ApiError("token response had no access_token", error_code="token")
        expires_in = int(body.get("expires_in") or 3600)
        # Refresh five minutes early. A short-lived token is refreshed on the next call.
        self._token = str(token)
        self._token_deadline = time.monotonic() + max(0, expires_in - 300)

    def _ensure_token(self) -> None:
        if self._token is None or time.monotonic() >= self._token_deadline:
            self._fetch_token()

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        ok: tuple[int, ...] = (200, 201, 202, 204),
        auth: bool = True,
    ) -> httpx.Response:
        if auth:
            self._ensure_token()
        response = self._with_retries(lambda: self._send(method, path, json_body=json_body, auth=auth))
        if auth and self._token_rejected(response):
            self._token = None
            self._token_deadline = 0.0
            self._ensure_token()
            response = self._with_retries(lambda: self._send(method, path, json_body=json_body, auth=auth))
        if response.status_code not in ok:
            raise ApiError(
                f"{method} {path} -> {response.status_code}: {redact(response.text)[:800]}",
                error_code=str(response.status_code),
            )
        return response

    def get_json(self, path: str, *, ok: tuple[int, ...] = (200,)) -> Any:
        response = self.request("GET", path, ok=ok)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def resolve_org_project(self) -> tuple[str, str]:
        if self.org_id and self.project_id:
            return self.org_id, self.project_id
        if not self.org_id:
            orgs = self._list_organizations()
            if len(orgs) == 1:
                self.org_id = str(orgs[0].get("id"))
            elif self.project_id:
                matches = [
                    org
                    for org in orgs
                    if any(
                        str(project.get("id")) == self.project_id
                        for project in self._list_projects(str(org.get("id")))
                    )
                ]
                if len(matches) != 1:
                    _print_choices("organizations", orgs)
                    raise ConfigError(
                        "AURA_PROJECT_ID did not identify exactly one organization. "
                        "Set AURA_ORG_ID. Refusing to pick one."
                    )
                self.org_id = str(matches[0].get("id"))
            else:
                _print_choices("organizations", orgs)
                raise ConfigError(
                    "Aura returned more than one organization. Set AURA_ORG_ID. Refusing to pick one."
                )
        if not self.project_id:
            project_path = format_path("projects", organization_id=self.org_id)
            try:
                payload = self.get_json(project_path)
            except ApiError as exc:
                if exc.error_code == "404":
                    raise ConfigError(
                        "Project discovery returned 404. Set AURA_PROJECT_ID from the Aura console."
                    ) from exc
                raise
            projects = _as_list(unwrap(payload))
            if len(projects) != 1:
                _print_choices("projects", projects)
                raise ConfigError(
                    "Aura returned more than one project. Set AURA_PROJECT_ID. Refusing to pick one."
                )
            self.project_id = str(projects[0].get("id"))
        return self.org_id, self.project_id

    def _list_organizations(self) -> list[dict[str, Any]]:
        try:
            payload = self.get_json(PATHS["organizations"])
        except ApiError as exc:
            if exc.error_code == "404":
                raise ConfigError(
                    "GET /v2beta1/organizations returned 404. Copy AURA_ORG_ID and AURA_PROJECT_ID "
                    "from the Aura console (organization and project URLs) and set them in .env."
                ) from exc
            raise
        return _as_list(unwrap(payload))

    def _list_projects(self, organization_id: str) -> list[dict[str, Any]]:
        project_path = format_path("projects", organization_id=organization_id)
        try:
            payload = self.get_json(project_path)
        except ApiError as exc:
            if exc.error_code in {"403", "404"}:
                return []
            raise
        return _as_list(unwrap(payload))

    def _ids(self) -> dict[str, str]:
        org, project = self.resolve_org_project()
        return {"organization_id": org, "project_id": project}

    def list_instances_v1(self) -> Any:
        return self.get_json(PATHS["v1_instances"])

    def list_instances(self) -> list[dict[str, Any]]:
        path = format_path("instances", **self._ids())
        return _as_list(unwrap(self.get_json(path)))

    def create_instance(self, body: dict[str, Any]) -> dict[str, Any]:
        path = format_path("instances", **self._ids())
        response = self.request("POST", path, json_body=body, ok=(200, 201, 202))
        data = unwrap(response.json())
        if not isinstance(data, dict):
            raise ApiError("create instance response was not an object", error_code="shape")
        return data

    def get_instance(self, instance_id: str, *, allow_missing: bool = False) -> dict[str, Any] | None:
        path = format_path("instance", instance_id=instance_id, **self._ids())
        ok = (200, 404) if allow_missing else (200,)
        response = self.request("GET", path, ok=ok)
        if response.status_code == 404:
            return None
        data = unwrap(response.json())
        if not isinstance(data, dict):
            raise ApiError("get instance response was not an object", error_code="shape")
        return data

    def delete_instance(self, instance_id: str) -> None:
        path = format_path("instance", instance_id=instance_id, **self._ids())
        self.request("DELETE", path, ok=(200, 202, 204, 404))

    def create_database(self, instance_id: str, name: str) -> dict[str, Any]:
        path = format_path("databases", instance_id=instance_id, **self._ids())
        response = self.request("POST", path, json_body={"name": name}, ok=(200, 201, 202))
        data = unwrap(response.json())
        if not isinstance(data, dict):
            raise ApiError("create database response was not an object", error_code="shape")
        return data

    def list_databases(self, instance_id: str) -> list[dict[str, Any]]:
        path = format_path("databases", instance_id=instance_id, **self._ids())
        return _as_list(unwrap(self.get_json(path)))

    def delete_database(self, instance_id: str, database_id: str) -> None:
        path = format_path(
            "database",
            instance_id=instance_id,
            database_id=database_id,
            **self._ids(),
        )
        self.request("DELETE", path, ok=(200, 202, 204, 404))


def wait_for(
    fn: Callable[[], Any],
    predicate: Callable[[Any], bool],
    timeout_s: float,
    interval_s: float,
    describe: str,
) -> list[dict[str, str]]:
    transitions: list[dict[str, str]] = []
    last: Any = object()
    deadline = time.monotonic() + timeout_s
    while True:
        value = fn()
        if value != last:
            observed = value if isinstance(value, str) else repr(value)
            transitions.append({"at": utc_now(), "value": observed})
            logger.info("%s -> %s", describe, observed)
            last = value
        if predicate(value):
            return transitions
        if time.monotonic() >= deadline:
            raise ApiError(
                f"timed out after {timeout_s:.0f}s waiting for {describe}; last value {value!r}",
                error_code="timeout",
            )
        time.sleep(interval_s)


def _as_list(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    raise ApiError(f"expected a list, got {type(value).__name__}", error_code="shape")


def _print_choices(kind: str, rows: list[dict[str, Any]]) -> None:
    print(f"Multiple Aura {kind}:")
    for row in rows:
        print(f"  id={row.get('id')} name={row.get('name')}")
