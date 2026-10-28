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
LIVE_EXCERPT = FIXTURES / "live_getfeature_excerpt.xml"
POLICY = GeometryPolicy()


def edge_snapshot(mapping: LayerMapping) -> SourceSnapshot:
    return retrieve_from_file(EDGE_CASES, mapping, default_srs=None)


def test_edge_case_collection_is_fully_accounted_for(mapping: LayerMapping) -> None:
    report = IngestionReport(mode="validate")
    result = process_snapshot(edge_snapshot(mapping), mapping, POLICY, report)

    assert report.counts == {
        "retrieved": 20,
        "accepted": 10,
        "rejected": 9,
        "duplicates_collapsed": 1,
        "repaired": 1,
        "with_warnings": 4,
        "multipart": 1,
    }
    # Every retrieved feature is accepted, collapsed or rejected - nothing disappears.
    assert report.counts["accepted"] + report.counts["rejected"] + report.counts["duplicates_collapsed"] == 20
    assert dict(report.rejections) == {
        "conflicting_duplicate_source_id": 2,
        "invalid_source_zone_id": 1,
        "malformed_geometry": 1,
        "missing_geometry": 1,
        "missing_source_zone_id": 1,
        "outside_plausibility_extent": 1,
        "repair_exceeds_tolerance": 1,
        "unsupported_geometry_type": 1,
    }
    assert dict(report.zone_categories) == {
        "UNKNOWN": 2,
        "ZONE_I": 2,
        "ZONE_II": 2,
        "ZONE_III": 1,
        "ZONE_IIIA": 1,
        "ZONE_IIIB": 2,
    }

    by_id = {z.key: z for z in result.zones}
    assert by_id["990000006"].attributes.zone_category == "UNKNOWN"  # "III A" is not guessed
    assert by_id["990000006"].attributes.zone_code_source == "III A"
    assert by_id["990000007"].geometry.repaired
    assert by_id["990000015"].attributes.extraction_rate is None  # remark text is not parsed
    assert by_id["990000015"].attributes.extraction_rate_status == "not_published"
    assert by_id["990000018"].geometry.source_crs == "EPSG:4326"
    assert by_id["990000019"].warnings == ("geometry_is_map_symbol",)  # remark "Symbol"
    assert all(z.geometry.geometry.is_valid for z in result.zones)

    quarantined = {(q.record_key, q.reason_code) for q in result.quarantined}
    assert ("ID_WSG_HQSG.10", "missing_source_zone_id") in quarantined  # no id -> keyed by gml:id
    assert ("990000008", "repair_exceeds_tolerance") in quarantined
    assert ("990000020.5", "invalid_source_zone_id") in quarantined
    bow_tie = next(q for q in result.quarantined if q.record_key == "990000008")
    assert bow_tie.source_geometry_wkb  # raw geometry kept as evidence
    assert bow_tie.evidence and "original_wkb_hex" in bow_tie.evidence


def test_recorded_live_excerpt_is_fully_classified(mapping: LayerMapping) -> None:
    # Eleven real TLUBN features (CC BY 4.0, (c) GDI-Th) covering the cases seen in the full dataset.
    report = IngestionReport(mode="validate")
    result = process_snapshot(retrieve_from_file(LIVE_EXCERPT, mapping, default_srs=None), mapping, POLICY, report)

    assert report.counts["accepted"] == 11
    assert report.counts["rejected"] == 0
    assert report.counts["multipart"] == 3
    assert "UNKNOWN" not in report.zone_categories

    by_id = {z.key: z for z in result.zones}  # "5.53510025E8" -> "553510025"
    symbol = by_id["553510025"]
    assert symbol.warnings == ("geometry_is_map_symbol",)
    assert symbol.geometry.geometry.area == pytest.approx(400, abs=0.1)  # 20 m x 20 m map symbol
    assert by_id["463320106"].geometry.repaired  # WSG Bretleben, zone II
    assert by_id["523023001"].attributes.zone_category == "ZONE_IIC"
    assert by_id["533333001"].attributes.zone_category == "ZONE_IIIC"
    hqsg = by_id["482991001"].attributes
    assert (hqsg.zone_category, hqsg.protection_area_id) == ("HQSG_QUANT_A", "138")  # Sg_Id "138.0"
    assert hqsg.legal_status_source == "in Planung / in Verfahren"
    assert hqsg.extraction_rate_status == "not_published"
    assert {z.attributes.source_modified_date.isoformat() for z in result.zones} == {"2026-10-13"}


