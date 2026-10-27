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

DEFAULT_MAPPING = Path("app/water_protection/mappings/th_wsg_zonen.toml")


def raw(**properties: str | None | list[str | None]) -> RawFeature:
    props: dict = {"zone_id": "TEST-Z-001", "zone": "I"}
    props.update(properties)
    return RawFeature(gml_id="f.1", properties=props, geometry=RawGeometry(None, None, None))


def with_extraction_rate(mapping: LayerMapping, unit: str | None = "foerdermenge_einheit") -> LayerMapping:
    return replace(mapping, fields=replace(mapping.fields, extraction_rate="foerdermenge", extraction_rate_unit=unit))


def test_default_mapping_is_marked_unverified(mapping: LayerMapping) -> None:
    assert mapping.layer == "th_wsg_zonen"
    assert mapping.state_code == "TH"
    assert mapping.schema_verified is False
    assert sorted(mapping.zone_classes) == ["I", "II", "III", "IIIA", "IIIB"]
    assert mapping.identifier.startswith("2026-10-27.1+")
    assert mapping.fields.extraction_rate is None  # not known to be published


@pytest.mark.parametrize(
    ("code", "category", "description"),
    [
        ("I", "ZONE_I", "Schutzzone I (Fassungsbereich)"),
        ("II", "ZONE_II", "Schutzzone II (Engere Schutzzone)"),
        ("III", "ZONE_III", "Schutzzone III (Weitere Schutzzone)"),
        ("IIIA", "ZONE_IIIA", "Schutzzone III A (Weitere Schutzzone, Teilzone A)"),
        ("IIIB", "ZONE_IIIB", "Schutzzone III B (Weitere Schutzzone, Teilzone B)"),
    ],
)
def test_official_codes_map_through_the_table(mapping: LayerMapping, code: str, category: str, description: str) -> None:
    result = map_attributes(raw(zone=code), mapping)

    assert result.zone_category == category
    assert result.zone_legal_description == description
    assert result.zone_code_source == code
    assert "DVGW W 101" in (result.zone_classification_basis or "")
    assert result.warnings == ()


@pytest.mark.parametrize(
    "code",
    [
        "III A",  # spelling variant: not in the table
        "ii",  # no case folding
        "Zone II",  # no substring matching
        "IIIa",
        "III/A",
        "Heilquellenschutzzone I",
        "IV",
    ],
)
def test_unlisted_codes_stay_unknown_and_are_preserved(mapping: LayerMapping, code: str) -> None:
    result = map_attributes(raw(zone=code), mapping)

    assert result.zone_category == "UNKNOWN"
    assert result.zone_legal_description is None
    assert result.zone_classification_basis is None
    assert result.zone_code_source == code
    assert result.warnings == ("unknown_zone_code",)


def test_outer_whitespace_is_the_only_normalisation(mapping: LayerMapping) -> None:
    result = map_attributes(raw(zone="  II "), mapping)
    assert result.zone_category == "ZONE_II"
    assert result.zone_code_source == "  II "  # stored verbatim


@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_zone_code(mapping: LayerMapping, value: str | None) -> None:
    result = map_attributes(raw(zone=value), mapping)
    assert result.zone_category == "UNKNOWN"
    assert result.zone_code_source is None
    assert result.warnings == ("missing_zone_code",)


def test_extraction_rate_is_never_inferred_from_other_text(mapping: LayerMapping) -> None:
    result = map_attributes(
        raw(bemerkung="Foerdermenge 500 m3/d", zone_bez="Fassung mit 1200 m3/d", wsg_name="Brunnen 300"),
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
    result = map_attributes(raw(foerdermenge=value, foerdermenge_einheit="m3/d"), with_extraction_rate(mapping))

    assert result.extraction_rate == rate
    assert result.extraction_rate_status == status
    assert result.warnings == warnings
    assert result.extraction_rate_unit == "m3/d"


def test_rate_without_published_unit_is_flagged(mapping: LayerMapping) -> None:
    result = map_attributes(raw(foerdermenge="800"), with_extraction_rate(mapping, unit=None))
    assert result.extraction_rate == Decimal("800")
    assert result.extraction_rate_unit is None
    assert result.warnings == ("extraction_rate_unit_unknown",)


@pytest.mark.parametrize("value", [None, "", "  "])
def test_missing_source_id_rejects_the_record(mapping: LayerMapping, value: str | None) -> None:
    with pytest.raises(RecordRejected) as excinfo:
        map_attributes(raw(zone_id=value), mapping)
    assert excinfo.value.reason_code == "missing_source_zone_id"


def test_multivalued_property_rejects_the_record(mapping: LayerMapping) -> None:
    with pytest.raises(RecordRejected) as excinfo:
        map_attributes(raw(zone=["I", "II"]), mapping)
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
        ("2001-05-14", date(2001, 5, 14), ()),
        ("2001-05-14Z", date(2001, 5, 14), ()),
        ("2001-05-14T08:30:00+02:00", date(2001, 5, 14), ()),
        ("14.05.2001", None, ("unparseable_legal_effective_date",)),
        ("2001-02-30", None, ("unparseable_legal_effective_date",)),
    ],
)
def test_dates(mapping: LayerMapping, value: str, expected: date | None, warnings: tuple[str, ...]) -> None:
    result = map_attributes(raw(datum_festsetzung=value), mapping)
    assert result.legal_effective_date == expected
    assert result.warnings == warnings


def test_raw_values_are_kept_verbatim(mapping: LayerMapping) -> None:
    result = map_attributes(
        raw(wsg_name=" Synthetisches Testgebiet ", status="festgesetzt", rechtsgrundlage="VO vom 1.2.2001"), mapping
    )
    assert result.protection_area_name == "Synthetisches Testgebiet"
    assert result.legal_status_source == "festgesetzt"
    assert result.legal_basis_source == "VO vom 1.2.2001"


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
        ('zone_label = "zone_bez"', 'zone_title = "zone_bez"', "unknown=['zone_title']"),
        ('zone_id = "zone_id"', 'zone_id = "zone id"', "not a valid property name"),
        # Leading newline: the header comment also mentions "schema_verified = false".
        ("\nschema_verified = false", '\nschema_verified = "no"', "must be true or false"),
        ('state_code = "TH"', 'state_code = "THU"', "two-letter"),
    ],
)
def test_invalid_mapping_files_are_rejected(tmp_path: Path, old: str, new: str, message: str) -> None:
    with pytest.raises(MappingError, match=re.escape(message)):
        load_mapping(write_variant(tmp_path, old, new))


def test_editing_the_mapping_changes_its_identifier(tmp_path: Path, mapping: LayerMapping) -> None:
    edited = load_mapping(write_variant(tmp_path, 'legal_description = "Schutzzone I (Fassungsbereich)"',
                                        'legal_description = "Schutzzone I (Fassungsbereich, Brunnen)"'))
    assert edited.version == mapping.version
    assert edited.identifier != mapping.identifier
