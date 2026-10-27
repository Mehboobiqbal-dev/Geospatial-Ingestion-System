"""
Staging-only write guard.

Every database write in the water protection ingestion goes through
``validate_staging_target`` (before connecting) and
``verify_connection`` (after connecting, inside the write transaction).
The guard refuses, with ``StagingTargetRefused``:

* a missing or non-staging ``IRIS_ENVIRONMENT`` (production is never allowed);
* any schema other than ``iris_staging`` - in particular ``iris_core``;
* non-PostgreSQL URLs and URLs without a database name;
* database names or hosts that look like production (prod, production, live, prd);
* hosts outside ``IRIS_STAGING_ALLOWED_HOSTS`` when that allowlist is set;
* a server whose ``current_database()`` differs from the configured name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.engine import Connection
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from app.database.staging.base import STAGING_SCHEMA
from app.water_protection.config import StagingDatabaseSettings
from app.water_protection.errors import PersistenceError
from app.water_protection.errors import StagingTargetRefused

ALLOWED_ENVIRONMENTS = frozenset({"development", "test", "staging"})
FORBIDDEN_SCHEMAS = frozenset({"iris_core", "public"})
_PRODUCTION_MARKER = re.compile(r"(^|[^a-z0-9])(prod|production|live|prd)([^a-z0-9]|$)", re.IGNORECASE)


@dataclass(frozen=True)
class StagingTarget:
    url: URL
    environment: str
    schema: str
    database: str
    host: str | None

    def describe(self) -> str:
        """Human-readable target without credentials."""
        return f"{self.url.drivername}://{self.host or 'local-socket'}:{self.url.port or 5432}/{self.database} schema={self.schema} env={self.environment}"


def validate_staging_target(settings: StagingDatabaseSettings) -> StagingTarget:
    environment = (settings.environment or "").strip().lower()
    if not environment:
        raise StagingTargetRefused(
            "IRIS_ENVIRONMENT is not set; set it to one of "
            f"{sorted(ALLOWED_ENVIRONMENTS)} for staging writes"
        )
    if environment not in ALLOWED_ENVIRONMENTS:
        raise StagingTargetRefused(
            f"IRIS_ENVIRONMENT={environment!r} is not allowed to write; "
            f"allowed: {sorted(ALLOWED_ENVIRONMENTS)}"
        )

    schema = settings.schema_name.strip()
    if schema in FORBIDDEN_SCHEMAS:
        raise StagingTargetRefused(f"writes to schema {schema!r} are forbidden; only {STAGING_SCHEMA!r} is allowed")
    if schema != STAGING_SCHEMA:
        raise StagingTargetRefused(f"schema {schema!r} is not the staging schema {STAGING_SCHEMA!r}")

    if settings.database_url is None:
        raise StagingTargetRefused("IRIS_STAGING_DATABASE_URL is not set")
    try:
        url = make_url(settings.database_url.get_secret_value())
    except ArgumentError as exc:
        raise StagingTargetRefused("IRIS_STAGING_DATABASE_URL is not a valid database URL") from exc

    if url.get_backend_name() != "postgresql":
        raise StagingTargetRefused(f"backend {url.get_backend_name()!r} is not PostgreSQL")
    if url.drivername == "postgresql":
        url = url.set(drivername="postgresql+psycopg")
    if url.drivername != "postgresql+psycopg":
        raise StagingTargetRefused(f"driver {url.drivername!r} is not supported; use postgresql+psycopg")

    database = url.database or ""
    if not database:
        raise StagingTargetRefused("IRIS_STAGING_DATABASE_URL has no database name")
    if _PRODUCTION_MARKER.search(database):
        raise StagingTargetRefused(f"database name {database!r} looks like a production database")
    host = (url.host or "").lower() or None
    if host and _PRODUCTION_MARKER.search(host):
        raise StagingTargetRefused(f"host {host!r} looks like a production host")

    allowed_hosts = settings.allowed_host_set
    if allowed_hosts is not None and (host or "") not in allowed_hosts:
        raise StagingTargetRefused(f"host {host!r} is not in IRIS_STAGING_ALLOWED_HOSTS")

    return StagingTarget(url=url, environment=environment, schema=schema, database=database, host=host)


def verify_connection(connection: Connection, target: StagingTarget) -> dict[str, Any]:
    """Server-side re-check of the target; run inside the write transaction."""

    row = connection.execute(
        text(
            "SELECT current_database() AS database, current_user AS db_user, "
            "(SELECT extversion FROM pg_extension WHERE extname = 'postgis') AS postgis, "
            "to_regnamespace(:schema) IS NOT NULL AS schema_exists"
        ),
        {"schema": target.schema},
    ).mappings().one()

    if row["database"] != target.database:
        raise StagingTargetRefused(
            f"connected database {row['database']!r} differs from configured {target.database!r}"
        )
    if _PRODUCTION_MARKER.search(row["database"]):
        raise StagingTargetRefused(f"connected database {row['database']!r} looks like production")
    if row["postgis"] is None:
        raise PersistenceError("PostGIS extension is not installed in the staging database")
    if not row["schema_exists"]:
        raise PersistenceError(
            f"schema {target.schema!r} does not exist; run 'python -m app.water_protection init-db'"
        )
    return dict(row)
