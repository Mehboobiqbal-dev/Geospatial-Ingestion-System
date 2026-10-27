"""
Geometry validation, conservative repair and transformation to EPSG:25832.

Order of operations for each feature:

1. reject missing, empty, non-finite and non-polygonal geometries;
2. bring coordinates into x/y order according to the srsName;
3. validate in the source CRS and, if allowed, repair with
   ``make_valid(method="structure")``. A repair is accepted only when
   the result is a valid, non-empty (Multi)Polygon whose area differs
   from the original by at most ``max_repair_area_change_ratio``.
   Everything else (bow-ties, overlapping parts, collapsed rings) is
   quarantined for manual review because the "correct" boundary of a
   legal zone cannot be decided automatically;
4. transform with pyproj and re-validate;
5. check the result lies inside a generous plausibility extent around
   Thuringia, which catches swapped axes and mislabelled CRSs;
6. normalise to MultiPolygon for a uniform column type.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import Any

import numpy as np
import shapely
from shapely.geometry import MultiPolygon
from shapely.geometry import Polygon
from shapely.geometry.base import BaseGeometry

from app.water_protection.crs import TARGET_CRS_LABEL
from app.water_protection.crs import CrsReference
from app.water_protection.crs import UnsupportedCrsError
from app.water_protection.crs import get_transformer
from app.water_protection.crs import parse_srs_name
from app.water_protection.crs import target_bounds_from_lonlat
from app.water_protection.crs import transformation_description
from app.water_protection.errors import RecordRejected
from app.water_protection.wfs.gml import RawGeometry

# Generous lon/lat box around Thuringia (approx. 9.9-12.7 E, 50.2-51.7 N)
# used only as a plausibility screen, not as a clipping boundary.
THURINGIA_PLAUSIBILITY_LONLAT = (9.5, 49.9, 13.0, 52.0)

REPAIR_METHOD = "shapely.make_valid(method='structure', keep_collapsed=False)"


@dataclass(frozen=True)
class GeometryPolicy:
    allow_repair: bool = True
    max_repair_area_change_ratio: float = 0.001
    plausibility_lonlat: tuple[float, float, float, float] | None = THURINGIA_PLAUSIBILITY_LONLAT


@dataclass(frozen=True)
class NormalizedGeometry:
    geometry: MultiPolygon  # EPSG:25832
    source_crs: str  # e.g. "EPSG:25832"
    source_srs_name: str
    axis_swapped: bool
    transformation: str
    repaired: bool
    repair_evidence: dict[str, Any] | None
    part_count: int
    area_m2: float
    warnings: tuple[str, ...] = field(default_factory=tuple)


def normalize_geometry(
    raw: RawGeometry,
    *,
    fallback_srs: str | None,
    policy: GeometryPolicy,
) -> NormalizedGeometry:
    if raw.error_code:
        raise RecordRejected(raw.error_code, raw.error_detail or "geometry could not be parsed")
    geom = raw.shape
    if geom is None:
        raise RecordRejected("missing_geometry", "feature has no geometry")

    srs_name = raw.srs_name or fallback_srs
    if not srs_name:
        raise RecordRejected("unknown_crs", "no srsName on the geometry and no declared default CRS")
    try:
        crs = parse_srs_name(srs_name)
    except UnsupportedCrsError as exc:
        raise RecordRejected("unsupported_crs", str(exc)) from exc

    if geom.is_empty:
        raise RecordRejected("empty_geometry", f"empty {geom.geom_type}")
    if not np.isfinite(shapely.get_coordinates(geom)).all():
        raise RecordRejected("non_finite_coordinates", "geometry contains NaN or infinite coordinates")
    if not isinstance(geom, (Polygon, MultiPolygon)):
        raise RecordRejected("unsupported_geometry_type", f"{geom.geom_type} is not a polygon")

    warnings: list[str] = []
    if raw.dropped_z:
        warnings.append("z_coordinates_dropped")

    if crs.needs_axis_swap:
        geom = shapely.transform(geom, lambda coords: coords[:, ::-1])

    repaired = False
    evidence: dict[str, Any] | None = None
    if not geom.is_valid:
        geom, evidence = _repair(geom, crs, policy)
        repaired = True
        warnings.append("geometry_repaired")

    geom = _transform(geom, crs)
    if not geom.is_valid:
        raise RecordRejected(
            "invalid_after_transformation",
            f"geometry became invalid after transformation: {shapely.is_valid_reason(geom)}",
        )

    if policy.plausibility_lonlat is not None:
        minx, miny, maxx, maxy = target_bounds_from_lonlat(policy.plausibility_lonlat)
        gx0, gy0, gx1, gy1 = geom.bounds
        if gx0 < minx or gy0 < miny or gx1 > maxx or gy1 > maxy:
            raise RecordRejected(
                "outside_plausibility_extent",
                f"bounds {tuple(round(v, 1) for v in geom.bounds)} in {TARGET_CRS_LABEL} lie outside "
                "the plausibility extent; check axis order and declared CRS",
            )

    multi = geom if isinstance(geom, MultiPolygon) else MultiPolygon([geom])
    return NormalizedGeometry(
        geometry=multi,
        source_crs=crs.label,
        source_srs_name=crs.srs_name,
        axis_swapped=crs.needs_axis_swap,
        transformation=transformation_description(crs.epsg),
        repaired=repaired,
        repair_evidence=evidence,
        part_count=len(multi.geoms),
        area_m2=float(multi.area),
        warnings=tuple(warnings),
    )


def _repair(
    geom: BaseGeometry,
    crs: CrsReference,
    policy: GeometryPolicy,
) -> tuple[BaseGeometry, dict[str, Any]]:
    reason = shapely.is_valid_reason(geom)
    if not policy.allow_repair:
        raise RecordRejected("invalid_geometry", reason)

    candidate = shapely.make_valid(geom, method="structure", keep_collapsed=False)
    polygonal, dropped = _polygonal_parts(candidate)

    original_area = float(geom.area)
    repaired_area = float(polygonal.area) if polygonal is not None else 0.0
    change = (
        abs(repaired_area - original_area) / original_area if original_area > 0 else float("inf")
    )
    evidence: dict[str, Any] = {
        "invalid_reason": reason,
        "method": REPAIR_METHOD,
        "geos_version": shapely.geos_version_string,
        "repair_crs": crs.label,
        "original_area": original_area,
        "repaired_area": repaired_area,
        "area_change_ratio": change if change != float("inf") else None,
        "dropped_non_polygonal_parts": dropped,
        "original_wkb_hex": shapely.to_wkb(geom, hex=True),
    }

    if polygonal is None or polygonal.is_empty or not polygonal.is_valid:
        raise RecordRejected("irreparable_geometry", f"repair produced no valid polygon ({reason})", evidence)
    if change > policy.max_repair_area_change_ratio:
        raise RecordRejected(
            "repair_exceeds_tolerance",
            f"repair would change the area by "
            f"{'an undefined ratio' if change == float('inf') else f'{change:.4%}'} "
            f"(limit {policy.max_repair_area_change_ratio:.4%}); invalid reason: {reason}",
            evidence,
        )
    return polygonal, evidence


def _polygonal_parts(geom: BaseGeometry) -> tuple[Polygon | MultiPolygon | None, int]:
    if isinstance(geom, (Polygon, MultiPolygon)):
        return geom, 0
    polygons: list[Polygon] = []
    dropped = 0
    for part in getattr(geom, "geoms", []):
        if isinstance(part, Polygon):
            polygons.append(part)
        elif isinstance(part, MultiPolygon):
            polygons.extend(part.geoms)
        else:
            dropped += 1
    if not polygons:
        return None, dropped
    return (polygons[0] if len(polygons) == 1 else MultiPolygon(polygons)), dropped


def _transform(geom: BaseGeometry, crs: CrsReference) -> BaseGeometry:
    transformer = get_transformer(crs.epsg)
    if transformer is None:
        return geom

    def apply(coords: np.ndarray) -> np.ndarray:
        x, y = transformer.transform(coords[:, 0], coords[:, 1], errcheck=False)
        return np.column_stack([x, y])

    result = shapely.transform(geom, apply)
    if not np.isfinite(shapely.get_coordinates(result)).all():
        raise RecordRejected(
            "transformation_failed",
            f"transformation {crs.label} -> {TARGET_CRS_LABEL} produced non-finite coordinates",
        )
    return result
