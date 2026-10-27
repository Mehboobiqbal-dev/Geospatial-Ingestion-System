from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

STAGING_SCHEMA = "iris_staging"

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class StagingBase(DeclarativeBase):
    """
    Declarative base for staging tables.

    Kept separate from ``app.database.base.Base`` (iris_core) on purpose:
    its metadata is bound to the iris_staging schema, so staging models
    and staging migrations cannot reference iris_core tables, and the
    iris_core Alembic environment never sees these tables.
    """

    metadata = MetaData(
        schema=STAGING_SCHEMA,
        naming_convention=NAMING_CONVENTION,
    )
