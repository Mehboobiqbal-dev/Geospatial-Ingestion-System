"""
Runtime configuration for the Thuringia water protection zone ingestion.

All settings come from environment variables (or a local ``.env``).
The WFS settings never need credentials; the staging database URL is
only required by commands that touch the database, so installing
dependencies and running unit tests works without any secrets.

The staging database deliberately uses its own variable
(``IRIS_STAGING_DATABASE_URL``) instead of the platform-wide
``DATABASE_URL``, which points at the iris_core application database.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic import SecretStr
from pydantic import field_validator
from pydantic_settings import BaseSettings
from pydantic_settings import SettingsConfigDict

# The WFS of the TLUBN dataset "Wasserschutzgebiete (WSG), Heilquellen-
# schutzgebiete (HQSG)" as registered in the Thuringian metadata catalogue
# (GeoMIS.Th record c030866f-e305-4ba0-b96d-768cb02b550c). The endpoint named
# in the assignment, https://geoportal.thueringen.de/wfs/wsg (layer
# th_wsg_zonen), answers scripts with a CAPTCHA and browsers with HTTP 404;
# it can still be selected with WSG_WFS_URL / WSG_LAYER.
DEFAULT_WFS_URL = "https://www.geoproxy.geoportal-th.de/geoproxy/services/wsg_hqsg_wfs"
DEFAULT_LAYER = "tmlnu:WASSER_UND_HEILQUELLENSCHUTZGEBIETE"
DEFAULT_STATE_CODE = "TH"
SUPPORTED_WFS_VERSIONS = ("2.0.0", "1.1.0")


# ==========================================================
# WFS source
# ==========================================================


class WfsSettings(BaseSettings):
    """
    Source endpoint, request limits and geometry policy.

    Environment variables use the ``WSG_`` prefix, for example
    ``WSG_PAGE_SIZE=500``.
    """

    model_config = SettingsConfigDict(
        env_prefix="WSG_",
        env_file=".env",
        extra="ignore",
    )

    wfs_url: str = DEFAULT_WFS_URL
    layer: str = DEFAULT_LAYER
    state_code: str = DEFAULT_STATE_CODE
    accept_versions: str = ",".join(SUPPORTED_WFS_VERSIONS)

    # HTTP behaviour
    connect_timeout_s: float = Field(default=10.0, gt=0, le=120)
    read_timeout_s: float = Field(default=120.0, gt=0, le=900)
    max_retries: int = Field(default=4, ge=0, le=10)
    backoff_base_s: float = Field(default=2.0, gt=0, le=60)
    backoff_max_s: float = Field(default=60.0, gt=0, le=600)
    min_request_interval_s: float = Field(default=1.0, ge=0, le=60)
    max_response_bytes: int = Field(default=200 * 1024 * 1024, gt=0)
    user_agent: str = "IRIS-WSG-TH/0.1 (staging ingestion of water protection zones)"

    # Retrieval bounds
    page_size: int = Field(default=500, ge=1, le=10_000)
    max_features: int = Field(default=100_000, ge=1)
    max_pages: int = Field(default=1_000, ge=1)

    # Mapping and geometry policy
    mapping_file: Path | None = None
    allow_geometry_repair: bool = True
    max_repair_area_change_ratio: float = Field(default=0.001, ge=0, le=0.5)

    @field_validator("state_code")
    @classmethod
    def _state_code_upper(cls, value: str) -> str:
        value = value.strip().upper()
        if len(value) != 2 or not value.isalpha():
            raise ValueError("state_code must be a two-letter code such as 'TH'")
        return value

    @field_validator("wfs_url")
    @classmethod
    def _https_only(cls, value: str) -> str:
        if not value.lower().startswith("https://"):
            raise ValueError("wfs_url must use https://")
        return value

    @property
    def versions(self) -> tuple[str, ...]:
        requested = tuple(v.strip() for v in self.accept_versions.split(",") if v.strip())
        unsupported = [v for v in requested if v not in SUPPORTED_WFS_VERSIONS]
        if unsupported or not requested:
            raise ValueError(f"Unsupported WFS version(s) {unsupported}; supported: {SUPPORTED_WFS_VERSIONS}")
        return requested


# ==========================================================
# Staging database
# ==========================================================


class StagingDatabaseSettings(BaseSettings):
    """
    Connection settings for the isolated staging database.

    ``IRIS_ENVIRONMENT`` must be one of development, test or staging;
    anything else (notably production) is refused by the guard in
    ``app.database.staging.guard`` before a connection is opened.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
    )

    database_url: SecretStr | None = Field(
        default=None,
        validation_alias="IRIS_STAGING_DATABASE_URL",
    )
    environment: str | None = Field(
        default=None,
        validation_alias="IRIS_ENVIRONMENT",
    )
    schema_name: str = Field(
        default="iris_staging",
        validation_alias="IRIS_STAGING_SCHEMA",
    )
    allowed_hosts: str | None = Field(
        default=None,
        validation_alias="IRIS_STAGING_ALLOWED_HOSTS",
    )
    batch_size: int = Field(
        default=500,
        ge=1,
        le=10_000,
        validation_alias="IRIS_STAGING_BATCH_SIZE",
    )

    @property
    def allowed_host_set(self) -> frozenset[str] | None:
        if not self.allowed_hosts:
            return None
        return frozenset(h.strip().lower() for h in self.allowed_hosts.split(",") if h.strip())
