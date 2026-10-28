"""
Integration tests against a real, disposable PostGIS database.

Run with IRIS_TEST_DATABASE_URL pointing at a database whose name ends in
"_test" (the docker compose setup creates iris_staging_test). The tests
apply the staging migrations and TRUNCATE the staging tables of that test
database between tests, so they refuse to run against anything else.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
import shapely
from sqlalchemy import Engine
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from app.database.staging.guard import StagingTarget
from app.database.staging.guard import validate_staging_target
from app.database.staging.repository import create_staging_engine
from app.database.staging.repository import quarantine_row
from app.database.staging.repository import staging_transaction
from app.database.staging.repository import zone_row
from app.water_protection import cli
from app.water_protection.config import StagingDatabaseSettings
from app.water_protection.errors import PersistenceError
from app.water_protection.geometry import GeometryPolicy
from app.water_protection.loader import load_into_staging
from app.water_protection.mapping import LayerMapping
from app.water_protection.pipeline import ProcessingResult
from app.water_protection.pipeline import process_snapshot
from app.water_protection.pipeline import retrieve_from_file
from app.water_protection.report import IngestionReport
from app.water_protection.report import utc_now
from app.water_protection.spatial import assess_parcel
from tests.water_protection.conftest import FIXTURES

pytestmark = pytest.mark.integration

TEST_URL = os.environ.get("IRIS_TEST_DATABASE_URL")
TABLES = (
    "iris_staging.water_protection_zones",
    "iris_staging.water_protection_zones_quarantine",
    "iris_staging.water_protection_zone_ingestion_runs",
)

if not TEST_URL:
    pytest.skip("IRIS_TEST_DATABASE_URL is not set", allow_module_level=True)
if not (make_url(TEST_URL).database or "").endswith("_test"):
    pytest.skip("IRIS_TEST_DATABASE_URL must name a database ending in _test", allow_module_level=True)


@pytest.fixture
def staging_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IRIS_ENVIRONMENT", "test")
    monkeypatch.setenv("IRIS_STAGING_DATABASE_URL", TEST_URL)  # type: ignore[arg-type]


@pytest.fixture
def target(staging_env: None) -> StagingTarget:
    return validate_staging_target(StagingDatabaseSettings(_env_file=None))


@pytest.fixture
def engine(target: StagingTarget) -> Iterator[Engine]:
    assert cli.main(["--log-level", "WARNING", "init-db"]) == 0
    engine = create_staging_engine(target)
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {', '.join(TABLES)}"))
    yield engine
    engine.dispose()


@pytest.fixture
def processed(mapping: LayerMapping) -> ProcessingResult:
    snapshot = retrieve_from_file(FIXTURES / "getfeature_edge_cases.xml", mapping, default_srs=None)
    return process_snapshot(snapshot, mapping, GeometryPolicy(), IngestionReport(mode="load"))


def load(engine: Engine, target: StagingTarget, result: ProcessingResult, batch_size: int = 4):
    return load_into_staging(
        lambda: staging_transaction(engine, target),
        result.zones,
        result.quarantined,
        state_code="TH",
        run_id=uuid.uuid4(),
        seen_at=utc_now(),
        batch_size=batch_size,
        zone_row=zone_row,
        quarantine_row=quarantine_row,
    )


def scalar(engine: Engine, sql: str):
    with engine.connect() as connection:
        return connection.execute(text(sql)).scalar_one()


def test_repeated_load_is_idempotent(engine: Engine, target: StagingTarget, processed: ProcessingResult) -> None:
    first = load(engine, target, processed)
    second = load(engine, target, processed)

    assert (first.inserted, first.updated, first.unchanged, first.quarantined) == (10, 0, 0, 9)
    assert (second.inserted, second.updated, second.unchanged, second.quarantined) == (0, 0, 10, 9)
    assert scalar(engine, "SELECT count(*) FROM iris_staging.water_protection_zones") == 10
    assert scalar(engine, "SELECT count(*) FROM iris_staging.water_protection_zones_quarantine") == 9
    assert scalar(engine, "SELECT max(occurrences) FROM iris_staging.water_protection_zones_quarantine") == 2
    # An unchanged row is not rewritten: updated_at still equals first_ingested_at.
    assert scalar(engine, "SELECT bool_and(updated_at = first_ingested_at) FROM iris_staging.water_protection_zones")


def test_changed_source_updates_only_that_row(
    engine: Engine, target: StagingTarget, processed: ProcessingResult, mapping: LayerMapping
) -> None:
    load(engine, target, processed)
    snapshot = retrieve_from_file(FIXTURES / "getfeature_edge_cases.xml", mapping, default_srs=None)
    features = list(snapshot.features)
    features[1] = replace(features[1], properties={**features[1].properties, "Status": "aufgehoben"})
    changed = process_snapshot(
        replace(snapshot, features=tuple(features)), mapping, GeometryPolicy(), IngestionReport(mode="load")
    )

    summary = load(engine, target, changed)

    assert (summary.inserted, summary.updated, summary.unchanged) == (0, 1, 9)
    assert (
        scalar(
            engine,
            "SELECT legal_status_source FROM iris_staging.water_protection_zones WHERE source_zone_id = '990000002'",
        )
        == "aufgehoben"
    )


def test_failure_in_a_later_batch_leaves_no_partial_data(
    engine: Engine, target: StagingTarget, processed: ProcessingResult, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.database.staging import repository

    original = repository.WaterProtectionZoneRepository.upsert_zones
    calls = {"n": 0}

    def fail_second_batch(self, rows):
        calls["n"] += 1
        if calls["n"] == 2:
            raise PersistenceError("simulated failure in batch 2")
        return original(self, rows)

    monkeypatch.setattr(repository.WaterProtectionZoneRepository, "upsert_zones", fail_second_batch)

    with pytest.raises(PersistenceError):
        load(engine, target, processed, batch_size=4)

    assert calls["n"] == 2
    assert scalar(engine, "SELECT count(*) FROM iris_staging.water_protection_zones") == 0


def test_stored_geometry_and_classification(engine: Engine, target: StagingTarget, processed: ProcessingResult) -> None:
    load(engine, target, processed)

    with engine.connect() as connection:
        rows = (
            connection.execute(
                text(
                    "SELECT source_zone_id, ST_SRID(geom) AS srid, GeometryType(geom) AS type, "
                    "ST_IsValid(geom) AS valid, ST_AsBinary(geom) AS wkb, zone_category, zone_code_source, "
                    "geometry_repaired, "
                    "geometry_repair_evidence IS NULL AS no_evidence FROM iris_staging.water_protection_zones"
                )
            )
            .mappings()
            .all()
        )

    by_id = {r["source_zone_id"]: r for r in rows}
    expected = {z.key: z for z in processed.zones}
    assert set(by_id) == set(expected)
    for key, row in by_id.items():
        assert (row["srid"], row["type"], row["valid"]) == (25832, "MULTIPOLYGON", True)
        assert shapely.from_wkb(bytes(row["wkb"])).equals_exact(expected[key].geometry.geometry, tolerance=1e-6)
        assert row["no_evidence"] is (not row["geometry_repaired"])
    assert (by_id["990000006"]["zone_category"], by_id["990000006"]["zone_code_source"]) == ("UNKNOWN", "III A")
    assert by_id["990000007"]["geometry_repaired"] is True


def test_recorded_live_excerpt_loads_with_every_category(
    engine: Engine, target: StagingTarget, mapping: LayerMapping
) -> None:
    snapshot = retrieve_from_file(FIXTURES / "live_getfeature_excerpt.xml", mapping, default_srs=None)
    live = process_snapshot(snapshot, mapping, GeometryPolicy(), IngestionReport(mode="load"))

    first = load(engine, target, live)
    second = load(engine, target, live)

    assert (first.inserted, second.unchanged) == (11, 11)
    with engine.connect() as connection:
        rows = dict(
            connection.execute(
                text(
                    "SELECT source_zone_id, zone_category || ' ' || array_to_string(validation_warnings, ',') "
                    "FROM iris_staging.water_protection_zones"
                )
            ).all()
        )
    assert rows["553510025"] == "ZONE_I geometry_is_map_symbol"
    assert rows["463320106"] == "ZONE_II geometry_repaired"
    assert rows["523023001"] == "ZONE_IIC "
    assert rows["482991001"] == "HQSG_QUANT_A "


def test_database_constraints_reject_inconsistent_rows(
    engine: Engine, target: StagingTarget, processed: ProcessingResult
) -> None:
    zone = next(z for z in processed.zones if z.key == "990000002")
    row = zone_row(zone, uuid.uuid4(), utc_now())
    row["extraction_rate"] = 500  # a rate without status "published" must never be stored

    with (
        pytest.raises(IntegrityError, match="extraction_rate_matches_status"),
        staging_transaction(engine, target) as repo,
    ):
        repo.upsert_zones([row])
    assert scalar(engine, "SELECT count(*) FROM iris_staging.water_protection_zones") == 0


def test_parcel_screening_in_postgis_matches_python(
    engine: Engine, target: StagingTarget, processed: ProcessingResult
) -> None:
    load(engine, target, processed)
    parcel = shapely.box(641020, 5651020, 641080, 5651080)

    with engine.connect() as connection:
        rows = (
            connection.execute(
                text(
                    "SELECT source_zone_id, ST_Covers(geom, p) AS within, ST_Relate(p, geom, 'T********') AS overlaps, "
                    "ST_Area(ST_Intersection(p, geom)) AS overlap FROM iris_staging.water_protection_zones, "
                    "ST_GeomFromText(:wkt, 25832) AS p WHERE ST_Intersects(geom, p)"
                ),
                {"wkt": parcel.wkt},
            )
            .mappings()
            .all()
        )

    zones = {z.key: z.geometry.geometry for z in processed.zones}
    assert {r["source_zone_id"] for r in rows} == {"990000001", "990000002"}
    for row in rows:
        python = assess_parcel(parcel, zones[row["source_zone_id"]])
        assert row["within"] is (python.relation.value == "within")
        assert row["overlap"] == pytest.approx(python.overlap_area_m2)


def test_cli_load_records_runs(
    staging_env: None,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    source = str(FIXTURES / "getfeature_edge_cases.xml")

    first = cli.main(["--log-level", "WARNING", "load", "--source-file", source])
    second = cli.main(["--log-level", "WARNING", "load", "--source-file", source])
    broken = tmp_path / "broken.xml"
    broken.write_bytes(Path(FIXTURES / "html_challenge.html").read_bytes())
    failed = cli.main(["--log-level", "WARNING", "load", "--source-file", str(broken)])
    capsys.readouterr()

    assert (first, second, failed) == (3, 3, 1)
    with engine.connect() as connection:
        runs = connection.execute(
            text(
                "SELECT status, rows_inserted, rows_unchanged, error_type "
                "FROM iris_staging.water_protection_zone_ingestion_runs ORDER BY finished_at"
            )
        ).all()
    assert [tuple(r) for r in runs] == [
        ("succeeded_with_rejections", 10, 0, None),
        ("succeeded_with_rejections", 0, 10, None),
        ("failed", None, None, "UnexpectedContentError"),
    ]
