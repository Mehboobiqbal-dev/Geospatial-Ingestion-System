from __future__ import annotations

import re
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.database.staging.models import EXTRACTION_RATE_STATUS_VALUES
from app.database.staging.models import ZONE_CATEGORY_VALUES
from app.water_protection.errors import MappingError
from app.water_protection.errors import RecordRejected
from app.water_protection.mapping import EXTRACTION_RATE_STATUSES
from app.water_protection.mapping import ZONE_CATEGORIES
from app.water_protection.mapping import LayerMapping
from app.water_protection.mapping import load_mapping
from app.water_protection.mapping import map_attributes
from app.water_protection.wfs.gml import RawFeature
from app.water_protection.wfs.gml import RawGeometry

DEFAULT_MAPPING = Path("app/water_protection/mappings/th_wsg_hqsg.toml")


def raw(**properties: str | None | list[str | None]) -> RawFeature:
    props: dict = {"Sz_Num": "9.90000001E8", "Zone": "I"}
    props.update(properties)
    return RawFeature(gml_id="f.1", properties=props, geometry=RawGeometry(None, None, None))


def with_extraction_rate(mapping: LayerMapping, unit: str | None = "Foerdermenge_Einheit") -> LayerMapping:
    return replace(mapping, fields=replace(mapping.fields, extraction_rate="Foerdermenge", extraction_rate_unit=unit))


def test_default_mapping_is_verified_against_the_live_schema(mapping: LayerMapping) -> None:
    assert mapping.layer == "WASSER_UND_HEILQUELLENSCHUTZGEBIETE"
    assert mapping.state_code == "TH"
    assert mapping.schema_verified is True
    assert mapping.zone_id_property == "Sz_Num"
    assert mapping.integer_identifiers == {"Sz_Num", "Sg_Id"}
    assert len(mapping.zone_classes) == 14
    assert mapping.identifier.startswith("2026-10-28.1+")
    assert mapping.fields.extraction_rate is None  # not published
    assert mapping.fields.restriction_category is None  # not published


@pytest.mark.parametrize(
    ("code", "category", "description"),
    [
        ("I", "ZONE_I", "Schutzzone I (Fassungsbereich)"),
        ("II", "ZONE_II", "Schutzzone II (Engere Schutzzone)"),
        ("IIA", "ZONE_IIA", "Schutzzone II A (Engere Schutzzone, Teilzone A)"),
        ("IIB", "ZONE_IIB", "Schutzzone II B (Engere Schutzzone, Teilzone B)"),
        ("IIC", "ZONE_IIC", "Schutzzone II C (Engere Schutzzone, Teilzone C)"),
        ("III", "ZONE_III", "Schutzzone III (Weitere Schutzzone)"),
        ("IIIA", "ZONE_IIIA", "Schutzzone III A (Weitere Schutzzone, Teilzone A)"),
        ("IIIB", "ZONE_IIIB", "Schutzzone III B (Weitere Schutzzone, Teilzone B)"),
        ("IIIC", "ZONE_IIIC", "Schutzzone III C (Weitere Schutzzone, Teilzone C)"),
        ("QUAL I", "HQSG_QUAL_I", "Heilquellenschutzgebiet, qualitative Schutzzone I"),
        ("QUAL II", "HQSG_QUAL_II", "Heilquellenschutzgebiet, qualitative Schutzzone II"),
        ("QUAL III", "HQSG_QUAL_III", "Heilquellenschutzgebiet, qualitative Schutzzone III"),
        ("QUANT A", "HQSG_QUANT_A", "Heilquellenschutzgebiet, quantitative Schutzzone A"),
        ("QUANT B", "HQSG_QUANT_B", "Heilquellenschutzgebiet, quantitative Schutzzone B"),
    ],
)
def test_official_codes_map_through_the_table(
    mapping: LayerMapping, code: str, category: str, description: str
) -> None:
    result = map_attributes(raw(Zone=code), mapping)

    assert result.zone_category == category
    assert result.zone_legal_description == description
    assert result.zone_code_source == code
    assert "GeoMIS F64A2B67" in (result.zone_classification_basis or "")  # traceable to the publisher
    assert result.warnings == ()


@pytest.mark.parametrize(
    "code",
    [
        "III A",  # spelling variant: not in the table
        "ii",  # no case folding
        "Zone II",  # no substring matching
        "IIIa",
        "III/A",
        "QUAL  I",
        "qual I",
        "QUANT C",  # not a published code
        "IV",
    ],
)
def test_unlisted_codes_stay_unknown_and_are_preserved(mapping: LayerMapping, code: str) -> None:
    result = map_attributes(raw(Zone=code), mapping)

    assert result.zone_category == "UNKNOWN"
    assert result.zone_legal_description is None
    assert result.zone_classification_basis is None
    assert result.zone_code_source == code
    assert result.warnings == ("unknown_zone_code",)


