"""
Staging tables for Thuringia water protection zones.

``water_protection_zones`` is the destination table. Its natural key is
``(state_code, source_zone_id)``; ``content_hash`` lets repeated runs
skip unchanged rows. Rejected features go to the quarantine table, and
every load run (including failed and empty ones) is logged in the
ingestion run table.
"""

import uuid
from datetime import date
from datetime import datetime
from decimal import Decimal
from typing import Any

from geoalchemy2 import Geometry
from sqlalchemy import BigInteger
from sqlalchemy import Boolean
from sqlalchemy import CheckConstraint
from sqlalchemy import Date
from sqlalchemy import DateTime
from sqlalchemy import Identity
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import LargeBinary
from sqlalchemy import Numeric
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy import UniqueConstraint
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from app.database.staging.base import StagingBase

TARGET_SRID = 25832

ZONE_CATEGORY_VALUES = ("ZONE_I", "ZONE_II", "ZONE_III", "ZONE_IIIA", "ZONE_IIIB", "UNKNOWN")
EXTRACTION_RATE_STATUS_VALUES = ("published", "missing", "unparseable", "not_published")
VALIDATION_STATUS_VALUES = ("valid", "valid_with_warnings")
RUN_STATUS_VALUES = ("succeeded", "succeeded_empty", "succeeded_with_rejections", "failed")


def _in(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


class WaterProtectionZone(StagingBase):
    """One official water protection zone polygon (EPSG:25832)."""

    __tablename__ = "water_protection_zones"

    __table_args__ = (
        UniqueConstraint(
            "state_code",
            "source_zone_id",
            name="uq_water_protection_zones_source_key",
        ),
        CheckConstraint("state_code ~ '^[A-Z]{2}$'", name="state_code_format"),
        CheckConstraint("btrim(source_zone_id) <> ''", name="source_zone_id_not_blank"),
        CheckConstraint(_in("zone_category", ZONE_CATEGORY_VALUES), name="zone_category_allowed"),
        CheckConstraint(
            "(zone_category = 'UNKNOWN') = (zone_legal_description IS NULL)",
            name="zone_description_matches_category",
        ),
        CheckConstraint(
            _in("extraction_rate_status", EXTRACTION_RATE_STATUS_VALUES),
            name="extraction_rate_status_allowed",
        ),
        CheckConstraint(
            "(extraction_rate IS NOT NULL) = (extraction_rate_status = 'published')",
            name="extraction_rate_matches_status",
        ),
        CheckConstraint("extraction_rate IS NULL OR extraction_rate >= 0", name="extraction_rate_non_negative"),
        CheckConstraint(
            "geometry_repaired = (geometry_repair_evidence IS NOT NULL)",
            name="repair_evidence_present",
        ),
        CheckConstraint(_in("validation_status", VALIDATION_STATUS_VALUES), name="validation_status_allowed"),
        CheckConstraint("char_length(content_hash) = 64", name="content_hash_length"),
        CheckConstraint("ST_IsValid(geom) AND NOT ST_IsEmpty(geom)", name="geom_valid"),
        Index("idx_water_protection_zones_geom", "geom", postgresql_using="gist"),
        Index("idx_water_protection_zones_category", "state_code", "zone_category"),
        Index("idx_water_protection_zones_protection_area", "state_code", "protection_area_id"),
        {"comment": "Official water protection zones (staging). Natural key: (state_code, source_zone_id)."},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)

    # Natural key
    state_code: Mapped[str] = mapped_column(String(2), comment="Jurisdiction code, e.g. TH (from mapping config)")
    source_zone_id: Mapped[str] = mapped_column(Text, comment="Stable zone identifier as published by the source")

    # Published attributes (verbatim) and documented classification
    protection_area_id: Mapped[str | None] = mapped_column(Text)
    protection_area_name: Mapped[str | None] = mapped_column(Text)
    zone_code_source: Mapped[str | None] = mapped_column(Text, comment="Zone code exactly as published")
    zone_category: Mapped[str] = mapped_column(Text, comment="Normalised zone category; UNKNOWN if not in mapping")
    zone_legal_description: Mapped[str | None] = mapped_column(Text, comment="Zone type from mapping table")
    zone_classification_basis: Mapped[str | None] = mapped_column(Text)
    zone_label_source: Mapped[str | None] = mapped_column(Text)
    restriction_category_source: Mapped[str | None] = mapped_column(Text)
    legal_status_source: Mapped[str | None] = mapped_column(Text)
    legal_basis_source: Mapped[str | None] = mapped_column(Text)
    legal_effective_date: Mapped[date | None] = mapped_column(Date)
    source_modified_date: Mapped[date | None] = mapped_column(Date)

    # Extraction rate: only ever a published value, never inferred
    extraction_rate: Mapped[Decimal | None] = mapped_column(Numeric)
    extraction_rate_unit: Mapped[str | None] = mapped_column(Text)
    extraction_rate_status: Mapped[str] = mapped_column(Text)

    # Geometry and CRS provenance
    geom: Mapped[Any] = mapped_column(
        Geometry(geometry_type="MULTIPOLYGON", srid=TARGET_SRID, spatial_index=False),
        nullable=False,
    )
    source_crs: Mapped[str] = mapped_column(Text, comment="Declared source CRS, e.g. EPSG:25832")
    source_srs_name: Mapped[str] = mapped_column(Text, comment="srsName exactly as in the response")
    crs_transformation: Mapped[str] = mapped_column(Text, comment="PROJ operation used, or none")
    geometry_repaired: Mapped[bool] = mapped_column(Boolean, server_default="false")
    geometry_repair_evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    # Validation
    validation_status: Mapped[str] = mapped_column(Text)
    validation_warnings: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")

    # Provenance
    source_layer: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str] = mapped_column(Text, comment="Canonical GetFeature request (redacted) or file URI")
    source_feature_gml_id: Mapped[str | None] = mapped_column(Text)
    source_attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, comment="All raw source properties")
    source_retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    mapping_version: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))

    # Load bookkeeping
    first_ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_ingestion_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))


