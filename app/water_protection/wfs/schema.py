"""
DescribeFeatureType (XML Schema) parsing and schema-drift detection.

The live layer schema is compared with the mapping before any feature
is fetched: if a mapped source property is missing the run stops with
``SchemaMismatchError`` instead of silently loading NULLs.
"""

from __future__ import annotations

from dataclasses import dataclass

from lxml import etree

from app.water_protection.errors import SchemaMismatchError
from app.water_protection.wfs.xml_utils import GML_NAMESPACES
from app.water_protection.wfs.xml_utils import XSD_NAMESPACE
from app.water_protection.wfs.xml_utils import child_elements
from app.water_protection.wfs.xml_utils import local_name
from app.water_protection.wfs.xml_utils import parse_xml_response

GEOMETRY_PROPERTY_TYPES = frozenset(
    {
        "GeometryPropertyType",
        "SurfacePropertyType",
        "MultiSurfacePropertyType",
        "PolygonPropertyType",
        "MultiPolygonPropertyType",
        "MultiGeometryPropertyType",
        "GeometricPrimitivePropertyType",
        "CurvePropertyType",
        "MultiCurvePropertyType",
        "LineStringPropertyType",
        "MultiLineStringPropertyType",
        "PointPropertyType",
        "MultiPointPropertyType",
    }
)
NUMERIC_XSD_TYPES = frozenset(
    {"decimal", "double", "float", "int", "integer", "long", "short", "nonNegativeInteger"}
)


@dataclass(frozen=True)
class PropertyInfo:
    name: str
    type_namespace: str | None
    type_local: str | None
    min_occurs: int
    max_occurs: str
    nillable: bool

    @property
    def is_geometry(self) -> bool:
        return self.type_namespace in GML_NAMESPACES and self.type_local in GEOMETRY_PROPERTY_TYPES

    @property
    def is_numeric(self) -> bool:
        return self.type_namespace == XSD_NAMESPACE and self.type_local in NUMERIC_XSD_TYPES

    @property
    def type_label(self) -> str:
        if self.type_local is None:
            return "(unknown)"
        if self.type_namespace in GML_NAMESPACES:
            return f"gml:{self.type_local}"
        if self.type_namespace == XSD_NAMESPACE:
            return f"xsd:{self.type_local}"
        return self.type_local


@dataclass(frozen=True)
class FeatureTypeSchema:
    element_name: str
    target_namespace: str | None
    properties: tuple[PropertyInfo, ...]

    def get(self, name: str) -> PropertyInfo | None:
        for prop in self.properties:
            if prop.name == name:
                return prop
        return None

    @property
    def geometry_properties(self) -> tuple[PropertyInfo, ...]:
        return tuple(p for p in self.properties if p.is_geometry)

    def to_dict(self) -> dict[str, object]:
        return {
            "element_name": self.element_name,
            "target_namespace": self.target_namespace,
            "properties": [
                {
                    "name": p.name,
                    "type": p.type_label,
                    "min_occurs": p.min_occurs,
                    "max_occurs": p.max_occurs,
                    "nillable": p.nillable,
                    "geometry": p.is_geometry,
                }
                for p in self.properties
            ],
        }


@dataclass(frozen=True)
class SchemaCheck:
    """Outcome of comparing the live schema with the mapping."""

    unmapped_properties: tuple[str, ...]
    warnings: tuple[str, ...]


def parse_feature_type_schema(
    content: bytes,
    layer_local_name: str,
    *,
    url: str | None = None,
) -> FeatureTypeSchema:
    root = parse_xml_response(
        content,
        operation="DescribeFeatureType",
        expected_roots=frozenset({"schema"}),
        url=url,
    )
    target_namespace = root.get("targetNamespace")

    element = _top_level(root, "element", layer_local_name)
    if element is None:
        raise SchemaMismatchError(
            f"schema declares no top-level element named {layer_local_name!r}",
            operation="DescribeFeatureType",
            url=url,
        )

    complex_type = _inline_child(element, "complexType")
    if complex_type is None:
        type_ns, type_local = _resolve_qname(element, element.get("type"))
        if type_local is None:
            raise SchemaMismatchError(
                f"element {layer_local_name!r} has no type",
                operation="DescribeFeatureType",
                url=url,
            )
        complex_type = _top_level(root, "complexType", type_local)
        if complex_type is None:
            raise SchemaMismatchError(
                f"complexType {type_local!r} for {layer_local_name!r} is not in the response",
                operation="DescribeFeatureType",
                url=url,
            )

    properties = tuple(_property(node) for node in _property_elements(complex_type))
    return FeatureTypeSchema(
        element_name=layer_local_name,
        target_namespace=target_namespace,
        properties=properties,
    )


