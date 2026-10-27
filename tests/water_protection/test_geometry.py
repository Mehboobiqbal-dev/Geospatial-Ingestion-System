from __future__ import annotations

import math

import pytest
from pyproj import Transformer
from shapely.geometry import LineString
from shapely.geometry import MultiPolygon
from shapely.geometry import Point
from shapely.geometry import Polygon

from app.water_protection.errors import RecordRejected
from app.water_protection.geometry import GeometryPolicy
from app.water_protection.geometry import normalize_geometry
from app.water_protection.wfs.gml import RawGeometry
from tests.water_protection.gml_builder import square

URN_25832 = "urn:ogc:def:crs:EPSG::25832"
URN_4326 = "urn:ogc:def:crs:EPSG::4326"
POLICY = GeometryPolicy()


def normalize(shape, srs: str | None = URN_25832, policy: GeometryPolicy = POLICY, fallback: str | None = None):
    return normalize_geometry(RawGeometry(shape=shape, srs_name=srs, gml_type=None), fallback_srs=fallback, policy=policy)


def rejected(shape, srs: str | None = URN_25832, **kwargs) -> RecordRejected:
    with pytest.raises(RecordRejected) as excinfo:
        normalize(shape, srs, **kwargs)
    return excinfo.value


def test_polygon_in_target_crs_becomes_multipolygon_unchanged() -> None:
    poly = Polygon(square(640000, 5650000, 100))
    result = normalize(poly)

    assert isinstance(result.geometry, MultiPolygon)
    assert result.part_count == 1
    assert result.geometry.geoms[0].equals_exact(poly, tolerance=0)
    assert result.area_m2 == pytest.approx(10_000)
    assert result.source_crs == "EPSG:25832"
    assert result.transformation == "none (source CRS is EPSG:25832)"
    assert not result.repaired and result.repair_evidence is None


def test_multipolygon_parts_and_holes_are_kept() -> None:
    zone = MultiPolygon(
        [
            Polygon(square(640000, 5650000, 2000), [square(640800, 5650800, 500)]),
            Polygon(square(642500, 5650000, 300)),
        ]
    )
    result = normalize(zone)
    assert result.part_count == 2
    assert result.area_m2 == pytest.approx(zone.area)
    assert len(result.geometry.geoms[0].interiors) == 1


def test_latitude_first_wgs84_is_swapped_and_transformed() -> None:
    lat, lon = 50.9800, 11.0300
    latlon = Polygon([(lat, lon), (lat, lon + 0.002), (lat + 0.0012, lon + 0.002), (lat + 0.0012, lon), (lat, lon)])

    result = normalize(latlon, URN_4326)

    expected_x, expected_y = Transformer.from_crs(4326, 25832, always_xy=True).transform(lon, lat)
    x0, y0 = result.geometry.geoms[0].exterior.coords[0]
    assert (x0, y0) == pytest.approx((expected_x, expected_y), abs=1e-6)
    assert result.axis_swapped
    assert result.source_crs == "EPSG:4326"
    assert "UTM zone 32N" in result.transformation
    # A proper transformation, not a relabelled SRID: coordinates are metres now.
    assert 640_000 < x0 < 645_000 and 5_645_000 < y0 < 5_655_000
    # ~140 m x ~133 m
    assert 15_000 < result.area_m2 < 21_000


def test_traditional_epsg_form_is_not_swapped() -> None:
    lonlat = Polygon([(11.03, 50.98), (11.032, 50.98), (11.032, 50.9812), (11.03, 50.9812), (11.03, 50.98)])
    latlon = Polygon([(y, x) for x, y in lonlat.exterior.coords])

    from_xy = normalize(lonlat, "EPSG:4326")
    from_urn = normalize(latlon, URN_4326)

    assert not from_xy.axis_swapped
    assert from_xy.geometry.equals_exact(from_urn.geometry, tolerance=1e-6)


def test_declared_default_crs_is_used_when_geometry_has_none() -> None:
    result = normalize(Polygon(square(640000, 5650000, 10)), srs=None, fallback=URN_25832)
    assert result.source_srs_name == URN_25832


