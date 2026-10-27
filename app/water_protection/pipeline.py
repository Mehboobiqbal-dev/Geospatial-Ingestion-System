"""
Pipeline: retrieve -> map + validate -> de-duplicate -> (export | load).

Retrieval problems fail the run before anything is written. Problems
with individual features only quarantine those features; the report
accounts for every retrieved feature as accepted, collapsed duplicate
or rejected (with a reason).
"""

from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from pathlib import Path
from typing import Any

import shapely

from app.water_protection.config import WfsSettings
from app.water_protection.errors import ConfigurationError
from app.water_protection.errors import PersistenceError
from app.water_protection.errors import RecordRejected
from app.water_protection.errors import SourceError
from app.water_protection.errors import StagingTargetRefused
from app.water_protection.geometry import GeometryPolicy
from app.water_protection.geometry import normalize_geometry
from app.water_protection.loader import LoadSummary
from app.water_protection.mapping import LayerMapping
from app.water_protection.mapping import map_attributes
from app.water_protection.records import QuarantinedFeature
from app.water_protection.records import StagedZone
from app.water_protection.records import compute_content_hash
from app.water_protection.records import raw_record_key
from app.water_protection.report import IngestionReport
from app.water_protection.report import utc_now
from app.water_protection.wfs.client import WfsClient
from app.water_protection.wfs.gml import RawFeature
from app.water_protection.wfs.gml import parse_feature_collection
from app.water_protection.wfs.schema import SchemaCheck
from app.water_protection.wfs.schema import check_schema_against_mapping

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SourceSnapshot:
    features: tuple[RawFeature, ...]
    source_url: str
    default_srs: str | None
    retrieved_at: datetime
    origin: dict[str, Any]
    schema_check: SchemaCheck | None = None
    duplicate_features_collapsed: int = 0


@dataclass
class ProcessingResult:
    zones: list[StagedZone] = field(default_factory=list)
    quarantined: list[QuarantinedFeature] = field(default_factory=list)
    duplicates_collapsed: int = 0


# ==========================================================
# Retrieval
# ==========================================================


def retrieve_from_wfs(settings: WfsSettings, mapping: LayerMapping, client: WfsClient) -> SourceSnapshot:
    capabilities, _ = client.get_capabilities()
    plan = client.plan(
        capabilities,
        settings.layer,
        page_size=settings.page_size,
        sort_by=mapping.zone_id_property,
    )
    schema = client.describe_feature_type(capabilities, plan.feature_type)
    schema_check = check_schema_against_mapping(
        schema,
        required=mapping.required_properties(),
        geometry_property=mapping.geometry_property,
        numeric=mapping.numeric_properties(),
    )
    retrieved_at = utc_now()
    result = client.fetch_all(
        plan,
        geometry_property=mapping.geometry_property,
        max_features=settings.max_features,
        max_pages=settings.max_pages,
    )
    origin = {
        "kind": "wfs",
        "endpoint": client.canonical_request_url(plan).split("?")[0],
        "layer": plan.feature_type.name,
        "service_title": capabilities.title,
        "service_provider": capabilities.provider,
        "wfs_version": capabilities.version,
        "output_format": plan.output_format or "(service default)",
        "declared_default_crs": plan.feature_type.default_crs,
        "paging": plan.paging,
        "page_size": plan.page_size,
        "sort_by": plan.sort_by,
        "pages": result.pages,
        "number_matched": result.number_matched,
        "server_time_stamp": result.server_time_stamp,
        "request_url": result.request_url,
    }
    return SourceSnapshot(
        features=result.features,
        source_url=result.request_url,
        # The srsName in the data wins; the declared default only fills gaps.
        default_srs=result.default_srs or plan.feature_type.default_crs,
        retrieved_at=retrieved_at,
        origin=origin,
        schema_check=schema_check,
        duplicate_features_collapsed=result.duplicate_features_collapsed,
    )


