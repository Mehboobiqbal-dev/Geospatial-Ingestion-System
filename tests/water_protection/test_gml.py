from __future__ import annotations

import pytest
from shapely.geometry import MultiPolygon
from shapely.geometry import Polygon

from app.water_protection.errors import SchemaMismatchError
from app.water_protection.wfs.gml import parse_feature_collection
from tests.water_protection.conftest import fixture_bytes
from tests.water_protection.gml_builder import collection
from tests.water_protection.gml_builder import feature
from tests.water_protection.gml_builder import multisurface
from tests.water_protection.gml_builder import polygon
from tests.water_protection.gml_builder import square
from tests.water_protection.gml_builder import zone_properties

GML2_NS = 'xmlns:gml="http://www.opengis.net/gml"'


def parse(content: bytes):
    return parse_feature_collection(
        content, layer_local_name="WASSER_UND_HEILQUELLENSCHUTZGEBIETE", geometry_property="geom"
    )


def single(geometry_xml: str):
    return parse(collection([feature("f.1", zone_properties("9.90000001E8", "I"), geometry_xml)])).features[0]


def test_page_metadata_and_properties() -> None:
    page = parse(fixture_bytes("getfeature_page_0.xml"))

    assert page.number_matched == 5
    assert page.number_returned == 2
    assert page.time_stamp == "2026-10-27T12:00:00Z"
    assert [f.gml_id for f in page.features] == ["ID_WSG_HQSG.1", "ID_WSG_HQSG.2"]

    first = page.features[0]
    assert first.properties["Sz_Num"] == "9.90000001E8"  # verbatim; normalised by the mapping
    assert first.properties["Zone"] == "I"
    assert first.properties["Bemerkung"] is None  # xsi:nil
    assert "geom" not in first.properties
    assert isinstance(first.geometry.shape, MultiPolygon)
    assert first.geometry.srs_name == "urn:ogc:def:crs:EPSG::25832"
    assert first.geometry.error_code is None


def test_polygon_holes_and_multiple_parts() -> None:
    page = parse(fixture_bytes("getfeature_page_2.xml"))
    zone_iii = page.features[0].geometry.shape

    assert isinstance(zone_iii, MultiPolygon)
    assert len(zone_iii.geoms) == 2
    assert len(zone_iii.geoms[0].interiors) == 1
    assert zone_iii.area == pytest.approx(2000 * 2000 - 500 * 500 + 300 * 300)


def test_gml2_coordinates_are_supported() -> None:
    gml = (
        f'<gml:Polygon {GML2_NS} srsName="EPSG:25832"><gml:outerBoundaryIs><gml:LinearRing>'
        '<gml:coordinates decimal="." cs="," ts=" ">640000,5650000 640100,5650000 640100,5650100 '
        "640000,5650100 640000,5650000</gml:coordinates></gml:LinearRing></gml:outerBoundaryIs></gml:Polygon>"
    )
    parsed = single(gml)
    assert isinstance(parsed.geometry.shape, Polygon)
    assert parsed.geometry.shape.area == pytest.approx(10_000)
    assert parsed.geometry.srs_name == "EPSG:25832"


def test_pos_elements_and_3d_coordinates() -> None:
    ring = "".join(f"<gml:pos>{x} {y} 300</gml:pos>" for x, y in square(640000, 5650000, 10))
    gml = (
        '<gml:Polygon srsName="urn:ogc:def:crs:EPSG::25832" srsDimension="3"><gml:exterior>'
        f"<gml:LinearRing>{ring}</gml:LinearRing></gml:exterior></gml:Polygon>"
    )
    parsed = single(gml)
    assert parsed.geometry.shape.area == pytest.approx(100)
    assert not parsed.geometry.shape.has_z
    assert parsed.geometry.dropped_z


@pytest.mark.parametrize(
    ("geometry_xml", "code", "detail"),
    [
        (
            polygon([(0, 0), (1, 0), (1, 1), (0, 1)], srs="EPSG:25832"),
            "malformed_geometry",
            "not closed",
        ),
        (
            "<gml:Polygon><gml:exterior><gml:LinearRing><gml:posList>0 0 1 x 1 1 0 0</gml:posList>"
            "</gml:LinearRing></gml:exterior></gml:Polygon>",
            "malformed_geometry",
            "non-numeric",
        ),
        (
            "<gml:Polygon><gml:exterior><gml:Ring><gml:curveMember/></gml:Ring></gml:exterior></gml:Polygon>",
            "unsupported_geometry_type",
            "Ring",
        ),
        (
            "<gml:Solid/>",
            "unsupported_geometry_type",
            "Solid",
        ),
        (
            "<gml:MultiSurface>"
            f"<gml:surfaceMember>{polygon(square(0, 0, 1), srs='EPSG:25832')}</gml:surfaceMember>"
            f"<gml:surfaceMember>{polygon(square(0, 0, 1), srs='EPSG:4326')}</gml:surfaceMember>"
            "</gml:MultiSurface>",
            "malformed_geometry",
            "mixed srsName",
        ),
    ],
)
def test_bad_geometries_are_captured_per_feature(geometry_xml: str, code: str, detail: str) -> None:
    parsed = single(geometry_xml)
    assert parsed.geometry.shape is None
    assert parsed.geometry.error_code == code
    assert detail in (parsed.geometry.error_detail or "")
    # The feature itself (and its attributes) is still available for quarantine.
    assert parsed.properties["Sz_Num"] == "9.90000001E8"


def test_missing_and_nil_geometry() -> None:
    nil = parse(collection([feature("f.1", zone_properties("9.90000001E8", "I"), None, nil_geometry=True)])).features[0]
    absent = parse(collection([feature("f.2", zone_properties("9.90000002E8", "I"), None)])).features[0]
    for item in (nil, absent):
        assert item.geometry.shape is None
        assert item.geometry.error_code is None


def test_unknown_number_matched() -> None:
    page = parse(collection([], number_matched="unknown", number_returned=0))
    assert page.number_matched is None
    assert page.features == ()


def test_repeated_property_becomes_a_list() -> None:
    content = collection(
        [feature("f.1", zone_properties("9.90000001E8", "I"), multisurface(polygon(square(0, 0, 1))))]
    ).replace(b"<tmlnu:Zone>I</tmlnu:Zone>", b"<tmlnu:Zone>I</tmlnu:Zone><tmlnu:Zone>II</tmlnu:Zone>")
    assert parse(content).features[0].properties["Zone"] == ["I", "II"]


def test_unexpected_feature_type_aborts() -> None:
    content = collection([feature("f.1", {"Sz_Num": "1"}, None)]).replace(
        b"WASSER_UND_HEILQUELLENSCHUTZGEBIETE", b"other_type"
    )
    with pytest.raises(SchemaMismatchError, match="unexpected feature type <other_type>"):
        parse(content)