class WaterProtectionZoneQuarantine(StagingBase):
    """Source features rejected by validation, kept with their evidence."""

    __tablename__ = "water_protection_zones_quarantine"

    __table_args__ = (
        UniqueConstraint(
            "state_code",
            "record_key",
            "reason_code",
            name="uq_water_protection_zones_quarantine_key",
        ),
        CheckConstraint(
            _in("record_key_kind", ("source_zone_id", "gml_id", "content_sha256")),
            name="record_key_kind_allowed",
        ),
        {"comment": "Rejected water protection zone features with reasons and raw source data."},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    state_code: Mapped[str] = mapped_column(String(2))
    record_key: Mapped[str] = mapped_column(Text)
    record_key_kind: Mapped[str] = mapped_column(Text)
    reason_code: Mapped[str] = mapped_column(Text)
    reason_detail: Mapped[str] = mapped_column(Text)
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    source_zone_id: Mapped[str | None] = mapped_column(Text)
    source_feature_gml_id: Mapped[str | None] = mapped_column(Text)
    source_attributes: Mapped[dict[str, Any]] = mapped_column(JSONB)
    source_geometry_wkb: Mapped[bytes | None] = mapped_column(LargeBinary, comment="As parsed, source CRS")
    source_srs_name: Mapped[str | None] = mapped_column(Text)
    source_layer: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    occurrences: Mapped[int] = mapped_column(Integer, server_default="1")


class WaterProtectionZoneIngestionRun(StagingBase):
    """Audit log of load runs, including failed and empty retrievals."""

    __tablename__ = "water_protection_zone_ingestion_runs"

    __table_args__ = (
        CheckConstraint(_in("status", RUN_STATUS_VALUES), name="status_allowed"),
        {"comment": "One row per staging load run with its full JSON report."},
    )

    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    source_layer: Mapped[str | None] = mapped_column(Text)
    mapping_version: Mapped[str | None] = mapped_column(Text)
    mapping_verified: Mapped[bool | None] = mapped_column(Boolean)
    features_retrieved: Mapped[int | None] = mapped_column(Integer)
    features_accepted: Mapped[int | None] = mapped_column(Integer)
    features_rejected: Mapped[int | None] = mapped_column(Integer)
    rows_inserted: Mapped[int | None] = mapped_column(Integer)
    rows_updated: Mapped[int | None] = mapped_column(Integer)
    rows_unchanged: Mapped[int | None] = mapped_column(Integer)
    error_type: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    report: Mapped[dict[str, Any]] = mapped_column(JSONB)
