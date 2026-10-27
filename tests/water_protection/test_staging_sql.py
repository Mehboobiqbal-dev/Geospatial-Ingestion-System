from __future__ import annotations

import io
import re
import uuid
from contextlib import redirect_stdout
from datetime import UTC
from datetime import datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.dialects import postgresql

from app.database.staging.base import STAGING_SCHEMA
from app.database.staging.base import StagingBase
from app.database.staging.repository import build_quarantine_upsert
from app.database.staging.repository import build_zone_upsert
from app.database.staging.repository import quarantine_row
from app.database.staging.repository import zone_row
from app.water_protection.geometry import GeometryPolicy
from app.water_protection.mapping import LayerMapping
from app.water_protection.pipeline import process_snapshot
from app.water_protection.pipeline import retrieve_from_file
from app.water_protection.report import IngestionReport
from tests.water_protection.conftest import FIXTURES

ROOT = Path(__file__).resolve().parents[2]
RUN_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


@pytest.fixture
def processed(mapping: LayerMapping):
    snapshot = retrieve_from_file(FIXTURES / "getfeature_edge_cases.xml", mapping, default_srs=None)
    return process_snapshot(snapshot, mapping, GeometryPolicy(), IngestionReport(mode="load"))


def compile_sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def test_all_staging_tables_live_in_iris_staging() -> None:
    assert StagingBase.metadata.schema == STAGING_SCHEMA == "iris_staging"
    assert sorted(StagingBase.metadata.tables) == [
        "iris_staging.water_protection_zone_ingestion_runs",
        "iris_staging.water_protection_zones",
        "iris_staging.water_protection_zones_quarantine",
    ]


def test_zone_upsert_is_parameterised_and_keyed(processed) -> None:
    rows = [zone_row(z, RUN_ID, NOW) for z in processed.zones]
    rows[0]["source_zone_id"] = "TEST'); DROP TABLE iris_core.facilities; --"

    statement = build_zone_upsert(rows)
    sql = compile_sql(statement)
    params = statement.compile(dialect=postgresql.dialect()).params

    assert sql.startswith("INSERT INTO iris_staging.water_protection_zones ")
    assert "ON CONFLICT ON CONSTRAINT uq_water_protection_zones_source_key DO UPDATE" in sql
    assert "WHERE iris_staging.water_protection_zones.content_hash IS DISTINCT FROM excluded.content_hash" in sql
    assert "RETURNING iris_staging.water_protection_zones.source_zone_id" in sql
    # Values travel as bound parameters, never as SQL text.
    assert "DROP TABLE" not in sql
    assert "DROP TABLE" in " ".join(str(v) for v in params.values())
    assert "iris_core" not in sql
    # The key and first_ingested_at are never overwritten by an update.
    update_clause = sql.split("DO UPDATE SET", 1)[1]
    assert "source_zone_id =" not in update_clause
    assert "state_code =" not in update_clause
    assert "first_ingested_at" not in update_clause


def test_quarantine_upsert_counts_repeat_sightings(processed) -> None:
    rows = [quarantine_row(q, RUN_ID, NOW) for q in processed.quarantined]
    sql = compile_sql(build_quarantine_upsert(rows))

    assert sql.startswith("INSERT INTO iris_staging.water_protection_zones_quarantine ")
    assert "ON CONFLICT ON CONSTRAINT uq_water_protection_zones_quarantine_key DO UPDATE" in sql
    assert "occurrences = (iris_staging.water_protection_zones_quarantine.occurrences +" in sql


def test_zone_rows_have_explicit_nulls_for_unpublished_values(processed) -> None:
    row = zone_row(next(z for z in processed.zones if z.key == "TEST-Z-015"), RUN_ID, NOW)
    assert row["zone_category"] == "UNKNOWN"
    assert row["zone_legal_description"] is None
    assert row["extraction_rate"] is None
    assert row["extraction_rate_status"] == "not_published"
    assert row["geometry_repair_evidence"] is None
    assert row["source_attributes"]["gml:id"] == "th_wsg_zonen.15"


def test_offline_migration_sql_touches_only_iris_staging(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(ROOT)
    config = Config(str(ROOT / "alembic_staging.ini"))
    buffer = io.StringIO()
    config.stdout = buffer
    with redirect_stdout(buffer):
        command.upgrade(config, "head", sql=True)
    sql = buffer.getvalue()

    assert "CREATE TABLE iris_staging.alembic_version" in sql
    assert "CREATE TABLE iris_staging.water_protection_zones (" in sql
    assert (
        "CREATE INDEX idx_water_protection_zones_geom ON iris_staging.water_protection_zones USING gist (geom)" in sql
    )
    assert "geometry(MULTIPOLYGON,25832)" in sql
    assert "CONSTRAINT uq_water_protection_zones_source_key UNIQUE (state_code, source_zone_id)" in sql
    assert "iris_core" not in sql
    created = set(re.findall(r"CREATE TABLE (\w+)\.", sql))
    assert created == {"iris_staging"}