def retrieve_from_file(path: Path, mapping: LayerMapping, *, default_srs: str | None) -> SourceSnapshot:
    """Replay a saved GetFeature response (e.g. one downloaded manually)."""
    content = path.read_bytes()
    page = parse_feature_collection(
        content,
        layer_local_name=mapping.layer,
        geometry_property=mapping.geometry_property,
        operation=f"file:{path.name}",
    )
    if page.number_matched is not None and page.number_matched != len(page.features):
        raise SourceError(
            f"file declares numberMatched={page.number_matched} but contains {len(page.features)} features",
            operation=f"file:{path.name}",
        )
    sha256 = hashlib.sha256(content).hexdigest()
    return SourceSnapshot(
        features=page.features,
        source_url=f"{path.resolve().as_uri()}#sha256={sha256}",
        default_srs=page.collection_srs or default_srs,
        retrieved_at=utc_now(),
        origin={
            "kind": "file",
            "path": str(path),
            "sha256": sha256,
            "layer": mapping.layer,
            "number_matched": page.number_matched,
            "server_time_stamp": page.time_stamp,
        },
    )


# ==========================================================
# Processing
# ==========================================================


def process_snapshot(
    snapshot: SourceSnapshot,
    mapping: LayerMapping,
    policy: GeometryPolicy,
    report: IngestionReport,
) -> ProcessingResult:
    result = ProcessingResult(duplicates_collapsed=snapshot.duplicate_features_collapsed)
    candidates: list[StagedZone] = []

    for feature in snapshot.features:
        try:
            zone = _stage(feature, snapshot, mapping, policy)
        except RecordRejected as exc:
            item = _quarantine(feature, snapshot, mapping, exc)
            result.quarantined.append(item)
            report.add_rejection(exc.reason_code, item.record_key, exc.detail)
            continue
        candidates.append(zone)

    # A source id must identify exactly one zone. Identical repeats are
    # collapsed; conflicting ones are all quarantined, because picking one
    # would be a guess about which version is legally current.
    by_key: dict[str, list[StagedZone]] = defaultdict(list)
    for zone in candidates:
        by_key[zone.key].append(zone)

    for key, group in by_key.items():
        if len({z.content_hash for z in group}) == 1:
            result.zones.append(group[0])
            result.duplicates_collapsed += len(group) - 1
            continue
        for zone in group:
            detail = f"{len(group)} features share source_zone_id {key!r} with different content"
            item = QuarantinedFeature(
                state_code=zone.attributes.state_code,
                record_key=zone.gml_id or key,
                record_key_kind="gml_id" if zone.gml_id else "source_zone_id",
                reason_code="conflicting_duplicate_source_id",
                reason_detail=detail,
                evidence={"content_hash": zone.content_hash},
                source_zone_id=key,
                gml_id=zone.gml_id,
                source_attributes=zone.source_attributes,
                source_geometry_wkb=None,
                source_srs_name=zone.geometry.source_srs_name,
                source_layer=zone.source_layer,
                source_url=zone.source_url,
            )
            result.quarantined.append(item)
            report.add_rejection(item.reason_code, item.record_key, detail)

    for zone in result.zones:
        report.zone_categories[zone.attributes.zone_category] += 1
        report.source_crs[zone.geometry.source_crs] += 1
        report.transformations.add(f"{zone.geometry.source_crs}: {zone.geometry.transformation}")
        for warning in zone.warnings:
            report.warnings[warning] += 1

    report.counts.update(
        {
            "retrieved": len(snapshot.features) + snapshot.duplicate_features_collapsed,
            "accepted": len(result.zones),
            "rejected": len(result.quarantined),
            "duplicates_collapsed": result.duplicates_collapsed,
            "repaired": sum(1 for z in result.zones if z.geometry.repaired),
            "with_warnings": sum(1 for z in result.zones if z.warnings),
            "multipart": sum(1 for z in result.zones if z.geometry.part_count > 1),
        }
    )
    return result