def check_schema_against_mapping(
    schema: FeatureTypeSchema,
    *,
    required: dict[str, str],
    geometry_property: str,
    numeric: tuple[str, ...] = (),
) -> SchemaCheck:
    """
    Verify that every mapped source property exists.

    ``required`` maps source property name -> mapping role (used in the
    error message). Raises ``SchemaMismatchError`` listing every
    missing property at once, so the mapping can be fixed in one pass.
    """

    problems: list[str] = []
    for prop_name, role in sorted(required.items()):
        if schema.get(prop_name) is None:
            problems.append(f"{role} -> {prop_name!r} is not in the layer schema")

    geometry = schema.get(geometry_property)
    if geometry is None:
        problems.append(f"geometry property {geometry_property!r} is not in the layer schema")
    elif not geometry.is_geometry:
        problems.append(
            f"geometry property {geometry_property!r} has non-geometry type {geometry.type_label}"
        )

    if problems:
        available = ", ".join(f"{p.name}:{p.type_label}" for p in schema.properties)
        raise SchemaMismatchError(
            "mapping does not match the live schema: "
            + "; ".join(problems)
            + f". Available properties: {available}",
            operation="DescribeFeatureType",
        )

    warnings = [
        f"mapped numeric property {name!r} is declared as {schema.get(name).type_label}"  # type: ignore[union-attr]
        for name in numeric
        if schema.get(name) is not None and not schema.get(name).is_numeric  # type: ignore[union-attr]
    ]
    mapped = set(required) | {geometry_property}
    unmapped = tuple(sorted(p.name for p in schema.properties if p.name not in mapped))
    return SchemaCheck(unmapped_properties=unmapped, warnings=tuple(warnings))


# ==========================================================
# XSD helpers
# ==========================================================


def _top_level(root: etree._Element, kind: str, name: str) -> etree._Element | None:
    for node in child_elements(root):
        if local_name(node) == kind and node.get("name") == name:
            return node
    return None


def _inline_child(node: etree._Element, kind: str) -> etree._Element | None:
    for child in child_elements(node):
        if local_name(child) == kind:
            return child
    return None


def _resolve_qname(node: etree._Element, qname: str | None) -> tuple[str | None, str | None]:
    if not qname:
        return None, None
    if ":" in qname:
        prefix, local = qname.split(":", 1)
        return node.nsmap.get(prefix), local
    return node.nsmap.get(None), qname


def _property_elements(complex_type: etree._Element) -> list[etree._Element]:
    # Properties live in (complexContent/extension/)sequence; nested
    # anonymous types of individual properties are not descended into.
    found: list[etree._Element] = []

    def walk(node: etree._Element) -> None:
        for child in child_elements(node):
            name = local_name(child)
            if name == "element" and child.get("name"):
                found.append(child)
            elif name in ("complexContent", "extension", "sequence", "all", "choice"):
                walk(child)

    walk(complex_type)
    return found


def _property(node: etree._Element) -> PropertyInfo:
    type_ns, type_local = _resolve_qname(node, node.get("type"))
    if type_local is None:
        # Inline simpleType with a restriction, e.g. a string with maxLength.
        for restriction in node.iter():
            if isinstance(restriction.tag, str) and local_name(restriction) == "restriction":
                type_ns, type_local = _resolve_qname(restriction, restriction.get("base"))
                break
    min_occurs = node.get("minOccurs", "1")
    return PropertyInfo(
        name=node.get("name", ""),
        type_namespace=type_ns,
        type_local=type_local,
        min_occurs=int(min_occurs) if min_occurs.isdigit() else 1,
        max_occurs=node.get("maxOccurs", "1"),
        nillable=node.get("nillable", "false").lower() == "true",
    )
