"""
GetCapabilities parsing for WFS 2.0.0 and 1.1.0.

Only facts advertised by the service are recorded: version, feature
types with their declared CRS, GetFeature output formats and the
paging/sorting constraints. Nothing is assumed when a capability is
not advertised.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field

from lxml import etree

from app.water_protection.errors import CapabilitiesError
from app.water_protection.wfs.xml_utils import child_elements
from app.water_protection.wfs.xml_utils import children
from app.water_protection.wfs.xml_utils import descendants
from app.water_protection.wfs.xml_utils import first_child
from app.water_protection.wfs.xml_utils import local_name
from app.water_protection.wfs.xml_utils import parse_xml_response
from app.water_protection.wfs.xml_utils import text_of

# Preferred GetFeature formats, most preferred first. Matching is on the
# exact advertised string after removing whitespace and lower-casing.
GML_OUTPUT_FORMATS: tuple[str, ...] = (
    "application/gml+xml;version=3.2",
    "text/xml;subtype=gml/3.2.1",
    "text/xml;subtype=gml/3.2",
    "gml32",
    "text/xml;subtype=gml/3.1.1",
    "gml3",
    "text/xml;subtype=gml/2.1.2",
    "gml2",
)


def _normalise_format(value: str) -> str:
    return "".join(value.split()).lower()


@dataclass(frozen=True)
class FeatureTypeInfo:
    name: str  # as advertised, possibly prefixed (e.g. "wsg:th_wsg_zonen")
    local_name: str
    prefix: str | None
    namespace_uri: str | None
    title: str | None
    default_crs: str | None
    other_crs: tuple[str, ...]
    wgs84_bbox: tuple[float, float, float, float] | None
    output_formats: tuple[str, ...]


@dataclass(frozen=True)
class WfsCapabilities:
    version: str
    title: str | None
    provider: str | None
    feature_types: tuple[FeatureTypeInfo, ...]
    operations: tuple[str, ...]
    getfeature_output_formats: tuple[str, ...]
    result_types: tuple[str, ...]
    implements_result_paging: bool
    implements_sorting: bool
    count_default: int | None
    constraints: dict[str, str] = field(default_factory=dict)

    @property
    def is_wfs2(self) -> bool:
        return self.version.startswith("2.")

    def find_feature_type(self, layer: str, *, operation: str = "GetCapabilities") -> FeatureTypeInfo:
        exact = [ft for ft in self.feature_types if ft.name == layer]
        if exact:
            return exact[0]
        by_local = [ft for ft in self.feature_types if ft.local_name == layer]
        if len(by_local) == 1:
            return by_local[0]
        available = ", ".join(sorted(ft.name for ft in self.feature_types)) or "(none)"
        if len(by_local) > 1:
            raise CapabilitiesError(
                f"layer name {layer!r} is ambiguous; advertised matches: "
                f"{[ft.name for ft in by_local]}",
                operation=operation,
            )
        raise CapabilitiesError(
            f"layer {layer!r} is not advertised by the service; available: {available}",
            operation=operation,
        )

    def select_output_format(self, feature_type: FeatureTypeInfo) -> str | None:
        """
        Pick a GML output format the service advertises.

        Returns ``None`` when the service advertises no output formats
        at all (the version's default GML is then used). Raises when
        formats are advertised but none of them is GML we can parse.
        """

        advertised = feature_type.output_formats or self.getfeature_output_formats
        if not advertised:
            return None
        by_normalised = {_normalise_format(f): f for f in advertised}
        for candidate in GML_OUTPUT_FORMATS:
            if candidate in by_normalised:
                return by_normalised[candidate]
        raise CapabilitiesError(
            f"no supported GML output format advertised; advertised: {list(advertised)}",
            operation="GetCapabilities",
        )


# ==========================================================
# Parsing
# ==========================================================


def parse_capabilities(content: bytes, *, url: str | None = None) -> WfsCapabilities:
    root = parse_xml_response(
        content,
        operation="GetCapabilities",
        expected_roots=frozenset({"WFS_Capabilities"}),
        url=url,
    )
    version = root.get("version") or ""
    if not (version.startswith("2.0") or version == "1.1.0"):
        raise CapabilitiesError(
            f"unsupported WFS version {version!r} (supported: 2.0.x, 1.1.0)",
            operation="GetCapabilities",
            url=url,
        )

    identification = first_child(root, "ServiceIdentification")
    provider = first_child(root, "ServiceProvider")

    operations: list[str] = []
    getfeature_formats: tuple[str, ...] = ()
    result_types: tuple[str, ...] = ()
    constraints: dict[str, str] = {}
    global_formats: tuple[str, ...] = ()

    metadata = first_child(root, "OperationsMetadata")
    if metadata is not None:
        for parameter in children(metadata, "Parameter"):
            if parameter.get("name", "").lower() == "outputformat":
                global_formats = _allowed_values(parameter)
        for operation in children(metadata, "Operation"):
            name = operation.get("name", "")
            operations.append(name)
            if name == "GetFeature":
                for parameter in children(operation, "Parameter"):
                    pname = parameter.get("name", "").lower()
                    if pname == "outputformat":
                        getfeature_formats = _allowed_values(parameter)
                    elif pname == "resulttype":
                        result_types = _allowed_values(parameter)
                for constraint in children(operation, "Constraint"):
                    constraints[constraint.get("name", "")] = _constraint_value(constraint)
        for constraint in children(metadata, "Constraint"):
            constraints.setdefault(constraint.get("name", ""), _constraint_value(constraint))

    # Sorting is declared in the Filter Encoding conformance section.
    for node in descendants(root, "Constraint"):
        if node.get("name") == "ImplementsSorting":
            constraints.setdefault("ImplementsSorting", _constraint_value(node))

    count_default: int | None = None
    raw_count = constraints.get("CountDefault")
    if raw_count and raw_count.strip().isdigit():
        count_default = int(raw_count.strip())

    is_wfs2 = version.startswith("2.")
    feature_type_list = first_child(root, "FeatureTypeList")
    feature_types = tuple(
        _feature_type(node)
        for node in (children(feature_type_list, "FeatureType") if feature_type_list is not None else [])
    )

    return WfsCapabilities(
        version=version,
        title=text_of(first_child(identification, "Title")) if identification is not None else None,
        provider=text_of(first_child(provider, "ProviderName")) if provider is not None else None,
        feature_types=feature_types,
        operations=tuple(operations),
        getfeature_output_formats=getfeature_formats or global_formats,
        result_types=result_types,
        # Result paging is a WFS 2.0 feature; WFS 1.1.0 has no standard paging.
        implements_result_paging=is_wfs2 and _is_true(constraints.get("ImplementsResultPaging")),
        implements_sorting=_is_true(constraints.get("ImplementsSorting")),
        count_default=count_default,
        constraints=constraints,
    )


def _feature_type(node: etree._Element) -> FeatureTypeInfo:
    name_el = first_child(node, "Name")
    name = text_of(name_el) or ""
    prefix: str | None = None
    local = name
    namespace_uri: str | None = None
    if ":" in name:
        prefix, local = name.split(":", 1)
        if name_el is not None:
            namespace_uri = name_el.nsmap.get(prefix)

    default_crs = text_of(first_child(node, "DefaultCRS")) or text_of(first_child(node, "DefaultSRS"))
    other_crs = tuple(
        value
        for value in (text_of(el) for el in children(node, "OtherCRS") + children(node, "OtherSRS"))
        if value
    )

    bbox: tuple[float, float, float, float] | None = None
    bbox_el = first_child(node, "WGS84BoundingBox")
    if bbox_el is not None:
        lower = (text_of(first_child(bbox_el, "LowerCorner")) or "").split()
        upper = (text_of(first_child(bbox_el, "UpperCorner")) or "").split()
        try:
            bbox = (float(lower[0]), float(lower[1]), float(upper[0]), float(upper[1]))
        except (IndexError, ValueError):
            bbox = None

    formats_el = first_child(node, "OutputFormats")
    formats = tuple(
        value for value in (text_of(f) for f in children(formats_el, "Format")) if value
    ) if formats_el is not None else ()

    return FeatureTypeInfo(
        name=name,
        local_name=local,
        prefix=prefix,
        namespace_uri=namespace_uri,
        title=text_of(first_child(node, "Title")),
        default_crs=default_crs,
        other_crs=other_crs,
        wgs84_bbox=bbox,
        output_formats=formats,
    )


def _allowed_values(parameter: etree._Element) -> tuple[str, ...]:
    values: list[str] = []
    for node in parameter.iter():
        if isinstance(node.tag, str) and local_name(node) == "Value":
            value = text_of(node)
            if value:
                values.append(value)
    return tuple(values)


def _constraint_value(constraint: etree._Element) -> str:
    default = first_child(constraint, "DefaultValue")
    if default is not None:
        return text_of(default) or ""
    for node in child_elements(constraint):
        if local_name(node) in ("Value", "AllowedValues"):
            values = _allowed_values(constraint)
            return values[0] if values else ""
    return ""


def _is_true(value: str | None) -> bool:
    return (value or "").strip().upper() == "TRUE"