def _stage(
    feature: RawFeature,
    snapshot: SourceSnapshot,
    mapping: LayerMapping,
    policy: GeometryPolicy,
) -> StagedZone:
    attributes = map_attributes(feature, mapping)
    geometry = normalize_geometry(feature.geometry, fallback_srs=snapshot.default_srs, policy=policy)
    return StagedZone(
        attributes=attributes,
        geometry=geometry,
        gml_id=feature.gml_id,
        source_attributes=feature.properties,
        source_layer=mapping.layer,
        source_url=snapshot.source_url,
        retrieved_at=snapshot.retrieved_at,
        mapping_version=mapping.identifier,
        content_hash=compute_content_hash(attributes, feature.properties, geometry, mapping.identifier),
    )


def _quarantine(
    feature: RawFeature,
    snapshot: SourceSnapshot,
    mapping: LayerMapping,
    exc: RecordRejected,
) -> QuarantinedFeature:
    shape = feature.geometry.shape
    wkb = shapely.to_wkb(shape) if shape is not None and not shape.is_empty else None
    raw_id = feature.properties.get(mapping.zone_id_property)
    source_id = raw_id.strip() if isinstance(raw_id, str) and raw_id.strip() else None
    if source_id:
        key, kind = source_id, "source_zone_id"
    elif feature.gml_id:
        key, kind = feature.gml_id, "gml_id"
    else:
        key, kind = raw_record_key(feature.properties, wkb), "content_sha256"
    return QuarantinedFeature(
        state_code=mapping.state_code,
        record_key=key,
        record_key_kind=kind,
        reason_code=exc.reason_code,
        reason_detail=exc.detail,
        evidence=exc.evidence,
        source_zone_id=source_id,
        gml_id=feature.gml_id,
        source_attributes=feature.properties,
        source_geometry_wkb=wkb,
        source_srs_name=feature.geometry.srs_name or snapshot.default_srs,
        source_layer=mapping.layer,
        source_url=snapshot.source_url,
    )


# ==========================================================
# Run
# ==========================================================


Loader = Callable[[ProcessingResult, IngestionReport], LoadSummary]
Exporter = Callable[[ProcessingResult], int]


def run_ingestion(
    *,
    mode: str,
    mapping: LayerMapping,
    policy: GeometryPolicy,
    snapshot_provider: Callable[[], SourceSnapshot],
    loader: Loader | None = None,
    exporter: Exporter | None = None,
    allow_unverified_mapping: bool = False,
    report: IngestionReport | None = None,
) -> IngestionReport:
    report = report or IngestionReport(mode=mode)
    report.mapping = {
        "identifier": mapping.identifier,
        "source": mapping.source,
        "schema_verified": mapping.schema_verified,
        "verification_note": mapping.verification_note,
    }
    if not mapping.schema_verified:
        report.notes.append("Mapping is not verified against the live schema; source property names are assumptions.")

    if mode == "load" and not mapping.schema_verified and not allow_unverified_mapping:
        report.fail(
            ConfigurationError(
                "refusing to load with an unverified mapping; verify it against DescribeFeatureType "
                "or pass --allow-unverified-mapping explicitly"
            ),
            refused=True,
        )
        return report

    try:
        snapshot = snapshot_provider()
    except SourceError as exc:
        logger.error("source retrieval failed", extra={"error": str(exc)})
        report.fail(exc)
        return report

    report.source = snapshot.origin
    if snapshot.schema_check is not None:
        report.schema = {
            "checked": True,
            "unmapped_properties": list(snapshot.schema_check.unmapped_properties),
            "warnings": list(snapshot.schema_check.warnings),
        }
    else:
        report.schema = {"checked": False, "reason": "no DescribeFeatureType for file input"}

    result = process_snapshot(snapshot, mapping, policy, report)
    if not snapshot.features:
        report.notes.append("The source returned a valid, empty feature collection.")

    try:
        if exporter is not None:
            report.counts["exported"] = exporter(result)
        if loader is not None:
            report.database = loader(result, report).to_dict()
    except StagingTargetRefused as exc:
        report.fail(exc, refused=True)
        return report
    except (PersistenceError, OSError, ValueError) as exc:
        report.fail(exc)
        return report

    report.finish()
    return report
