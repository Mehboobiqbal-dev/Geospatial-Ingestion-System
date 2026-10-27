import json
import uuid
import zlib
from collections.abc import Iterator
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from geoalchemy2.shape import from_shape
from sqlalchemy import Engine
from sqlalchemy import create_engine
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import Insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from app.database.staging.base import STAGING_SCHEMA
from app.database.staging.guard import StagingTarget
from app.database.staging.guard import verify_connection
from app.database.staging.models import TARGET_SRID
from app.database.staging.models import WaterProtectionZone
from app.database.staging.models import WaterProtectionZoneIngestionRun
from app.database.staging.models import WaterProtectionZoneQuarantine
from app.water_protection.errors import StagingTargetRefused
from app.water_protection.records import QuarantinedFeature
from app.water_protection.records import StagedZone

zones = WaterProtectionZone.__table__
quarantine = WaterProtectionZoneQuarantine.__table__
runs = WaterProtectionZoneIngestionRun.__table__

# Serialises concurrent loads of the zone table (transaction-scoped lock).
LOAD_LOCK_KEY = zlib.crc32(b"iris_staging.water_protection_zones")

# Columns overwritten when a zone's content changes. Key columns and
# first_ingested_at are never touched by an update.
ZONE_CONTENT_COLUMNS = (
    "protection_area_id",
    "protection_area_name",
    "zone_code_source",
    "zone_category",
    "zone_legal_description",
    "zone_classification_basis",
    "zone_label_source",
    "restriction_category_source",
    "legal_status_source",
    "legal_basis_source",
    "legal_effective_date",
    "source_modified_date",
    "extraction_rate",
    "extraction_rate_unit",
    "extraction_rate_status",
    "geom",
    "source_crs",
    "source_srs_name",
    "crs_transformation",
    "geometry_repaired",
    "geometry_repair_evidence",
    "validation_status",
    "validation_warnings",
    "source_layer",
    "source_url",
    "source_feature_gml_id",
    "source_attributes",
    "source_retrieved_at",
    "mapping_version",
    "content_hash",
    "last_ingestion_run_id",
    "last_seen_at",
    "last_seen_run_id",
)


for _table in (zones, quarantine, runs):
    if _table.schema != STAGING_SCHEMA:  # pragma: no cover - structural safety net
        raise StagingTargetRefused(f"table {_table.name} is not in {STAGING_SCHEMA}")


def create_staging_engine(target: StagingTarget) -> Engine:
    return create_engine(
        target.url,
        pool_pre_ping=True,
        connect_args={"application_name": "iris-wsg-th-ingest"},
    )


def _json(value: Any) -> Any:
    # Round-trip through json so dates/Decimals in evidence become plain JSON.
    return None if value is None else json.loads(json.dumps(value, default=str))


def zone_row(zone: StagedZone, run_id: uuid.UUID, seen_at: datetime) -> dict[str, Any]:
    a = zone.attributes
    g = zone.geometry
    return {
        "state_code": a.state_code,
        "source_zone_id": a.source_zone_id,
        "protection_area_id": a.protection_area_id,
        "protection_area_name": a.protection_area_name,
        "zone_code_source": a.zone_code_source,
        "zone_category": a.zone_category,
        "zone_legal_description": a.zone_legal_description,
        "zone_classification_basis": a.zone_classification_basis,
        "zone_label_source": a.zone_label_source,
        "restriction_category_source": a.restriction_category_source,
        "legal_status_source": a.legal_status_source,
        "legal_basis_source": a.legal_basis_source,
        "legal_effective_date": a.legal_effective_date,
        "source_modified_date": a.source_modified_date,
        "extraction_rate": a.extraction_rate,
        "extraction_rate_unit": a.extraction_rate_unit,
        "extraction_rate_status": a.extraction_rate_status,
        "geom": from_shape(g.geometry, srid=TARGET_SRID),
        "source_crs": g.source_crs,
        "source_srs_name": g.source_srs_name,
        "crs_transformation": g.transformation,
        "geometry_repaired": g.repaired,
        "geometry_repair_evidence": _json(g.repair_evidence),
        "validation_status": zone.validation_status,
        "validation_warnings": list(zone.warnings),
        "source_layer": zone.source_layer,
        "source_url": zone.source_url,
        "source_feature_gml_id": zone.gml_id,
        "source_attributes": _json({"gml:id": zone.gml_id, **zone.source_attributes}),
        "source_retrieved_at": zone.retrieved_at,
        "mapping_version": zone.mapping_version,
        "content_hash": zone.content_hash,
        "last_ingestion_run_id": run_id,
        "last_seen_at": seen_at,
        "last_seen_run_id": run_id,
    }


