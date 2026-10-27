from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import geopandas as gpd
import pytest
import responses

from app.water_protection.config import WfsSettings
from app.water_protection.errors import PersistenceError
from app.water_protection.export import export_geopackage
from app.water_protection.geometry import GeometryPolicy
from app.water_protection.loader import LoadSummary
from app.water_protection.mapping import LayerMapping
from app.water_protection.pipeline import SourceSnapshot
from app.water_protection.pipeline import process_snapshot
from app.water_protection.pipeline import retrieve_from_file
from app.water_protection.pipeline import retrieve_from_wfs
from app.water_protection.pipeline import run_ingestion
from app.water_protection.report import IngestionReport
from app.water_protection.wfs.client import WfsClient
from tests.water_protection.conftest import FIXTURES
from tests.water_protection.conftest import FakeWfs
from tests.water_protection.conftest import fixture_bytes

EDGE_CASES = FIXTURES / "getfeature_edge_cases.xml"
POLICY = GeometryPolicy()


def edge_snapshot(mapping: LayerMapping) -> SourceSnapshot:
    return retrieve_from_file(EDGE_CASES, mapping, default_srs=None)


def test_edge_case_collection_is_fully_accounted_for(mapping: LayerMapping) -> None:
    report = IngestionReport(mode="validate")
    result = process_snapshot(edge_snapshot(mapping), mapping, POLICY, report)

    assert report.counts == {
        "retrieved": 18,
        "accepted": 9,
        "rejected": 8,
        "duplicates_collapsed": 1,
        "repaired": 1,
        "with_warnings": 3,
        "multipart": 1,
    }
    # Every retrieved feature is accepted, collapsed or rejected - nothing disappears.
    assert report.counts["accepted"] + report.counts["rejected"] + report.counts["duplicates_collapsed"] == 18
    assert dict(report.rejections) == {
        "conflicting_duplicate_source_id": 2,
        "malformed_geometry": 1,
        "missing_geometry": 1,
        "missing_source_zone_id": 1,
        "outside_plausibility_extent": 1,
        "repair_exceeds_tolerance": 1,
        "unsupported_geometry_type": 1,
    }
    assert dict(report.zone_categories) == {
        "UNKNOWN": 2,
        "ZONE_I": 1,
        "ZONE_II": 2,
        "ZONE_III": 1,
        "ZONE_IIIA": 1,
        "ZONE_IIIB": 2,
    }

    by_id = {z.key: z for z in result.zones}
    assert by_id["TEST-Z-006"].attributes.zone_category == "UNKNOWN"  # "III A" is not guessed
    assert by_id["TEST-Z-006"].attributes.zone_code_source == "III A"
    assert by_id["TEST-Z-007"].geometry.repaired
    assert by_id["TEST-Z-015"].attributes.extraction_rate is None  # remark text is not parsed
    assert by_id["TEST-Z-015"].attributes.extraction_rate_status == "not_published"
    assert by_id["TEST-Z-018"].geometry.source_crs == "EPSG:4326"
    assert all(z.geometry.geometry.is_valid for z in result.zones)

    quarantined = {(q.record_key, q.reason_code) for q in result.quarantined}
    assert ("th_wsg_zonen.10", "missing_source_zone_id") in quarantined  # no id -> keyed by gml:id
    assert ("TEST-Z-008", "repair_exceeds_tolerance") in quarantined
    bow_tie = next(q for q in result.quarantined if q.record_key == "TEST-Z-008")
    assert bow_tie.source_geometry_wkb  # raw geometry kept as evidence
    assert bow_tie.evidence and "original_wkb_hex" in bow_tie.evidence


def test_content_hash_ignores_run_specific_values(mapping: LayerMapping) -> None:
    first = process_snapshot(edge_snapshot(mapping), mapping, POLICY, IngestionReport(mode="validate"))
    later = replace(edge_snapshot(mapping), source_url="https://other.example/wsg?STARTINDEX=500")
    second = process_snapshot(later, mapping, POLICY, IngestionReport(mode="validate"))

    assert {z.key: z.content_hash for z in first.zones} == {z.key: z.content_hash for z in second.zones}


def test_content_hash_changes_with_content(mapping: LayerMapping) -> None:
    snapshot = edge_snapshot(mapping)
    # features[1] is TEST-Z-002, which (unlike TEST-Z-001) has no duplicate in the file.
    changed_feature = replace(
        snapshot.features[1], properties={**snapshot.features[1].properties, "status": "aufgehoben"}
    )
    changed = replace(snapshot, features=(snapshot.features[0], changed_feature, *snapshot.features[2:]))

    before = {z.key: z.content_hash for z in process_snapshot(snapshot, mapping, POLICY, IngestionReport("v")).zones}
    after = {z.key: z.content_hash for z in process_snapshot(changed, mapping, POLICY, IngestionReport("v")).zones}

    assert before["TEST-Z-002"] != after["TEST-Z-002"]
    assert {k: v for k, v in before.items() if k != "TEST-Z-002"} == {
        k: v for k, v in after.items() if k != "TEST-Z-002"
    }


