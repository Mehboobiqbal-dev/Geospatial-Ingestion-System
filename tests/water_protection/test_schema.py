from __future__ import annotations

from dataclasses import replace

import pytest

from app.water_protection.errors import SchemaMismatchError
from app.water_protection.mapping import LayerMapping
from app.water_protection.wfs.schema import check_schema_against_mapping
from app.water_protection.wfs.schema import parse_feature_type_schema
from tests.water_protection.conftest import fixture_bytes


@pytest.fixture
def schema():
    return parse_feature_type_schema(fixture_bytes("describe_feature_type.xsd"), "WASSER_UND_HEILQUELLENSCHUTZGEBIETE")


def test_layer_properties_are_read_from_the_schema(schema) -> None:
    # describe_feature_type.xsd is the recorded live schema (default XSD namespace, no prefix).
    names = [p.name for p in schema.properties]
    assert names[:3] == ["Gebiet_Nr", "Gebiet", "Sg_Id"]
    assert schema.target_namespace == "http://www.geoproxy.geoportal-th.de/tmlnu"

    assert schema.get("Sz_Num").type_label == "xsd:double"
    assert schema.get("Zone").type_label == "xsd:string"
    assert schema.get("Stand").type_label == "xsd:date"
    geometry = schema.get("geom")
    assert geometry.is_geometry
    assert geometry.type_label == "gml:GeometryPropertyType"
    assert [p.name for p in schema.geometry_properties] == ["geom"]


def test_inline_simple_type_uses_its_restriction_base() -> None:
    content = b"""<xsd:schema xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:gml="http://www.opengis.net/gml/3.2"
        xmlns:t="https://wfs.test/t" targetNamespace="https://wfs.test/t">
      <xsd:complexType name="LType"><xsd:complexContent><xsd:extension base="gml:AbstractFeatureType"><xsd:sequence>
        <xsd:element name="Zone"><xsd:simpleType><xsd:restriction base="xsd:string"><xsd:maxLength value="10"/>
        </xsd:restriction></xsd:simpleType></xsd:element>
      </xsd:sequence></xsd:extension></xsd:complexContent></xsd:complexType>
      <xsd:element name="L" type="t:LType" substitutionGroup="gml:AbstractFeature"/>
    </xsd:schema>"""

    assert parse_feature_type_schema(content, "L").get("Zone").type_label == "xsd:string"


def test_mapping_matches_live_schema(schema, mapping: LayerMapping) -> None:
    check = check_schema_against_mapping(
        schema,
        required=mapping.required_properties(),
        geometry_property=mapping.geometry_property,
        numeric=mapping.numeric_properties(),
    )
    # Present in the source but not mapped: kept in source_attributes, reported as drift.
    assert check.unmapped_properties == ("Gebiet", "Gebiet_Nr", "Gebietsart", "Herkunft", "Scale", "Symbolik", "Url")
    assert check.warnings == ()  # Sz_Num and Sg_Id are numeric, as integer identifiers must be


def test_missing_mapped_properties_are_all_reported(schema, mapping: LayerMapping) -> None:
    fields = replace(mapping.fields, zone_code="zonen_art", legal_status="rechtsstatus")
    broken = replace(mapping, fields=fields)

    with pytest.raises(SchemaMismatchError) as excinfo:
        check_schema_against_mapping(
            schema, required=broken.required_properties(), geometry_property=broken.geometry_property
        )

    message = str(excinfo.value)
    assert "fields.zone_code -> 'zonen_art'" in message
    assert "fields.legal_status -> 'rechtsstatus'" in message
    assert "Available properties:" in message


def test_geometry_property_must_have_a_geometry_type(schema, mapping: LayerMapping) -> None:
    with pytest.raises(SchemaMismatchError, match="non-geometry type"):
        check_schema_against_mapping(schema, required=mapping.required_properties(), geometry_property="Name")


def test_non_numeric_extraction_rate_property_is_flagged(schema, mapping: LayerMapping) -> None:
    check = check_schema_against_mapping(
        schema,
        required={**mapping.required_properties(), "Symbolik": "fields.extraction_rate"},
        geometry_property="geom",
        numeric=("Symbolik",),
    )
    assert check.warnings == ("mapped numeric property 'Symbolik' is declared as xsd:string",)


def test_layer_missing_from_schema() -> None:
    with pytest.raises(SchemaMismatchError, match="no top-level element named 'other_layer'"):
        parse_feature_type_schema(fixture_bytes("describe_feature_type.xsd"), "other_layer")
