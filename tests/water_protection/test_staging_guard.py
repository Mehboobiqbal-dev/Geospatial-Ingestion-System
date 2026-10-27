from __future__ import annotations

import pytest

from app.database.staging.guard import validate_staging_target
from app.water_protection import cli
from app.water_protection.config import StagingDatabaseSettings
from app.water_protection.errors import StagingTargetRefused

GOOD_URL = "postgresql+psycopg://iris_staging_writer:pw-123@localhost:5434/iris_staging_dev"


def settings(**overrides: object) -> StagingDatabaseSettings:
    values: dict[str, object] = {
        "IRIS_STAGING_DATABASE_URL": GOOD_URL,
        "IRIS_ENVIRONMENT": "development",
    }
    values.update(overrides)
    return StagingDatabaseSettings(_env_file=None, **values)  # type: ignore[arg-type]


def test_valid_staging_target() -> None:
    target = validate_staging_target(settings())

    assert target.schema == "iris_staging"
    assert target.database == "iris_staging_dev"
    assert target.environment == "development"
    assert "pw-123" not in target.describe()
    assert "iris_staging_writer" not in target.describe()


def test_plain_postgresql_url_uses_psycopg3() -> None:
    target = validate_staging_target(settings(IRIS_STAGING_DATABASE_URL=GOOD_URL.replace("+psycopg", "")))
    assert target.url.drivername == "postgresql+psycopg"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"IRIS_ENVIRONMENT": None}, "IRIS_ENVIRONMENT is not set"),
        ({"IRIS_ENVIRONMENT": "production"}, "not allowed to write"),
        ({"IRIS_ENVIRONMENT": "prod"}, "not allowed to write"),
        ({"IRIS_STAGING_SCHEMA": "iris_core"}, "schema 'iris_core' are forbidden"),
        ({"IRIS_STAGING_SCHEMA": "public"}, "schema 'public' are forbidden"),
        ({"IRIS_STAGING_SCHEMA": "iris_staging_tmp"}, "is not the staging schema"),
        ({"IRIS_STAGING_DATABASE_URL": None}, "IRIS_STAGING_DATABASE_URL is not set"),
        ({"IRIS_STAGING_DATABASE_URL": "not a url"}, "not a valid database URL"),
        ({"IRIS_STAGING_DATABASE_URL": "sqlite:///staging.db"}, "is not PostgreSQL"),
        ({"IRIS_STAGING_DATABASE_URL": "postgresql+psycopg2://u:p@localhost/iris_staging"}, "not supported"),
        ({"IRIS_STAGING_DATABASE_URL": "postgresql+psycopg://u:p@localhost"}, "no database name"),
        ({"IRIS_STAGING_DATABASE_URL": "postgresql+psycopg://u:p@localhost/iris_prod"}, "looks like a production"),
        ({"IRIS_STAGING_DATABASE_URL": "postgresql+psycopg://u:p@localhost/production"}, "looks like a production"),
        ({"IRIS_STAGING_DATABASE_URL": "postgresql+psycopg://u:p@db.prod.internal/iris_staging"}, "production host"),
        ({"IRIS_STAGING_ALLOWED_HOSTS": "postgis-staging,127.0.0.1"}, "not in IRIS_STAGING_ALLOWED_HOSTS"),
    ],
)
def test_unsafe_targets_are_refused(overrides: dict[str, object], message: str) -> None:
    with pytest.raises(StagingTargetRefused, match=message):
        validate_staging_target(settings(**overrides))


def test_production_marker_does_not_flag_ordinary_words() -> None:
    url = "postgresql+psycopg://u:p@localhost/products_staging"
    assert validate_staging_target(settings(IRIS_STAGING_DATABASE_URL=url)).database == "products_staging"


def test_allowed_hosts() -> None:
    target = validate_staging_target(settings(IRIS_STAGING_ALLOWED_HOSTS="localhost, postgis-staging"))
    assert target.host == "localhost"


@pytest.mark.parametrize(
    ("environment", "schema"),
    [("production", "iris_staging"), ("development", "iris_core")],
)
def test_load_command_refuses_before_any_connection_or_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path, environment: str, schema: str
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IRIS_ENVIRONMENT", environment)
    monkeypatch.setenv("IRIS_STAGING_SCHEMA", schema)
    monkeypatch.setenv("IRIS_STAGING_DATABASE_URL", GOOD_URL)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("must not be reached")

    monkeypatch.setattr("app.database.staging.repository.create_staging_engine", forbidden)
    monkeypatch.setattr(cli, "retrieve_from_wfs", forbidden)

    assert cli.main(["load", "--allow-unverified-mapping"]) == 2


def test_init_db_refuses_iris_core(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IRIS_ENVIRONMENT", "development")
    monkeypatch.setenv("IRIS_STAGING_SCHEMA", "iris_core")
    monkeypatch.setenv("IRIS_STAGING_DATABASE_URL", GOOD_URL)

    assert cli.main(["init-db"]) == 2
