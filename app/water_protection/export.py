from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import geopandas as gpd

from app.water_protection.crs import TARGET_EPSG
from app.water_protection.records import StagedZone


def zones_to_geodataframe(zones: Sequence[StagedZone]) -> gpd.GeoDataFrame:
    records = []
    for zone in zones:
        a = zone.attributes
        records.append(
            {
                "state_code": a.state_code,
                "source_zone_id": a.source_zone_id,
                "protection_area_id": a.protection_area_id,
                "protection_area_name": a.protection_area_name,
                "zone_code_source": a.zone_code_source,
                "zone_category": a.zone_category,
                "zone_legal_description": a.zone_legal_description,
                "legal_status_source": a.legal_status_source,
                "legal_effective_date": a.legal_effective_date.isoformat() if a.legal_effective_date else None,
                "extraction_rate": float(a.extraction_rate) if a.extraction_rate is not None else None,
                "extraction_rate_unit": a.extraction_rate_unit,
                "extraction_rate_status": a.extraction_rate_status,
                "source_crs": zone.geometry.source_crs,
                "geometry_repaired": zone.geometry.repaired,
                "validation_status": zone.validation_status,
                "validation_warnings": ",".join(zone.warnings),
                "area_m2": round(zone.geometry.area_m2, 2),
                "source_attributes": json.dumps(zone.source_attributes, ensure_ascii=False, default=str),
                "geometry": zone.geometry.geometry,
            }
        )
    if not records:
        return gpd.GeoDataFrame({"source_zone_id": []}, geometry=[], crs=f"EPSG:{TARGET_EPSG}")
    return gpd.GeoDataFrame(records, geometry="geometry", crs=f"EPSG:{TARGET_EPSG}")


def export_geopackage(zones: Sequence[StagedZone], path: Path) -> int:
    """Write accepted zones to a GeoPackage for visual review (dry runs)."""
    if path.suffix.lower() != ".gpkg":
        raise ValueError("export path must end in .gpkg")
    frame = zones_to_geodataframe(zones)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_file(path, driver="GPKG", layer="water_protection_zones", engine="pyogrio")
    return len(frame)
