"""
Parcel-level screening against water protection zones.

Containment and intersection are deliberately different questions:

* ``parcel_within_zone`` - the whole parcel lies inside the zone
  (``zone.covers(parcel)``); a parcel whose edge runs along the zone
  boundary is still inside.
* ``parcel_intersects_zone`` - the parcel and the zone share area
  (DE-9IM interior/interior intersection, ``T********``). Touching
  only along an edge or at a corner is *not* an intersection here,
  because a shared boundary implies no restricted area on the parcel.
  ``include_boundary_contact=True`` switches to the plain topological
  ``intersects`` for callers that need it.

All geometries must be in the same projected CRS (EPSG:25832) so that
areas are in square metres.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import geopandas as gpd
import pandas as pd
from shapely.geometry import MultiPolygon
from shapely.geometry import Polygon
from shapely.geometry.base import BaseGeometry

from app.water_protection.crs import TARGET_EPSG

INTERIOR_OVERLAP = "T********"


class ParcelZoneRelation(StrEnum):
    DISJOINT = "disjoint"
    BOUNDARY_CONTACT = "boundary_contact"
    PARTIAL_OVERLAP = "partial_overlap"
    WITHIN = "within"


@dataclass(frozen=True)
class ParcelZoneAssessment:
    relation: ParcelZoneRelation
    overlap_area_m2: float
    parcel_area_m2: float

    @property
    def overlap_ratio(self) -> float:
        return self.overlap_area_m2 / self.parcel_area_m2 if self.parcel_area_m2 else 0.0

    @property
    def affected(self) -> bool:
        return self.relation in (ParcelZoneRelation.PARTIAL_OVERLAP, ParcelZoneRelation.WITHIN)


def _require_area(geom: BaseGeometry, name: str) -> None:
    if not isinstance(geom, (Polygon, MultiPolygon)):
        raise ValueError(f"{name} must be a Polygon or MultiPolygon, got {geom.geom_type}")
    if geom.is_empty or not geom.is_valid or geom.area <= 0:
        raise ValueError(f"{name} must be a valid, non-empty polygon with positive area")


def parcel_within_zone(parcel: BaseGeometry, zone: BaseGeometry) -> bool:
    _require_area(parcel, "parcel")
    _require_area(zone, "zone")
    return bool(zone.covers(parcel))


def parcel_intersects_zone(
    parcel: BaseGeometry,
    zone: BaseGeometry,
    *,
    include_boundary_contact: bool = False,
) -> bool:
    _require_area(parcel, "parcel")
    _require_area(zone, "zone")
    if include_boundary_contact:
        return bool(parcel.intersects(zone))
    return bool(parcel.relate_pattern(zone, INTERIOR_OVERLAP))


def assess_parcel(parcel: BaseGeometry, zone: BaseGeometry) -> ParcelZoneAssessment:
    _require_area(parcel, "parcel")
    _require_area(zone, "zone")
    parcel_area = float(parcel.area)
    if not parcel.intersects(zone):
        return ParcelZoneAssessment(ParcelZoneRelation.DISJOINT, 0.0, parcel_area)
    if not parcel.relate_pattern(zone, INTERIOR_OVERLAP):
        return ParcelZoneAssessment(ParcelZoneRelation.BOUNDARY_CONTACT, 0.0, parcel_area)
    if zone.covers(parcel):
        return ParcelZoneAssessment(ParcelZoneRelation.WITHIN, parcel_area, parcel_area)
    overlap = float(parcel.intersection(zone).area)
    return ParcelZoneAssessment(ParcelZoneRelation.PARTIAL_OVERLAP, overlap, parcel_area)


def screen_parcels(
    parcels: gpd.GeoDataFrame,
    zones: gpd.GeoDataFrame,
    *,
    parcel_id_column: str,
    zone_id_column: str = "source_zone_id",
    zone_columns: tuple[str, ...] = ("zone_category",),
    include_boundary_contact: bool = False,
) -> pd.DataFrame:
    """
    Screen many parcels against many zones.

    A spatial-index join (GeoPandas ``sjoin``) finds candidate pairs;
    each pair is then classified with ``assess_parcel``. Parcels in a
    different CRS are reprojected to EPSG:25832 with a proper
    transformation; layers without a CRS are refused.
    """

    for name, frame in (("parcels", parcels), ("zones", zones)):
        if frame.crs is None:
            raise ValueError(f"{name} GeoDataFrame has no CRS; set it explicitly")
    if zones.crs.to_epsg() != TARGET_EPSG:
        raise ValueError(f"zones must be in EPSG:{TARGET_EPSG}, got {zones.crs}")
    if parcels.crs.to_epsg() != TARGET_EPSG:
        parcels = parcels.to_crs(epsg=TARGET_EPSG)

    zone_frame = zones[[zone_id_column, *zone_columns, "geometry"]].rename_geometry("zone_geometry")
    zone_frame = zone_frame.set_geometry("zone_geometry")
    candidates = gpd.sjoin(
        parcels[[parcel_id_column, "geometry"]],
        zone_frame,
        how="inner",
        predicate="intersects",
    )

    rows: list[dict[str, object]] = []
    zone_geoms = zone_frame.geometry
    for _, candidate in candidates.iterrows():
        zone_geom = zone_geoms.loc[candidate["index_right"]]
        assessment = assess_parcel(candidate.geometry, zone_geom)
        if assessment.relation is ParcelZoneRelation.BOUNDARY_CONTACT and not include_boundary_contact:
            continue
        rows.append(
            {
                parcel_id_column: candidate[parcel_id_column],
                zone_id_column: candidate[zone_id_column],
                **{column: candidate[column] for column in zone_columns},
                "relation": assessment.relation.value,
                "overlap_area_m2": round(assessment.overlap_area_m2, 3),
                "overlap_ratio": round(assessment.overlap_ratio, 6),
            }
        )
    columns = [parcel_id_column, zone_id_column, *zone_columns, "relation", "overlap_area_m2", "overlap_ratio"]
    return pd.DataFrame(rows, columns=columns)