def test_outer_whitespace_is_the_only_normalisation(mapping: LayerMapping) -> None:
    result = map_attributes(raw(Zone="  II "), mapping)
    assert result.zone_category == "ZONE_II"
    assert result.zone_code_source == "  II "  # stored verbatim


@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_zone_code(mapping: LayerMapping, value: str | None) -> None:
    result = map_attributes(raw(Zone=value), mapping)
    assert result.zone_category == "UNKNOWN"
    assert result.zone_code_source is None
    assert result.warnings == ("missing_zone_code",)


def test_extraction_rate_is_never_inferred_from_other_text(mapping: LayerMapping) -> None:
    result = map_attributes(
        raw(Bemerkung="Foerdermenge 500 m3/d", Name="Brunnen 300", Symbolik="T, I, F"),
        mapping,
    )
    assert result.extraction_rate is None
    assert result.extraction_rate_unit is None
    assert result.extraction_rate_status == "not_published"


@pytest.mark.parametrize(
    ("value", "rate", "status", "warnings"),
    [
        ("1250.5", Decimal("1250.5"), "published", ()),
        ("0", Decimal("0"), "published", ()),
        (None, None, "missing", ()),
        ("", None, "missing", ()),
        ("1.234,5", None, "unparseable", ("extraction_rate_unparseable",)),
        ("ca. 500", None, "unparseable", ("extraction_rate_unparseable",)),
        ("-3", None, "unparseable", ("extraction_rate_unparseable",)),
        ("NaN", None, "unparseable", ("extraction_rate_unparseable",)),
    ],
)
def test_published_extraction_rate(
    mapping: LayerMapping, value: str | None, rate: Decimal | None, status: str, warnings: tuple[str, ...]
) -> None:
    result = map_attributes(raw(Foerdermenge=value, Foerdermenge_Einheit="m3/d"), with_extraction_rate(mapping))

    assert result.extraction_rate == rate
    assert result.extraction_rate_status == status
    assert result.warnings == warnings
    assert result.extraction_rate_unit == "m3/d"


def test_rate_without_published_unit_is_flagged(mapping: LayerMapping) -> None:
    result = map_attributes(raw(Foerdermenge="800"), with_extraction_rate(mapping, unit=None))
    assert result.extraction_rate == Decimal("800")
    assert result.extraction_rate_unit is None
    assert result.warnings == ("extraction_rate_unit_unknown",)


@pytest.mark.parametrize("value", [None, "", "  "])
def test_missing_source_id_rejects_the_record(mapping: LayerMapping, value: str | None) -> None:
    with pytest.raises(RecordRejected) as excinfo:
        map_attributes(raw(Sz_Num=value), mapping)
    assert excinfo.value.reason_code == "missing_source_zone_id"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("5.53210009E8", "553210009"),  # as the service writes xsd:double
        ("553210009", "553210009"),
        ("553210009.0", "553210009"),
        (" 4.6332E8 ", "463320000"),
        ("1.0E0", "1"),
    ],
)
def test_zone_number_is_normalised_from_its_double_form(mapping: LayerMapping, value: str, expected: str) -> None:
    assert map_attributes(raw(Sz_Num=value), mapping).source_zone_id == expected


@pytest.mark.parametrize(
    "value",
    [
        "990000020.5",
        "5.5E-1",
        "Z-1",
        "NaN",
        "Infinity",
        "1E300",
        "9007199254740993",  # 2**53 + 1: a double cannot hold it, so it may already be rounded
    ],
)
def test_zone_number_that_is_not_a_whole_number_rejects_the_record(mapping: LayerMapping, value: str) -> None:
    with pytest.raises(RecordRejected) as excinfo:
        map_attributes(raw(Sz_Num=value), mapping)
    assert excinfo.value.reason_code == "invalid_source_zone_id"


def test_protection_area_number_is_normalised(mapping: LayerMapping) -> None:
    result = map_attributes(raw(Sg_Id="101.0", Name="WSG Steinheid"), mapping)
    assert result.protection_area_id == "101"
    assert result.protection_area_name == "WSG Steinheid"

    odd = map_attributes(raw(Sg_Id="101.5"), mapping)
    assert odd.protection_area_id == "101.5"  # kept verbatim, not rounded
    assert odd.warnings == ("protection_area_id_not_integer",)


@pytest.mark.parametrize(
    ("remark", "warnings"),
    [
        ("Symbol", ("geometry_is_map_symbol",)),
        (" Symbol ", ("geometry_is_map_symbol",)),
        ("symbol", ()),
        ("Symbol; nur gedr. Karte", ()),
        ("Beschluss nichtig", ()),
        (None, ()),
    ],
)
def test_map_symbol_remark_is_flagged_exactly(
    mapping: LayerMapping, remark: str | None, warnings: tuple[str, ...]
) -> None:
    assert map_attributes(raw(Bemerkung=remark), mapping).warnings == warnings