def quarantine_row(item: QuarantinedFeature, run_id: uuid.UUID, seen_at: datetime) -> dict[str, Any]:
    return {
        "state_code": item.state_code,
        "record_key": item.record_key,
        "record_key_kind": item.record_key_kind,
        "reason_code": item.reason_code,
        "reason_detail": item.reason_detail,
        "evidence": _json(item.evidence),
        "source_zone_id": item.source_zone_id,
        "source_feature_gml_id": item.gml_id,
        "source_attributes": _json({"gml:id": item.gml_id, **item.source_attributes}),
        "source_geometry_wkb": item.source_geometry_wkb,
        "source_srs_name": item.source_srs_name,
        "source_layer": item.source_layer,
        "source_url": item.source_url,
        "first_seen_at": seen_at,
        "last_seen_at": seen_at,
        "last_seen_run_id": run_id,
    }


def build_zone_upsert(rows: Sequence[dict[str, Any]]) -> Insert:
    statement = pg_insert(zones).values(list(rows))
    excluded = statement.excluded
    changes: dict[str, Any] = {name: excluded[name] for name in ZONE_CONTENT_COLUMNS}
    changes["updated_at"] = func.now()
    # The WHERE clause makes the upsert a no-op for identical content, so a
    # concurrent or repeated load can never rewrite an unchanged row.
    return statement.on_conflict_do_update(
        constraint="uq_water_protection_zones_source_key",
        set_=changes,
        where=zones.c.content_hash.is_distinct_from(excluded.content_hash),
    ).returning(zones.c.source_zone_id)


def build_quarantine_upsert(rows: Sequence[dict[str, Any]]) -> Insert:
    statement = pg_insert(quarantine).values(list(rows))
    excluded = statement.excluded
    return statement.on_conflict_do_update(
        constraint="uq_water_protection_zones_quarantine_key",
        set_={
            "reason_detail": excluded.reason_detail,
            "evidence": excluded.evidence,
            "source_zone_id": excluded.source_zone_id,
            "source_feature_gml_id": excluded.source_feature_gml_id,
            "source_attributes": excluded.source_attributes,
            "source_geometry_wkb": excluded.source_geometry_wkb,
            "source_srs_name": excluded.source_srs_name,
            "source_layer": excluded.source_layer,
            "source_url": excluded.source_url,
            "last_seen_at": excluded.last_seen_at,
            "last_seen_run_id": excluded.last_seen_run_id,
            "occurrences": quarantine.c.occurrences + 1,
        },
    ).returning(quarantine.c.id)


class WaterProtectionZoneRepository:
    """SQL access for the staging tables. All calls run in the caller's transaction."""

    def __init__(self, connection: Connection):
        self.connection = connection

    def acquire_load_lock(self) -> None:
        self.connection.execute(select(func.pg_advisory_xact_lock(LOAD_LOCK_KEY)))

    def source_ids(self, state_code: str) -> set[str]:
        result = self.connection.execute(
            select(zones.c.source_zone_id).where(zones.c.state_code == state_code)
        )
        return set(result.scalars())

    def content_hashes(self, state_code: str, source_ids: Sequence[str]) -> dict[str, str]:
        statement = (
            select(zones.c.source_zone_id, zones.c.content_hash)
            .where(zones.c.state_code == state_code)
            .where(zones.c.source_zone_id.in_(list(source_ids)))
            .with_for_update()
        )
        return {row.source_zone_id: row.content_hash for row in self.connection.execute(statement)}

    def upsert_zones(self, rows: Sequence[dict[str, Any]]) -> int:
        if not rows:
            return 0
        return len(self.connection.execute(build_zone_upsert(rows)).all())

    def mark_seen(self, state_code: str, source_ids: Sequence[str], seen_at: datetime, run_id: uuid.UUID) -> int:
        if not source_ids:
            return 0
        result = self.connection.execute(
            update(zones)
            .where(zones.c.state_code == state_code)
            .where(zones.c.source_zone_id.in_(list(source_ids)))
            .values(last_seen_at=seen_at, last_seen_run_id=run_id)
        )
        return result.rowcount

    def upsert_quarantine(self, rows: Sequence[dict[str, Any]]) -> int:
        if not rows:
            return 0
        return len(self.connection.execute(build_quarantine_upsert(rows)).all())

    def record_run(self, row: dict[str, Any]) -> None:
        statement = pg_insert(runs).values(row)
        statement = statement.on_conflict_do_update(
            index_elements=[runs.c.run_id],
            set_={k: statement.excluded[k] for k in row if k != "run_id"},
        )
        self.connection.execute(statement)


@contextmanager
def staging_transaction(engine: Engine, target: StagingTarget) -> Iterator[WaterProtectionZoneRepository]:
    """
    One transaction for the whole load: commit on success, roll back on any
    exception. The target is re-verified on the live connection first.
    """
    with engine.begin() as connection:
        verify_connection(connection, target)
        yield WaterProtectionZoneRepository(connection)
