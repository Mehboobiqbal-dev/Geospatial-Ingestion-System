"""
Source-to-destination attribute mapping.

The mapping is data (``mappings/th_wsg_hqsg.toml``), not code, so it
can be audited and corrected without touching the pipeline. This
module loads and validates it and applies it to raw features.

Legal classification rule: a zone code is classified only through an
exact entry in the mapping table. Unknown or missing codes are kept
verbatim with category ``UNKNOWN``; they are never guessed from a
label, a substring or a similar-looking code.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass
from dataclasses import fields as dataclass_fields
from datetime import date
from decimal import Decimal
from decimal import InvalidOperation
from importlib import resources
from pathlib import Path
from typing import Any

from app.water_protection.errors import MappingError
from app.water_protection.errors import RecordRejected
from app.water_protection.wfs.gml import RawFeature

# Must stay in sync with the CHECK constraint in the staging schema
# (verified by tests/water_protection/test_staging_sql.py).
ZONE_CATEGORIES = (
    "ZONE_I",
    "ZONE_II",
    "ZONE_IIA",
    "ZONE_IIB",
    "ZONE_IIC",
    "ZONE_III",
    "ZONE_IIIA",
    "ZONE_IIIB",
    "ZONE_IIIC",
    "HQSG_QUAL_I",
    "HQSG_QUAL_II",
    "HQSG_QUAL_III",
    "HQSG_QUANT_A",
    "HQSG_QUANT_B",
    "UNKNOWN",
)
UNKNOWN_CATEGORY = "UNKNOWN"

EXTRACTION_RATE_STATUSES = ("published", "missing", "unparseable", "not_published")

_PROPERTY_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-:]*$")
_WARNING_CODE = re.compile(r"^[a-z][a-z0-9_]*$")
_DECIMAL = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")
_XSD_DATE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})"
    r"(?:T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?"
    r"(?:Z|[+-]\d{2}:\d{2})?$"
)
# Largest magnitude below which every integer has an exact double; an
# identifier above it may already have been rounded by the service.
_MAX_EXACT_DOUBLE_INTEGER = 2**53

DEFAULT_MAPPING_RESOURCE = "th_wsg_hqsg.toml"


# ==========================================================
# Mapping definition
# ==========================================================


@dataclass(frozen=True)
class ZoneClass:
    source_code: str
    category: str
    legal_description: str
    basis: str


@dataclass(frozen=True)
class SourceFlag:
    """An exact source value that limits how a zone can be used (kept, with a warning)."""

    property: str
    value: str
    warning: str


@dataclass(frozen=True)
class FieldMap:
    """Destination attribute -> source property (``None`` = not published)."""

    state_code: str | None
    protection_area_id: str | None
    protection_area_name: str | None
    zone_code: str | None
    zone_label: str | None
    restriction_category: str | None
    legal_status: str | None
    legal_basis: str | None
    legal_effective_date: str | None
    source_modified_date: str | None
    extraction_rate: str | None
    extraction_rate_unit: str | None


@dataclass(frozen=True)
class LayerMapping:
    version: str
    layer: str
    state_code: str
    schema_verified: bool
    verification_note: str
    zone_id_property: str
    geometry_property: str
    integer_identifiers: frozenset[str]
    fields: FieldMap
    zone_classes: dict[str, ZoneClass]
    source_flags: tuple[SourceFlag, ...]
    digest: str
    source: str

    @property
    def identifier(self) -> str:
        """Version plus content digest: any change to the mapping values changes it."""
        return f"{self.version}+{self.digest[:12]}"

    def required_properties(self) -> dict[str, str]:
        """Source property -> role, for schema verification."""
        required = {self.zone_id_property: "source.zone_id"}
        for item in dataclass_fields(FieldMap):
            prop = getattr(self.fields, item.name)
            if prop:
                required[prop] = f"fields.{item.name}"
        for flag in self.source_flags:
            required.setdefault(flag.property, f"source_flag.{flag.warning}")
        return required

    def numeric_properties(self) -> tuple[str, ...]:
        return tuple(p for p in (self.fields.extraction_rate, *sorted(self.integer_identifiers)) if p)


def load_mapping(path: Path | None = None) -> LayerMapping:
    if path is None:
        resource = resources.files("app.water_protection.mappings").joinpath(DEFAULT_MAPPING_RESOURCE)
        raw = resource.read_bytes()
        source = f"package:app/water_protection/mappings/{DEFAULT_MAPPING_RESOURCE}"
    else:
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise MappingError(f"cannot read mapping file {path}: {exc}") from exc
        source = str(path)
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise MappingError(f"mapping file {source} is not valid TOML: {exc}") from exc
    # Digest of the parsed document, not the file bytes: CRLF/LF checkouts and
    # comment edits must not change mapping_version (it feeds content_hash).
    canonical = json.dumps(document, sort_keys=True, ensure_ascii=False).encode()
    return _build_mapping(document, digest=hashlib.sha256(canonical).hexdigest(), source=source)


def _build_mapping(document: dict[str, Any], *, digest: str, source: str) -> LayerMapping:
    try:
        meta = document["mapping"]
        src = document["source"]
        fields_doc = document["fields"]
        classes_doc = document.get("zone_classification", [])
    except KeyError as exc:
        raise MappingError(f"mapping file {source} lacks section [{exc.args[0]}]") from exc

    def required_str(section: dict[str, Any], key: str, where: str) -> str:
        value = section.get(key)
        if not isinstance(value, str) or not value.strip():
            raise MappingError(f"{where}.{key} must be a non-empty string")
        return value.strip()

    def property_name(value: Any, where: str) -> str | None:
        if not isinstance(value, str):
            raise MappingError(f'{where} must be a string (use "" for not published)')
        value = value.strip()
        if not value:
            return None
        if not _PROPERTY_NAME.match(value):
            raise MappingError(f"{where} = {value!r} is not a valid property name")
        return value

    expected = {f.name for f in dataclass_fields(FieldMap)}
    unknown = set(fields_doc) - expected
    missing = expected - set(fields_doc)
    if unknown or missing:
        raise MappingError(
            f"[fields] must define exactly {sorted(expected)}; unknown={sorted(unknown)} missing={sorted(missing)}"
        )
    field_map = FieldMap(**{name: property_name(fields_doc[name], f"fields.{name}") for name in expected})

    state_code = required_str(meta, "state_code", "mapping").upper()
    if len(state_code) != 2 or not state_code.isalpha():
        raise MappingError("mapping.state_code must be a two-letter code")
    if not isinstance(meta.get("schema_verified"), bool):
        raise MappingError("mapping.schema_verified must be true or false")

    zone_classes: dict[str, ZoneClass] = {}
    for index, entry in enumerate(classes_doc):
        where = f"zone_classification[{index}]"
        code = required_str(entry, "source_code", where)
        category = required_str(entry, "category", where)
        if category not in ZONE_CATEGORIES or category == UNKNOWN_CATEGORY:
            raise MappingError(f"{where}.category {category!r} is not one of {ZONE_CATEGORIES[:-1]}")
        if code in zone_classes:
            raise MappingError(f"{where}.source_code {code!r} is defined twice")
        zone_classes[code] = ZoneClass(
            source_code=code,
            category=category,
            legal_description=required_str(entry, "legal_description", where),
            basis=required_str(entry, "basis", where),
        )
    if field_map.zone_code and not zone_classes:
        raise MappingError("fields.zone_code is mapped but no [[zone_classification]] rows exist")

    source_flags: list[SourceFlag] = []
    for index, entry in enumerate(document.get("source_flag", [])):
        where = f"source_flag[{index}]"
        prop = property_name(entry.get("property", ""), f"{where}.property")
        warning = required_str(entry, "warning", where)
        value = entry.get("value")
        if prop is None or not isinstance(value, str) or not value.strip():
            raise MappingError(f"{where} needs a property and a non-empty value")
        if not _WARNING_CODE.match(warning):
            raise MappingError(f"{where}.warning {warning!r} must be lower_snake_case")
        source_flags.append(SourceFlag(property=prop, value=value.strip(), warning=warning))

    zone_id = property_name(src.get("zone_id", ""), "source.zone_id")
    geometry = property_name(src.get("geometry", ""), "source.geometry")
    if zone_id is None or geometry is None:
        raise MappingError("source.zone_id and source.geometry are required")

    raw_integer_ids = src.get("integer_identifiers", [])
    if not isinstance(raw_integer_ids, list):
        raise MappingError("source.integer_identifiers must be a list of property names")
    integer_ids = {property_name(p, "source.integer_identifiers[]") for p in raw_integer_ids}
    identifier_properties = {zone_id, field_map.protection_area_id}
    if None in integer_ids or not integer_ids <= identifier_properties:
        raise MappingError(
            "source.integer_identifiers may only list the zone id and protection area id properties "
            f"({sorted(p for p in identifier_properties if p)})"
        )

    return LayerMapping(
        version=required_str(meta, "version", "mapping"),
        layer=required_str(meta, "layer", "mapping"),
        state_code=state_code,
        schema_verified=meta["schema_verified"],
        verification_note=str(meta.get("verification_note", "")),
        zone_id_property=zone_id,
        geometry_property=geometry,
        integer_identifiers=frozenset(p for p in integer_ids if p),
        fields=field_map,
        zone_classes=zone_classes,
        source_flags=tuple(source_flags),
        digest=digest,
        source=source,
    )


# ==========================================================
# Applying the mapping
# ==========================================================


@dataclass(frozen=True)
class MappedAttributes:
    source_zone_id: str
    state_code: str
    protection_area_id: str | None
    protection_area_name: str | None
    zone_code_source: str | None
    zone_category: str
    zone_legal_description: str | None
    zone_classification_basis: str | None
    zone_label_source: str | None
    restriction_category_source: str | None
    legal_status_source: str | None
    legal_basis_source: str | None
    legal_effective_date: date | None
    source_modified_date: date | None
    extraction_rate: Decimal | None
    extraction_rate_unit: str | None
    extraction_rate_status: str
    warnings: tuple[str, ...]

    def to_hashable_dict(self) -> dict[str, Any]:
        return {
            f.name: (
                str(getattr(self, f.name))
                if isinstance(getattr(self, f.name), (date, Decimal))
                else getattr(self, f.name)
            )
            for f in dataclass_fields(self)
        }


def map_attributes(feature: RawFeature, mapping: LayerMapping) -> MappedAttributes:
    warnings: list[str] = []
    fields_ = mapping.fields

    source_zone_id = _single(feature, mapping.zone_id_property)
    if source_zone_id is None:
        raise RecordRejected(
            "missing_source_zone_id",
            f"property {mapping.zone_id_property!r} is missing or empty",
        )
    if mapping.zone_id_property in mapping.integer_identifiers:
        normalized = integer_identifier(source_zone_id)
        if normalized is None:
            raise RecordRejected(
                "invalid_source_zone_id",
                f"{mapping.zone_id_property}={source_zone_id!r} is not a whole number",
            )
        source_zone_id = normalized

    protection_area_id = _single(feature, fields_.protection_area_id)
    if protection_area_id is not None and fields_.protection_area_id in mapping.integer_identifiers:
        normalized = integer_identifier(protection_area_id)
        if normalized is None:
            warnings.append("protection_area_id_not_integer")
        else:
            protection_area_id = normalized

    if fields_.state_code:
        published_state = _single(feature, fields_.state_code)
        if published_state is not None and published_state != mapping.state_code:
            raise RecordRejected(
                "state_code_mismatch",
                f"{fields_.state_code}={published_state!r}, expected {mapping.state_code!r}",
            )

    zone_code_raw = _raw(feature, fields_.zone_code)
    zone_code = zone_code_raw.strip() if zone_code_raw is not None else None
    zone_class = mapping.zone_classes.get(zone_code) if zone_code else None
    if fields_.zone_code and not zone_code:
        warnings.append("missing_zone_code")
    elif zone_code and zone_class is None:
        warnings.append("unknown_zone_code")

    rate, rate_status = _extraction_rate(feature, fields_.extraction_rate)
    if rate_status == "unparseable":
        warnings.append("extraction_rate_unparseable")
    unit = _single(feature, fields_.extraction_rate_unit) if fields_.extraction_rate_unit else None
    if rate is not None and unit is None:
        warnings.append("extraction_rate_unit_unknown")

    # Same matching rule as zone codes: exact after trimming outer whitespace.
    for flag in mapping.source_flags:
        if _single(feature, flag.property) == flag.value:
            warnings.append(flag.warning)

    return MappedAttributes(
        source_zone_id=source_zone_id,
        state_code=mapping.state_code,
        protection_area_id=protection_area_id,
        protection_area_name=_single(feature, fields_.protection_area_name),
        zone_code_source=zone_code_raw if zone_code else None,
        zone_category=zone_class.category if zone_class else UNKNOWN_CATEGORY,
        zone_legal_description=zone_class.legal_description if zone_class else None,
        zone_classification_basis=zone_class.basis if zone_class else None,
        zone_label_source=_single(feature, fields_.zone_label),
        restriction_category_source=_single(feature, fields_.restriction_category),
        legal_status_source=_single(feature, fields_.legal_status),
        legal_basis_source=_single(feature, fields_.legal_basis),
        legal_effective_date=_date(feature, fields_.legal_effective_date, "legal_effective_date", warnings),
        source_modified_date=_date(feature, fields_.source_modified_date, "source_modified_date", warnings),
        extraction_rate=rate,
        extraction_rate_unit=unit,
        extraction_rate_status=rate_status,
        warnings=tuple(warnings),
    )


def integer_identifier(value: str) -> str | None:
    """
    Canonical text of a whole-number identifier typed as xsd:double.

    "5.53210009E8" and "553210009.0" both become "553210009". Fractions,
    non-numeric text and values too large for an exact double give None.
    """
    value = value.strip()
    if not _DECIMAL.match(value):
        return None
    number = Decimal(value)
    if number != number.to_integral_value() or abs(number) > _MAX_EXACT_DOUBLE_INTEGER:
        return None
    return str(int(number))


def zone_id_for_key(feature: RawFeature, mapping: LayerMapping) -> str | None:
    """Best-effort source zone id for quarantine keys; never raises."""
    raw = feature.properties.get(mapping.zone_id_property)
    if not isinstance(raw, str) or not raw.strip():
        return None
    value = raw.strip()
    if mapping.zone_id_property in mapping.integer_identifiers:
        return integer_identifier(value) or value
    return value


def _raw(feature: RawFeature, prop: str | None) -> str | None:
    if prop is None:
        return None
    value = feature.properties.get(prop)
    if isinstance(value, list):
        raise RecordRejected("multivalued_property", f"property {prop!r} occurs {len(value)} times")
    return value


def _single(feature: RawFeature, prop: str | None) -> str | None:
    value = _raw(feature, prop)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _extraction_rate(feature: RawFeature, prop: str | None) -> tuple[Decimal | None, str]:
    if prop is None:
        return None, "not_published"
    value = _single(feature, prop)
    if value is None:
        return None, "missing"
    # xsd numeric lexical form only; a German "1.234,5" or "ca. 500" is
    # kept as raw text and flagged rather than reinterpreted.
    if not _DECIMAL.match(value):
        return None, "unparseable"
    try:
        number = Decimal(value)
    except InvalidOperation:
        return None, "unparseable"
    if not number.is_finite() or number < 0:
        return None, "unparseable"
    return number, "published"


def _date(feature: RawFeature, prop: str | None, name: str, warnings: list[str]) -> date | None:
    value = _single(feature, prop)
    if value is None:
        return None
    match = _XSD_DATE.match(value)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            pass
    warnings.append(f"unparseable_{name}")
    return None