def test_multivalued_property_rejects_the_record(mapping: LayerMapping) -> None:
    with pytest.raises(RecordRejected) as excinfo:
        map_attributes(raw(Zone=["I", "II"]), mapping)
    assert excinfo.value.reason_code == "multivalued_property"


def test_state_code_mismatch(mapping: LayerMapping) -> None:
    with_state = replace(mapping, fields=replace(mapping.fields, state_code="land"))
    assert map_attributes(raw(land="TH"), with_state).state_code == "TH"
    with pytest.raises(RecordRejected) as excinfo:
        map_attributes(raw(land="SN"), with_state)
    assert excinfo.value.reason_code == "state_code_mismatch"


@pytest.mark.parametrize(
    ("value", "expected", "warnings"),
    [
        ("2026-10-13", date(2026, 10, 13), ()),
        ("2026-10-13Z", date(2026, 10, 13), ()),
        ("2026-10-13T08:30:00+02:00", date(2026, 10, 13), ()),
        ("13.10.2026", None, ("unparseable_source_modified_date",)),
        ("2026-02-30", None, ("unparseable_source_modified_date",)),
    ],
)
def test_dates(mapping: LayerMapping, value: str, expected: date | None, warnings: tuple[str, ...]) -> None:
    result = map_attributes(raw(Stand=value), mapping)
    assert result.source_modified_date == expected
    assert result.warnings == warnings


def test_raw_values_are_kept_verbatim_and_unpublished_ones_stay_null(mapping: LayerMapping) -> None:
    result = map_attributes(raw(Name=" Synthetisches Testgebiet ", Status="in Planung / in Verfahren"), mapping)
    assert result.protection_area_name == "Synthetisches Testgebiet"
    assert result.legal_status_source == "in Planung / in Verfahren"
    assert result.legal_basis_source is None
    assert result.legal_effective_date is None
    assert result.zone_label_source is None
    assert result.restriction_category_source is None


def test_categories_and_statuses_match_database_constraints() -> None:
    assert ZONE_CATEGORIES == ZONE_CATEGORY_VALUES
    assert EXTRACTION_RATE_STATUSES == EXTRACTION_RATE_STATUS_VALUES


# ==========================================================
# Mapping file validation
# ==========================================================


def write_variant(tmp_path: Path, old: str, new: str) -> Path:
    text = DEFAULT_MAPPING.read_text(encoding="utf-8")
    assert old in text
    path = tmp_path / "mapping.toml"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ('category = "ZONE_I"', 'category = "ZONE_X"', "is not one of"),
        ('source_code = "II"', 'source_code = "I"', "defined twice"),
        ('zone_label = ""', 'zone_title = ""', "unknown=['zone_title']"),
        ('zone_id = "Sz_Num"', 'zone_id = "Sz Num"', "not a valid property name"),
        ("\nschema_verified = true", '\nschema_verified = "yes"', "must be true or false"),
        ('state_code = "TH"', 'state_code = "THU"', "two-letter"),
        (
            'integer_identifiers = ["Sz_Num", "Sg_Id"]',
            'integer_identifiers = ["Sz_Num", "Gebiet_Nr"]',
            "may only list the zone id and protection area id",
        ),
        ('warning = "geometry_is_map_symbol"', 'warning = "Map Symbol"', "lower_snake_case"),
        ('value = "Symbol"', 'value = " "', "non-empty value"),
    ],
)
def test_invalid_mapping_files_are_rejected(tmp_path: Path, old: str, new: str, message: str) -> None:
    with pytest.raises(MappingError, match=re.escape(message)):
        load_mapping(write_variant(tmp_path, old, new))


def test_editing_the_mapping_changes_its_identifier(tmp_path: Path, mapping: LayerMapping) -> None:
    edited = load_mapping(
        write_variant(
            tmp_path,
            'legal_description = "Schutzzone I (Fassungsbereich)"',
            'legal_description = "Schutzzone I (Fassungsbereich, Brunnen)"',
        )
    )
    assert edited.version == mapping.version
    assert edited.identifier != mapping.identifier


def test_identifier_ignores_line_endings_and_comments(tmp_path: Path, mapping: LayerMapping) -> None:
    text = DEFAULT_MAPPING.read_bytes().decode("utf-8").replace("\r\n", "\n")
    lf = tmp_path / "lf.toml"
    crlf = tmp_path / "crlf.toml"
    commented = tmp_path / "commented.toml"
    lf.write_bytes(text.encode())
    crlf.write_bytes(text.replace("\n", "\r\n").encode())
    commented.write_bytes((text + "\n# reviewed on 2026-10-28\n").encode())

    identifiers = {load_mapping(path).identifier for path in (lf, crlf, commented)}

    assert identifiers == {mapping.identifier}
