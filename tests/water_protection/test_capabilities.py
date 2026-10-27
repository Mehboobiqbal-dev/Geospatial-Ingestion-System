from __future__ import annotations

from pathlib import Path

import pytest

from app.water_protection.errors import CapabilitiesError
from app.water_protection.errors import UnexpectedContentError
from app.water_protection.errors import WfsServiceException
from app.water_protection.wfs.capabilities import parse_capabilities
from tests.water_protection.conftest import fixture_bytes


def test_wfs200_capabilities() -> None:
    caps = parse_capabilities(fixture_bytes("capabilities_wfs200.xml"))

    assert caps.version == "2.0.0"
    assert caps.is_wfs2
    assert caps.implements_result_paging
    assert caps.implements_sorting
    assert caps.count_default == 1000
    assert "hits" in caps.result_types
    assert [ft.name for ft in caps.feature_types] == ["wsg:th_wsg_zonen", "wsg:th_wsg_gebiete"]

    layer = caps.find_feature_type("th_wsg_zonen")
    assert layer.name == "wsg:th_wsg_zonen"
    assert layer.prefix == "wsg"
    assert layer.namespace_uri == "https://example.invalid/wsg"
    assert layer.default_crs == "urn:ogc:def:crs:EPSG::25832"
    assert "urn:ogc:def:crs:EPSG::31468" in layer.other_crs
    assert layer.wgs84_bbox == (9.87, 50.20, 12.65, 51.65)
    assert caps.select_output_format(layer) == "application/gml+xml; version=3.2"


def test_wfs110_capabilities_have_no_standard_paging() -> None:
    caps = parse_capabilities(fixture_bytes("capabilities_wfs110.xml"))

    assert caps.version == "1.1.0"
    assert not caps.is_wfs2
    assert not caps.implements_result_paging
    layer = caps.find_feature_type("wsg:th_wsg_zonen")
    assert layer.default_crs == "urn:ogc:def:crs:EPSG::25832"
    assert caps.select_output_format(layer) == "text/xml; subtype=gml/3.1.1"


def test_missing_layer_lists_available_layers() -> None:
    caps = parse_capabilities(fixture_bytes("capabilities_wfs200.xml"))

    with pytest.raises(CapabilitiesError, match="wsg:th_wsg_gebiete") as excinfo:
        caps.find_feature_type("th_wsg_unknown")
    assert "not advertised" in str(excinfo.value)


def test_only_unsupported_output_formats_is_an_error() -> None:
    content = fixture_bytes("capabilities_wfs200.xml")
    content = content.replace(b"<ows:Value>application/gml+xml; version=3.2</ows:Value>", b"")
    content = content.replace(b"<ows:Value>text/xml; subtype=gml/3.1.1</ows:Value>", b"")
    caps = parse_capabilities(content)

    with pytest.raises(CapabilitiesError, match="no supported GML output format"):
        caps.select_output_format(caps.find_feature_type("th_wsg_zonen"))


def test_html_page_is_reported_as_bot_protection() -> None:
    with pytest.raises(UnexpectedContentError) as excinfo:
        parse_capabilities(fixture_bytes("html_challenge.html"))

    message = str(excinfo.value)
    assert message.startswith("GetCapabilities:")
    assert "HTML page instead of WFS XML" in message
    assert "Bot protection - CAPTCHA" in message


def test_exception_report_with_http_200_is_raised() -> None:
    with pytest.raises(WfsServiceException) as excinfo:
        parse_capabilities(fixture_bytes("exception_report.xml"))
    assert excinfo.value.code == "InvalidParameterValue"


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (b"", "empty response body"),
        (b"<WFS_Capabilities version='2.0.0'", "not well-formed XML"),
        (b"<FeatureCollection/>", "unexpected root element"),
    ],
)
def test_malformed_capabilities(content: bytes, expected: str) -> None:
    with pytest.raises(UnexpectedContentError, match=expected):
        parse_capabilities(content)


def test_unsupported_version() -> None:
    content = b'<wfs:WFS_Capabilities xmlns:wfs="http://www.opengis.net/wfs" version="1.0.0"/>'
    with pytest.raises(CapabilitiesError, match="unsupported WFS version"):
        parse_capabilities(content)


def test_external_entities_are_not_resolved(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET-CONTENT", encoding="utf-8")
    content = (
        f'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY xxe SYSTEM "{secret.as_uri()}">]>'
        '<wfs:WFS_Capabilities xmlns:wfs="http://www.opengis.net/wfs/2.0" '
        'xmlns:ows="http://www.opengis.net/ows/1.1" version="2.0.0">'
        "<ows:ServiceIdentification><ows:Title>&xxe;</ows:Title></ows:ServiceIdentification>"
        "</wfs:WFS_Capabilities>"
    ).encode()

    caps = parse_capabilities(content)

    assert caps.title is None or "TOP-SECRET-CONTENT" not in caps.title
