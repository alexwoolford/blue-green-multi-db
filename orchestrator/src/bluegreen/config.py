"""Environment configuration. Fail fast, and never log secrets."""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from bluegreen.errors import ConfigError
from bluegreen.naming import validate_tenant

_CALVER = re.compile(r"^\d{4}\.\d{1,2}\.\d+$")
_MEMORY = re.compile(r"^(\d+(?:\.\d+)?)(GB|MB)$", re.IGNORECASE)
_PREFIX = re.compile(r"^[a-z][a-z0-9-]*[a-z0-9]$")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    aura_client_id: str = ""
    aura_client_secret: str = ""
    aura_api_base: str = "https://api.neo4j.io"
    aura_org_id: str = ""
    aura_project_id: str = ""
    aura_instance_name_prefix: str = "bg-demo"
    aura_cloud_provider: str = "gcp"
    aura_region: str = ""
    aura_memory: str = "2GB"
    aura_instance_type: str = "business-critical"
    aura_max_runtime_minutes: int = 180
    aura_keep_instance: bool = False
    aura_pause_api: str = "v1"
    aura_pause_path_template: str = ""
    aura_resume_path_template: str = ""
    pause_hold_seconds: int = 120
    pause_iterations: int = 0
    neo4j_tag: str = "2026.10.1"
    neo4j_accept_license_agreement: str = "yes"
    neo4j_local_password: str = "change-me-local"
    neo4j_local_uri: str = "neo4j://localhost:7687"
    neo4j_local_user: str = "neo4j"
    tenants: str = "acme,globex,initech"
    scale: float = 1.0
    seed: int = 42
    admin_heap_size: str = "2g"
    switch_mode: str = "auto"
    keep_dumps: bool = False
    import_with_schema: bool = False
    work_dir: str = ""
    toolbox_conf_dir: str = ""

    @model_validator(mode="after")
    def _validate(self) -> "Settings":
        tenants = self.tenant_list
        if not tenants:
            raise ValueError("TENANTS must list at least one tenant")
        if not _CALVER.fullmatch(self.neo4j_tag):
            raise ValueError(f"NEO4J_TAG must be CalVer YYYY.MM.P, got {self.neo4j_tag!r}")
        if self.scale <= 0:
            raise ValueError("SCALE must be greater than 0")
        if self.switch_mode not in {"auto", "alias", "registry"}:
            raise ValueError("SWITCH_MODE must be auto, alias, or registry")
        if self.aura_cloud_provider not in {"gcp", "aws", "azure"}:
            raise ValueError("AURA_CLOUD_PROVIDER must be gcp, aws, or azure")
        if self.aura_pause_api not in {"v1", "custom"}:
            raise ValueError("AURA_PAUSE_API must be v1 or custom")
        if self.aura_pause_api == "custom" and (
            not self.aura_pause_path_template or not self.aura_resume_path_template
        ):
            raise ValueError("AURA_PAUSE_API=custom requires pause and resume path templates")
        if not _PREFIX.fullmatch(self.aura_instance_name_prefix):
            raise ValueError(
                "AURA_INSTANCE_NAME_PREFIX must be lowercase letters, digits, and dashes"
            )
        if self.aura_max_runtime_minutes < 1:
            raise ValueError("AURA_MAX_RUNTIME_MINUTES must be at least 1")
        if self.pause_iterations < 0:
            raise ValueError("PAUSE_ITERATIONS must be 0 (pause disabled) or more")
        need = 2 * len(tenants) + 1
        allowed = 5 * self.memory_gb
        if need > allowed:
            raise ValueError(
                f"capacity: {len(tenants)} tenants need {need} database slots during a swap "
                f"(2 per tenant plus the default neo4j database) but {self.aura_memory} allows "
                f"{allowed:g}. Use fewer tenants or more memory."
            )
        return self

    @property
    def tenant_list(self) -> list[str]:
        names = [part.strip() for part in self.tenants.split(",") if part.strip()]
        return [validate_tenant(name) for name in names]

    @property
    def memory_gb(self) -> float:
        match = _MEMORY.fullmatch(self.aura_memory.strip())
        if not match:
            raise ConfigError("AURA_MEMORY must look like 2GB or 512MB")
        amount = float(match.group(1))
        if match.group(2).upper() == "GB":
            return amount
        return amount / 1024

    def require_region(self) -> str:
        if not self.aura_region.strip():
            raise ConfigError(
                "AURA_REGION is required before Aura commands that create an instance. "
                "Set it to a region where this organization can create Business Critical."
            )
        return self.aura_region.strip()

    def require_credentials(self) -> None:
        if not self.aura_client_id or not self.aura_client_secret:
            raise ConfigError("AURA_CLIENT_ID and AURA_CLIENT_SECRET are required")

    def work_path(self) -> Path:
        if self.work_dir:
            return Path(self.work_dir)
        return Path("work")

    def conf_dir(self) -> Path:
        if self.toolbox_conf_dir:
            return Path(self.toolbox_conf_dir)
        return Path(__file__).resolve().parents[3] / "toolbox" / "conf"


def load_settings() -> Settings:
    try:
        return Settings()
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(str(exc)) from exc
