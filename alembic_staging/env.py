from logging.config import fileConfig

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy import pool
from sqlalchemy import text

import app.database.staging.models  # noqa: F401  (registers staging tables)
from app.database.staging.base import STAGING_SCHEMA
from app.database.staging.base import StagingBase
from app.database.staging.guard import validate_staging_target
from app.water_protection.config import StagingDatabaseSettings
from app.water_protection.errors import StagingTargetRefused

load_dotenv()

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = StagingBase.metadata


def include_name(name, type_, parent_names):
    # Only ever compare or touch the staging schema.
    if type_ == "schema":
        return name == STAGING_SCHEMA
    return True


def run_migrations_offline() -> None:
    # SQL generation only (alembic ... upgrade head --sql); no connection.
    context.configure(
        url="postgresql+psycopg://offline/iris_staging_offline",
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
        include_name=include_name,
        version_table_schema=STAGING_SCHEMA,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    target = validate_staging_target(StagingDatabaseSettings())
    engine = create_engine(target.url, poolclass=pool.NullPool)

    with engine.connect() as connection:
        database, postgis = connection.execute(
            text(
                "SELECT current_database(), "
                "(SELECT extversion FROM pg_extension WHERE extname = 'postgis')"
            )
        ).one()
        if database != target.database:
            raise StagingTargetRefused(f"connected to {database!r}, expected {target.database!r}")
        if postgis is None:
            raise RuntimeError(
                "PostGIS is not installed in the staging database; "
                "a superuser must run CREATE EXTENSION postgis first"
            )
        # The version table lives in iris_staging, so the schema must exist
        # before Alembic creates it. Checked first because even
        # "CREATE SCHEMA IF NOT EXISTS" needs database-level CREATE, which a
        # least-privilege staging role does not have.
        exists = connection.execute(
            text("SELECT to_regnamespace(:schema) IS NOT NULL"), {"schema": STAGING_SCHEMA}
        ).scalar_one()
        if not exists:
            connection.execute(text(f"CREATE SCHEMA {STAGING_SCHEMA}"))
        # End the transaction the checks auto-began; otherwise Alembic joins
        # it instead of owning it and the migration is rolled back on close.
        connection.commit()

        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_schemas=True,
            include_name=include_name,
            version_table_schema=STAGING_SCHEMA,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()

    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
