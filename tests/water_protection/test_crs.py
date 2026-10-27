from __future__ import annotations

import pytest
from pyproj import Transformer

from app.water_protection.crs import UnsupportedCrsError
from app.water_protection.crs import get_transformer
from app.water_protection.crs import parse_srs_name
from app.water_protection.crs import transformation_description


@pytest.mark.parametrize(
    ("srs_name", "epsg", "authority_order", "swap"),
    [
        ("urn:ogc:def:crs:EPSG::25832", 25832, True, False),
        ("urn:ogc:def:crs:EPSG:6.9:25832", 25832, True, False),
        ("http://www.opengis.net/def/crs/EPSG/0/25832", 25832, True, False),
        ("EPSG:25832", 25832, False, False),
        ("urn:ogc:def:crs:EPSG::4326", 4326, True, True),  # latitude first
        ("http://www.opengis.net/def/crs/EPSG/0/4258", 4258, True, True),
        ("EPSG:4326", 4326, False, False),  # traditional lon/lat
        ("http://www.opengis.net/gml/srs/epsg.xml#4326", 4326, False, False),
        ("urn:ogc:def:crs:OGC:1.3:CRS84", 4326, False, False),
        ("urn:ogc:def:crs:EPSG::31468", 31468, True, True),  # Gauss-Krueger: northing first
    ],
)
def test_srs_name_forms_and_axis_order(srs_name: str, epsg: int, authority_order: bool, swap: bool) -> None:
    ref = parse_srs_name(srs_name)
    assert ref.epsg == epsg
    assert ref.authority_axis_order is authority_order
    assert ref.needs_axis_swap is swap


@pytest.mark.parametrize("srs_name", ["EPSG:999999", "urn:ogc:def:crs:XYZ::1", "WGS84", ""])
def test_unsupported_srs_names(srs_name: str) -> None:
    with pytest.raises(UnsupportedCrsError):
        parse_srs_name(srs_name)


def test_target_crs_needs_no_transformation() -> None:
    assert get_transformer(25832) is None
    assert transformation_description(25832) == "none (source CRS is EPSG:25832)"


def test_utm32_central_meridian_maps_to_false_easting() -> None:
    # Independent check of the projection: 9 deg E is UTM zone 32's central
    # meridian, which maps to the false easting of exactly 500 000 m; the
    # equator maps to northing 0.
    transformer = get_transformer(4258)
    assert transformer is not None
    x, y = transformer.transform(9.0, 0.0)
    assert x == pytest.approx(500_000.0, abs=1e-6)
    assert y == pytest.approx(0.0, abs=1e-6)
    x, _ = transformer.transform(9.0, 51.0)
    assert x == pytest.approx(500_000.0, abs=1e-6)


def test_round_trip_through_wgs84() -> None:
    forward = get_transformer(4326)
    assert forward is not None
    inverse = Transformer.from_crs(25832, 4326, always_xy=True)
    x, y = forward.transform(11.0300, 50.9800)  # near Erfurt
    lon, lat = inverse.transform(x, y)
    assert (lon, lat) == pytest.approx((11.0300, 50.9800), abs=1e-9)
    assert 640_000 < x < 645_000
    assert 5_645_000 < y < 5_655_000


def test_transformation_description_names_the_operation() -> None:
    assert "UTM zone 32N" in transformation_description(4326)