def test_content_hash_ignores_run_specific_values(mapping: LayerMapping) -> None:
    first = process_snapshot(edge_snapshot(mapping), mapping, POLICY, IngestionReport(mode="validate"))
    later = replace(edge_snapshot(mapping), source_url="https://other.example/wsg?STARTINDEX=500")
    second = process_snapshot(later, mapping, POLICY, IngestionReport(mode="validate"))

    assert {z.key: z.content_hash for z in first.zones} == {z.key: z.content_hash for z in second.zones}


def test_content_hash_changes_with_content(mapping: LayerMapping) -> None:
    snapshot = edge_snapshot(mapping)
    # features[1] is 990000002, which (unlike 990000001) has no duplicate in the file.
    changed_feature = replace(
        snapshot.features[1], properties={**snapshot.features[1].properties, "Status": "aufgehoben"}
    )
    changed = replace(snapshot, features=(snapshot.features[0], changed_feature, *snapshot.features[2:]))

    before = {z.key: z.content_hash for z in process_snapshot(snapshot, mapping, POLICY, IngestionReport("v")).zones}
    after = {z.key: z.content_hash for z in process_snapshot(changed, mapping, POLICY, IngestionReport("v")).zones}

    assert before["990000002"] != after["990000002"]
    assert {k: v for k, v in before.items() if k != "990000002"} == {k: v for k, v in after.items() if k != "990000002"}


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
    assert report.schema == {
        "checked": True,
        "unmapped_properties": ["Gebiet", "Gebiet_Nr", "Gebietsart", "Herkunft", "Scale", "Symbolik", "Url"],
        "warnings": [],
    }
    assert report.mapping["schema_verified"] is True
    assert report.notes == []


@responses.activate
def test_schema_drift_fails_before_features_are_downloaded(
    settings: WfsSettings, mapping: LayerMapping, client: WfsClient, fake_wfs: Callable[[], FakeWfs]
) -> None:
    fake = fake_wfs()
    fake.schema = fake.schema.replace(b'name="Zone"', b'name="Zonen_Art"')

    report = run_ingestion(
        mode="validate",
        mapping=mapping,
        policy=POLICY,
        snapshot_provider=lambda: retrieve_from_wfs(settings, mapping, client),
    )

    assert report.status == "failed"
    assert report.error is not None and report.error["type"] == "SchemaMismatchError"
    assert "fields.zone_code -> 'Zone'" in report.error["message"]
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

    unverified = replace(mapping, schema_verified=False)
    report = run_ingestion(mode="load", mapping=unverified, policy=POLICY, snapshot_provider=must_not_run)

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
    assert report.counts["accepted"] == 10  # processing evidence is kept in the report
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
    assert len(data["rejected_samples"]) == 9
    assert data["transformations"] == [
        "EPSG:25832: none (source CRS is EPSG:25832)",
        next(t for t in data["transformations"] if t.startswith("EPSG:4326:")),
    ]
    assert report.to_json()


def test_geopackage_export(mapping: LayerMapping, tmp_path: Path) -> None:
    result = process_snapshot(edge_snapshot(mapping), mapping, POLICY, IngestionReport(mode="validate"))
    target = tmp_path / "zones.gpkg"

    assert export_geopackage(result.zones, target) == 10

    frame = gpd.read_file(target, layer="water_protection_zones")
    assert frame.crs.to_epsg() == 25832
    assert set(frame["zone_category"]) == {"UNKNOWN", "ZONE_I", "ZONE_II", "ZONE_III", "ZONE_IIIA", "ZONE_IIIB"}
    assert frame.geometry.is_valid.all()
    with pytest.raises(ValueError, match=".gpkg"):
        export_geopackage(result.zones, tmp_path / "zones.geojson")
