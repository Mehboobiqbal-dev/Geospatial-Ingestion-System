"""
Command line interface.

    python -m app.water_protection inspect   [--output FILE]
    python -m app.water_protection validate  [--source-file FILE] [--export FILE.gpkg] [--report FILE]
    python -m app.water_protection load      [--source-file FILE] [--allow-unverified-mapping] [--report FILE]
    python -m app.water_protection check-db
    python -m app.water_protection init-db

Exit codes: 0 success, 1 failure, 2 refused (configuration/safety guard),
3 completed but some features were rejected.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import Engine
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import SQLAlchemyError

from app.water_protection.config import StagingDatabaseSettings
from app.water_protection.config import WfsSettings
from app.water_protection.errors import ConfigurationError
from app.water_protection.errors import PersistenceError
from app.water_protection.errors import SourceError
from app.water_protection.errors import StagingTargetRefused
from app.water_protection.geometry import GeometryPolicy
from app.water_protection.logging_config import configure_logging
from app.water_protection.mapping import LayerMapping
from app.water_protection.mapping import load_mapping
from app.water_protection.pipeline import ProcessingResult
from app.water_protection.pipeline import SourceSnapshot
from app.water_protection.pipeline import retrieve_from_file
from app.water_protection.pipeline import retrieve_from_wfs
from app.water_protection.pipeline import run_ingestion
from app.water_protection.report import EXIT_FAILED
from app.water_protection.report import EXIT_OK
from app.water_protection.report import EXIT_REFUSED
from app.water_protection.report import IngestionReport
from app.water_protection.report import utc_now
from app.water_protection.wfs.client import WfsClient
from app.water_protection.wfs.http import WfsHttpClient

logger = logging.getLogger("app.water_protection")

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.water_protection",
        description="Thuringia water protection zones (IRIS-WSG-TH): WFS -> iris_staging.water_protection_zones",
    )
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--log-format", choices=("text", "json"), default="text")
    sub = parser.add_subparsers(dest="command", required=True)

    inspect = sub.add_parser("inspect", help="record capabilities, layer schema and sample features")
    inspect.add_argument("--output", type=Path, help="write the inspection JSON to this file")
    inspect.add_argument("--sample-size", type=int, default=3)

    for name, help_text in (
        ("validate", "dry run: retrieve, validate and report without database writes"),
        ("load", "retrieve, validate and upsert into iris_staging.water_protection_zones"),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument(
            "--source-file", type=Path, help="process a saved GetFeature response instead of the live WFS"
        )
        command.add_argument(
            "--default-srs",
            help="CRS for --source-file geometries without srsName (e.g. urn:ogc:def:crs:EPSG::25832)",
        )
        command.add_argument("--report", type=Path, help="also write the JSON report to this file")
        if name == "validate":
            command.add_argument("--export", type=Path, help="write accepted zones to a GeoPackage (.gpkg)")
        else:
            command.add_argument(
                "--allow-unverified-mapping",
                action="store_true",
                help="load although the mapping is not verified against the live schema (recorded in the report)",
            )

    sub.add_parser("check-db", help="read-only check of the staging target and tables")
    sub.add_parser("init-db", help="apply staging migrations (alembic_staging) to iris_staging only")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.log_level, args.log_format)
    try:
        settings = WfsSettings()
        mapping = load_mapping(settings.mapping_file)
        handlers: dict[str, Callable[[], int]] = {
            "inspect": lambda: cmd_inspect(args, settings, mapping),
            "validate": lambda: cmd_validate(args, settings, mapping),
            "load": lambda: cmd_load(args, settings, mapping),
            "check-db": cmd_check_db,
            "init-db": cmd_init_db,
        }
        return handlers[args.command]()
    except StagingTargetRefused as exc:
        logger.error("refused: %s", exc)
        return EXIT_REFUSED
    except (ConfigurationError, ValueError) as exc:
        logger.error("configuration error: %s", exc)
        return EXIT_REFUSED


# ==========================================================
# Commands
# ==========================================================


def _wfs_client(settings: WfsSettings) -> WfsClient:
    return WfsClient(WfsHttpClient(settings), settings.wfs_url, accept_versions=settings.versions)


def _policy(settings: WfsSettings) -> GeometryPolicy:
    return GeometryPolicy(
        allow_repair=settings.allow_geometry_repair,
        max_repair_area_change_ratio=settings.max_repair_area_change_ratio,
    )


def _snapshot_provider(
    args: argparse.Namespace, settings: WfsSettings, mapping: LayerMapping
) -> Callable[[], SourceSnapshot]:
    if args.source_file is not None:
        return lambda: retrieve_from_file(args.source_file, mapping, default_srs=args.default_srs)
    return lambda: retrieve_from_wfs(settings, mapping, _wfs_client(settings))


def _emit(report: IngestionReport, path: Path | None) -> int:
    print(report.to_json())
    if path is not None:
        report.write(path)
    logger.info(
        "run finished",
        extra={"run_id": str(report.run_id), "status": report.status, "counts": report.counts},
    )
    return report.exit_code


def cmd_inspect(args: argparse.Namespace, settings: WfsSettings, mapping: LayerMapping) -> int:
    client = _wfs_client(settings)
    inspection: dict[str, Any] = {"inspected_at": utc_now().isoformat(), "endpoint": settings.wfs_url}
    try:
        capabilities, _ = client.get_capabilities()
        inspection["capabilities"] = {
            "version": capabilities.version,
            "title": capabilities.title,
            "provider": capabilities.provider,
            "operations": list(capabilities.operations),
            "getfeature_output_formats": list(capabilities.getfeature_output_formats),
            "result_types": list(capabilities.result_types),
            "implements_result_paging": capabilities.implements_result_paging,
            "implements_sorting": capabilities.implements_sorting,
            "count_default": capabilities.count_default,
            "feature_types": [
                {
                    "name": ft.name,
                    "title": ft.title,
                    "default_crs": ft.default_crs,
                    "other_crs": list(ft.other_crs),
                    "wgs84_bbox": ft.wgs84_bbox,
                }
                for ft in capabilities.feature_types
            ],
        }
        plan = client.plan(capabilities, settings.layer, page_size=settings.page_size, sort_by=None)
        schema = client.describe_feature_type(capabilities, plan.feature_type)
        inspection["layer_schema"] = schema.to_dict()
        inspection["mapping_check"] = _mapping_check(schema, mapping)
        sample = client.fetch_sample(plan, geometry_property=mapping.geometry_property, count=args.sample_size)
        inspection["sample"] = [
            {
                "gml_id": f.gml_id,
                "properties": f.properties,
                "geometry_type": f.geometry.gml_type,
                "srs_name": f.geometry.srs_name,
            }
            for f in sample.features
        ]
        status = EXIT_OK
    except SourceError as exc:
        inspection["error"] = {"type": type(exc).__name__, "operation": exc.operation, "message": str(exc)}
        status = EXIT_FAILED

    output = json.dumps(inspection, indent=2, ensure_ascii=False, default=str)
    print(output)
    if args.output:
        args.output.write_text(output + "\n", encoding="utf-8")
    return status


def _mapping_check(schema: Any, mapping: LayerMapping) -> dict[str, Any]:
    from app.water_protection.wfs.schema import check_schema_against_mapping

    try:
        check = check_schema_against_mapping(
            schema,
            required=mapping.required_properties(),
            geometry_property=mapping.geometry_property,
            numeric=mapping.numeric_properties(),
        )
    except SourceError as exc:
        return {"matches": False, "problems": str(exc)}
    return {"matches": True, "unmapped_properties": list(check.unmapped_properties), "warnings": list(check.warnings)}


def cmd_validate(args: argparse.Namespace, settings: WfsSettings, mapping: LayerMapping) -> int:
    exporter = None
    if args.export is not None:
        from app.water_protection.export import export_geopackage

        exporter = lambda result: export_geopackage(result.zones, args.export)  # noqa: E731
    report = run_ingestion(
        mode="validate",
        mapping=mapping,
        policy=_policy(settings),
        snapshot_provider=_snapshot_provider(args, settings, mapping),
        exporter=exporter,
    )
    return _emit(report, args.report)


def cmd_load(args: argparse.Namespace, settings: WfsSettings, mapping: LayerMapping) -> int:
    from app.database.staging.guard import validate_staging_target
    from app.database.staging.repository import create_staging_engine

    db_settings = StagingDatabaseSettings()
    # Refuse unsafe targets before touching the source or the network.
    target = validate_staging_target(db_settings)
    engine = create_staging_engine(target)
    logger.info("staging target accepted", extra={"target": target.describe()})

    try:
        report = run_ingestion(
            mode="load",
            mapping=mapping,
            policy=_policy(settings),
            snapshot_provider=_snapshot_provider(args, settings, mapping),
            loader=_make_loader(engine, target, db_settings.batch_size, mapping.state_code),
            allow_unverified_mapping=args.allow_unverified_mapping,
        )
        if args.allow_unverified_mapping and not mapping.schema_verified:
            report.notes.append("Loaded with --allow-unverified-mapping.")
        _record_run(engine, target, report)
        return _emit(report, args.report)
    finally:
        engine.dispose()


def _make_loader(
    engine: Engine, target: Any, batch_size: int, state_code: str
) -> Callable[[ProcessingResult, IngestionReport], Any]:
    from app.database.staging.repository import quarantine_row
    from app.database.staging.repository import staging_transaction
    from app.database.staging.repository import zone_row
    from app.water_protection.loader import load_into_staging

    def load(result: ProcessingResult, report: IngestionReport) -> Any:
        try:
            return load_into_staging(
                lambda: staging_transaction(engine, target),
                result.zones,
                result.quarantined,
                state_code=state_code,
                run_id=report.run_id,
                seen_at=utc_now(),
                batch_size=batch_size,
                zone_row=zone_row,
                quarantine_row=quarantine_row,
            )
        except SQLAlchemyError as exc:
            raise PersistenceError(f"staging load rolled back: {_db_error(exc)}") from exc

    return load


def _record_run(engine: Engine, target: Any, report: IngestionReport) -> None:
    """Log the run in its own transaction so failed loads are recorded too."""
    from app.database.staging.repository import staging_transaction

    try:
        with staging_transaction(engine, target) as repo:
            repo.record_run(report.run_log_row())
    except (SQLAlchemyError, PersistenceError, StagingTargetRefused) as exc:
        message = _db_error(exc) if isinstance(exc, SQLAlchemyError) else str(exc)
        report.notes.append(f"Run log could not be written: {message}")
        logger.error("could not write run log", extra={"error": message})


def _db_error(exc: Exception) -> str:
    # DBAPI errors carry the bound parameters (geometries, raw attributes)
    # in str(exc); report only the driver message.
    if isinstance(exc, DBAPIError) and exc.orig is not None:
        return f"{type(exc.orig).__name__}: {str(exc.orig).strip()[:500]}"
    return f"{type(exc).__name__}: {str(exc)[:500]}"


def cmd_check_db() -> int:
    from app.database.staging.guard import validate_staging_target
    from app.database.staging.guard import verify_connection
    from app.database.staging.models import WaterProtectionZone
    from app.database.staging.repository import create_staging_engine

    target = validate_staging_target(StagingDatabaseSettings())
    engine = create_staging_engine(target)
    try:
        with engine.connect() as connection:
            info = verify_connection(connection, target)
            table = connection.execute(
                text("SELECT to_regclass('iris_staging.water_protection_zones') IS NOT NULL")
            ).scalar_one()
            rows = (
                connection.execute(select(func.count()).select_from(WaterProtectionZone)).scalar_one()
                if table
                else None
            )
            connection.rollback()
    except SQLAlchemyError as exc:
        logger.error("database check failed: %s", _db_error(exc))
        return EXIT_FAILED
    finally:
        engine.dispose()
    print(
        json.dumps(
            {"target": target.describe(), **info, "zones_table_exists": table, "zone_rows": rows}, indent=2, default=str
        )
    )
    return EXIT_OK if table else EXIT_FAILED


def cmd_init_db() -> int:
    from alembic import command
    from alembic.config import Config

    from app.database.staging.guard import validate_staging_target

    # The Alembic env re-validates; checking here gives a clean error first.
    validate_staging_target(StagingDatabaseSettings())
    config = Config(str(PROJECT_ROOT / "alembic_staging.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic_staging"))
    try:
        command.upgrade(config, "head")
    except SQLAlchemyError as exc:
        logger.error("staging migration failed: %s", _db_error(exc))
        return EXIT_FAILED
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
