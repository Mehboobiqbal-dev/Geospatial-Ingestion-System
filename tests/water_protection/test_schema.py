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
    return parse_feature_type_schema(fixture_bytes("describe_feature_type.xsd"), "th_wsg_zonen")


def test_layer_properties_are_read_from_the_schema(schema) -> None:
    names = [p.name for p in schema.properties]
    assert names[:3] == ["zone_id", "wsg_nr", "wsg_name"]
    assert schema.target_namespace == "https://example.invalid/wsg"

    zone = schema.get("zone")
    assert zone is not None and zone.type_label == "xsd:string"  # inline restriction base
    assert schema.get("datum_festsetzung").type_label == "xsd:date"
    geometry = schema.get("geom")
    assert geometry.is_geometry
    assert geometry.type_label == "gml:MultiSurfacePropertyType"
    assert [p.name for p in schema.geometry_properties] == ["geom"]


def test_mapping_matches_fixture_schema(schema, mapping: LayerMapping) -> None:
    check = check_schema_against_mapping(
        schema,
        required=mapping.required_properties(),
        geometry_property=mapping.geometry_property,
    )
    # Present in the source but not mapped: kept in source_attributes, reported as drift.
    assert check.unmapped_properties == ("bemerkung",)


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
        check_schema_against_mapping(schema, required=mapping.required_properties(), geometry_property="wsg_name")


def test_non_numeric_extraction_rate_property_is_flagged(schema, mapping: LayerMapping) -> None:
    check = check_schema_against_mapping(
        schema,
        required={**mapping.required_properties(), "bemerkung": "fields.extraction_rate"},
        geometry_property="geom",
        numeric=("bemerkung",),
    )
    assert check.warnings == ("mapped numeric property 'bemerkung' is declared as xsd:string",)


def test_layer_missing_from_schema() -> None:
    with pytest.raises(SchemaMismatchError, match="no top-level element named 'other_layer'"):
        parse_feature_type_schema(fixture_bytes("describe_feature_type.xsd"), "other_layer")