@responses.activate
def test_validate_run_against_wfs(
    settings: WfsSettings, mapping: LayerMapping, client: WfsClient, fake_wfs: Callable[[], FakeWfs]
) -> None:
    fake_wfs()

    report = run_ingestion(
        mode="validate",
        mapping=mapping,
        policy=POLICY,
        snapshot_provider=lambda: retrieve_from_wfs(settings, mapping, client),
    )

    assert report.status == "succeeded"
    assert report.exit_code == 0
    assert report.counts["accepted"] == 5
    assert report.source["wfs_version"] == "2.0.0"
    assert report.source["number_matched"] == 5
    assert report.source["pages"] == 3
    assert report.source["declared_default_crs"] == "urn:ogc:def:crs:EPSG::25832"
    assert report.schema == {"checked": True, "unmapped_properties": ["bemerkung"], "warnings": []}
    assert report.mapping["schema_verified"] is False
    assert any("not verified" in note for note in report.notes)


@responses.activate
def test_schema_drift_fails_before_features_are_downloaded(
    settings: WfsSettings, mapping: LayerMapping, client: WfsClient, fake_wfs: Callable[[], FakeWfs]
) -> None:
    fake = fake_wfs()
    fake.schema = fake.schema.replace(b'name="zone"', b'name="zonen_art"')

    report = run_ingestion(
        mode="validate",
        mapping=mapping,
        policy=POLICY,
        snapshot_provider=lambda: retrieve_from_wfs(settings, mapping, client),
    )

    assert report.status == "failed"
    assert report.error is not None and report.error["type"] == "SchemaMismatchError"
    assert "fields.zone_code -> 'zone'" in report.error["message"]
    assert fake.requests_named("GetFeature") == []


def test_empty_source_is_reported_as_successful_empty(mapping: LayerMapping, tmp_path: Path) -> None:
    empty = tmp_path / "empty.xml"
    empty.write_bytes(fixture_bytes("getfeature_empty.xml"))

    report = run_ingestion(
        mode="validate",
        mapping=mapping,
        policy=POLICY,
        snapshot_provider=lambda: retrieve_from_file(empty, mapping, default_srs=None),
    )

    assert report.status == "succeeded_empty"
    assert report.exit_code == 0
    assert report.error is None
    assert "valid, empty feature collection" in " ".join(report.notes)


def test_failed_retrieval_is_not_an_empty_result(mapping: LayerMapping, tmp_path: Path) -> None:
    challenge = tmp_path / "challenge.xml"
    challenge.write_bytes(fixture_bytes("html_challenge.html"))
    loader_calls: list[object] = []

    report = run_ingestion(
        mode="load",
        mapping=mapping,
        policy=POLICY,
        allow_unverified_mapping=True,
        snapshot_provider=lambda: retrieve_from_file(challenge, mapping, default_srs=None),
        loader=lambda result, rep: loader_calls.append(result) or LoadSummary(),
    )

    assert report.status == "failed"
    assert report.exit_code == 1
    assert report.error is not None and report.error["type"] == "UnexpectedContentError"
    assert loader_calls == []


def test_load_with_unverified_mapping_is_refused_up_front(mapping: LayerMapping) -> None:
    def must_not_run() -> SourceSnapshot:
        raise AssertionError("source must not be contacted")

    report = run_ingestion(mode="load", mapping=mapping, policy=POLICY, snapshot_provider=must_not_run)

    assert report.status == "failed"
    assert report.exit_code == 2
    assert "unverified mapping" in (report.error or {}).get("message", "")


def test_persistence_failure_marks_the_run_failed(mapping: LayerMapping) -> None:
    def failing_loader(result, report):
        raise PersistenceError("staging load rolled back: CheckViolation")

    report = run_ingestion(
        mode="load",
        mapping=replace(mapping, schema_verified=True),
        policy=POLICY,
        snapshot_provider=lambda: edge_snapshot(mapping),
        loader=failing_loader,
    )

    assert report.status == "failed"
    assert report.counts["accepted"] == 9  # processing evidence is kept in the report
    assert report.error == {"type": "PersistenceError", "message": "staging load rolled back: CheckViolation"}


def test_report_serialises_to_json(mapping: LayerMapping) -> None:
    report = run_ingestion(
        mode="validate", mapping=mapping, policy=POLICY, snapshot_provider=lambda: edge_snapshot(mapping)
    )
    data = report.to_dict()

    assert data["status"] == "succeeded_with_rejections"
    assert report.exit_code == 3
    assert data["source"]["kind"] == "file"
    assert len(data["source"]["sha256"]) == 64
    assert len(data["rejected_samples"]) == 8
    assert data["transformations"] == [
        "EPSG:25832: none (source CRS is EPSG:25832)",
        next(t for t in data["transformations"] if t.startswith("EPSG:4326:")),
    ]
    assert report.to_json()


def test_geopackage_export(mapping: LayerMapping, tmp_path: Path) -> None:
    result = process_snapshot(edge_snapshot(mapping), mapping, POLICY, IngestionReport(mode="validate"))
    target = tmp_path / "zones.gpkg"

    assert export_geopackage(result.zones, target) == 9

    frame = gpd.read_file(target, layer="water_protection_zones")
    assert frame.crs.to_epsg() == 25832
    assert set(frame["zone_category"]) == {"UNKNOWN", "ZONE_I", "ZONE_II", "ZONE_III", "ZONE_IIIA", "ZONE_IIIB"}
    assert frame.geometry.is_valid.all()
    with pytest.raises(ValueError, match=".gpkg"):
        export_geopackage(result.zones, tmp_path / "zones.geojson")