@pytest.mark.parametrize(
    ("shape", "srs", "reason"),
    [
        (None, URN_25832, "missing_geometry"),
        (Polygon(), URN_25832, "empty_geometry"),
        (Polygon([(640000, 5650000), (math.nan, 5650000), (640000, 5650100), (640000, 5650000)]), URN_25832,
         "non_finite_coordinates"),
        (Polygon([(640000, 5650000), (math.inf, 5650000), (640000, 5650100), (640000, 5650000)]), URN_25832,
         "non_finite_coordinates"),
        (LineString([(640000, 5650000), (640100, 5650100)]), URN_25832, "unsupported_geometry_type"),
        (Point(640000, 5650000), URN_25832, "unsupported_geometry_type"),
        (Polygon(square(640000, 5650000, 10)), None, "unknown_crs"),
        (Polygon(square(640000, 5650000, 10)), "urn:ogc:def:crs:EPSG::999999", "unsupported_crs"),
    ],
)
def test_irrecoverable_geometries_are_rejected(shape, srs, reason: str) -> None:
    assert rejected(shape, srs).reason_code == reason


def test_parse_errors_are_passed_through() -> None:
    raw = RawGeometry(shape=None, srs_name=URN_25832, gml_type="Polygon", error_code="malformed_geometry",
                      error_detail="LinearRing is not closed")
    with pytest.raises(RecordRejected) as excinfo:
        normalize_geometry(raw, fallback_srs=None, policy=POLICY)
    assert (excinfo.value.reason_code, excinfo.value.detail) == ("malformed_geometry", "LinearRing is not closed")


def test_spike_is_repaired_with_evidence() -> None:
    spike = Polygon(
        [(644500, 5650000), (644600, 5650000), (644600, 5650050), (644650, 5650050),
         (644600, 5650050), (644600, 5650100), (644500, 5650100), (644500, 5650000)]
    )
    assert not spike.is_valid

    result = normalize(spike)

    assert result.repaired
    assert result.geometry.is_valid
    assert result.area_m2 == pytest.approx(10_000)
    evidence = result.repair_evidence
    assert evidence is not None
    assert evidence["invalid_reason"].startswith("Self-intersection")
    assert evidence["area_change_ratio"] == pytest.approx(0)
    assert evidence["original_wkb_hex"]
    assert "geometry_repaired" in result.warnings


def test_bow_tie_is_not_guessed_but_quarantined() -> None:
    bow_tie = Polygon([(645000, 5650000), (645100, 5650100), (645100, 5650000), (645000, 5650100), (645000, 5650000)])

    error = rejected(bow_tie)

    assert error.reason_code == "repair_exceeds_tolerance"
    assert error.evidence is not None
    assert error.evidence["repaired_area"] == pytest.approx(5_000)


def test_overlapping_parts_need_review_unless_tolerance_allows_union() -> None:
    overlapping = MultiPolygon([Polygon(square(640000, 5650000, 100)), Polygon(square(640050, 5650000, 100))])
    assert not overlapping.is_valid

    assert rejected(overlapping).reason_code == "repair_exceeds_tolerance"

    lenient = normalize(overlapping, policy=GeometryPolicy(max_repair_area_change_ratio=0.5))
    assert lenient.repaired
    assert lenient.area_m2 == pytest.approx(150 * 100)  # union, not even-odd hole


def test_repair_can_be_disabled() -> None:
    spike = Polygon([(0, 0), (10, 0), (10, 5), (15, 5), (10, 5), (10, 10), (0, 10), (0, 0)])
    shifted = Polygon([(x + 640000, y + 5650000) for x, y in spike.exterior.coords])
    assert rejected(shifted, policy=GeometryPolicy(allow_repair=False)).reason_code == "invalid_geometry"


def test_swapped_axes_are_caught_by_the_plausibility_extent() -> None:
    northing_first = Polygon([(y, x) for x, y in square(646000, 5650000, 100)])

    error = rejected(northing_first)

    assert error.reason_code == "outside_plausibility_extent"
    assert "axis order" in error.detail


def test_plausibility_check_can_be_disabled() -> None:
    far_away = Polygon(square(300000, 5000000, 100))
    assert normalize(far_away, policy=GeometryPolicy(plausibility_lonlat=None)).area_m2 == pytest.approx(10_000)
