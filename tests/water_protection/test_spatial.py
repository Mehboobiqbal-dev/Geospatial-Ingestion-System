from __future__ import annotations

import geopandas as gpd
import pytest
from shapely.geometry import LineString
from shapely.geometry import MultiPolygon
from shapely.geometry import Polygon
from shapely.geometry import box

from app.water_protection.spatial import ParcelZoneRelation
from app.water_protection.spatial import assess_parcel
from app.water_protection.spatial import parcel_intersects_zone
from app.water_protection.spatial import parcel_within_zone
from app.water_protection.spatial import screen_parcels

X, Y = 640000, 5650000
ZONE = box(X, Y, X + 100, Y + 100)


def parcel(x0: float, y0: float, x1: float, y1: float) -> Polygon:
    return box(X + x0, Y + y0, X + x1, Y + y1)


@pytest.mark.parametrize(
    ("name", "geom", "relation", "within", "intersects", "touches_or_intersects"),
    [
        ("inside", parcel(10, 10, 20, 20), ParcelZoneRelation.WITHIN, True, True, True),
        ("inside, sharing the zone edge", parcel(0, 0, 20, 20), ParcelZoneRelation.WITHIN, True, True, True),
        ("identical to the zone", parcel(0, 0, 100, 100), ParcelZoneRelation.WITHIN, True, True, True),
        ("outside, sharing an edge", parcel(100, 0, 150, 50), ParcelZoneRelation.BOUNDARY_CONTACT, False, False, True),
        (
            "outside, touching a corner",
            parcel(100, 100, 120, 120),
            ParcelZoneRelation.BOUNDARY_CONTACT,
            False,
            False,
            True,
        ),
        ("half inside", parcel(50, 0, 150, 100), ParcelZoneRelation.PARTIAL_OVERLAP, False, True, True),
        ("covering the zone", parcel(-10, -10, 110, 110), ParcelZoneRelation.PARTIAL_OVERLAP, False, True, True),
        ("disjoint", parcel(200, 200, 210, 210), ParcelZoneRelation.DISJOINT, False, False, False),
    ],
)
def test_containment_and_intersection_are_different(
    name: str, geom: Polygon, relation: ParcelZoneRelation, within: bool, intersects: bool, touches_or_intersects: bool
) -> None:
    assert assess_parcel(geom, ZONE).relation is relation, name
    assert parcel_within_zone(geom, ZONE) is within
    assert parcel_intersects_zone(geom, ZONE) is intersects
    assert parcel_intersects_zone(geom, ZONE, include_boundary_contact=True) is touches_or_intersects


def test_partial_overlap_area_and_ratio() -> None:
    result = assess_parcel(parcel(50, 0, 150, 100), ZONE)
    assert result.overlap_area_m2 == pytest.approx(5_000)
    assert result.parcel_area_m2 == pytest.approx(10_000)
    assert result.overlap_ratio == pytest.approx(0.5)
    assert result.affected


def test_zone_holes_are_respected() -> None:
    zone_with_hole = Polygon(ZONE.exterior.coords, [parcel(40, 40, 60, 60).exterior.coords])

    in_hole = assess_parcel(parcel(45, 45, 55, 55), zone_with_hole)
    filling_hole = assess_parcel(parcel(40, 40, 60, 60), zone_with_hole)
    across_hole_edge = assess_parcel(parcel(30, 45, 50, 55), zone_with_hole)

    assert in_hole.relation is ParcelZoneRelation.DISJOINT
    assert filling_hole.relation is ParcelZoneRelation.BOUNDARY_CONTACT
    assert not filling_hole.affected
    assert across_hole_edge.relation is ParcelZoneRelation.PARTIAL_OVERLAP
    assert across_hole_edge.overlap_area_m2 == pytest.approx(10 * 10)


def test_multipart_zone() -> None:
    zone = MultiPolygon([box(X, Y, X + 100, Y + 100), box(X + 200, Y, X + 300, Y + 100)])

    inside_second_part = assess_parcel(parcel(210, 10, 220, 20), zone)
    spanning_gap = assess_parcel(parcel(50, 0, 250, 100), zone)
    in_gap = assess_parcel(parcel(120, 10, 180, 20), zone)

    assert inside_second_part.relation is ParcelZoneRelation.WITHIN
    assert spanning_gap.relation is ParcelZoneRelation.PARTIAL_OVERLAP
    assert spanning_gap.overlap_area_m2 == pytest.approx(2 * 50 * 100)
    assert in_gap.relation is ParcelZoneRelation.DISJOINT


def test_multipart_parcel_within_zone() -> None:
    two_pieces = MultiPolygon([parcel(10, 10, 20, 20), parcel(60, 60, 70, 70)])
    assert parcel_within_zone(two_pieces, ZONE)


@pytest.mark.parametrize(
    "bad",
    [
        LineString([(X, Y), (X + 1, Y + 1)]),
        Polygon(),
        Polygon([(X, Y), (X + 1, Y + 1), (X + 1, Y), (X, Y + 1), (X, Y)]),
    ],
)
def test_invalid_inputs_are_refused(bad) -> None:
    with pytest.raises(ValueError):
        assess_parcel(bad, ZONE)


def zones_frame() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {
            "source_zone_id": ["Z-I", "Z-II"],
            "zone_category": ["ZONE_I", "ZONE_II"],
        },
        geometry=[box(X, Y, X + 50, Y + 50), box(X, Y, X + 200, Y + 200).difference(box(X, Y, X + 50, Y + 50))],
        crs="EPSG:25832",
    )


def test_screen_parcels_classifies_every_candidate_pair() -> None:
    parcels = gpd.GeoDataFrame(
        {"parcel_id": ["P-in-I", "P-across", "P-edge", "P-far"]},
        geometry=[parcel(10, 10, 20, 20), parcel(40, 40, 60, 60), parcel(200, 0, 260, 50), parcel(900, 900, 910, 910)],
        crs="EPSG:25832",
    )

    result = screen_parcels(parcels, zones_frame(), parcel_id_column="parcel_id")
    rows = {(r.parcel_id, r.source_zone_id): r for r in result.itertuples()}

    assert set(rows) == {("P-in-I", "Z-I"), ("P-across", "Z-I"), ("P-across", "Z-II")}
    assert rows[("P-in-I", "Z-I")].relation == "within"
    assert rows[("P-across", "Z-I")].overlap_area_m2 == pytest.approx(100)
    assert rows[("P-across", "Z-II")].overlap_area_m2 == pytest.approx(300)
    assert rows[("P-across", "Z-II")].zone_category == "ZONE_II"

    with_contact = screen_parcels(parcels, zones_frame(), parcel_id_column="parcel_id", include_boundary_contact=True)
    assert ("P-edge", "Z-II", "boundary_contact") in {
        (r.parcel_id, r.source_zone_id, r.relation) for r in with_contact.itertuples()
    }


def test_screen_parcels_reprojects_parcels_and_requires_crs() -> None:
    parcels_25832 = gpd.GeoDataFrame({"parcel_id": ["P"]}, geometry=[parcel(10, 10, 20, 20)], crs="EPSG:25832")
    parcels_4326 = parcels_25832.to_crs(4326)

    result = screen_parcels(parcels_4326, zones_frame(), parcel_id_column="parcel_id")
    assert result.loc[0, "relation"] == "within"

    with pytest.raises(ValueError, match="no CRS"):
        screen_parcels(parcels_25832.set_crs(None, allow_override=True), zones_frame(), parcel_id_column="parcel_id")
    with pytest.raises(ValueError, match="EPSG:25832"):
        screen_parcels(parcels_25832, zones_frame().to_crs(4326), parcel_id_column="parcel_id")
