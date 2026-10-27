"""
GetFeature response parsing (GML 3.2, GML 3.1.1 and GML 2 geometries).

Document-level problems (exception reports, HTML, wrong root, wrong
feature type) raise and abort the retrieval. Problems inside a single
feature's geometry are captured on the ``RawGeometry`` so the feature
can be quarantined with a precise reason while the rest of the page is
processed normally.

Coordinates are returned exactly in the order they appear in the
document. Axis-order interpretation depends on the srsName and is
handled in ``app.water_protection.crs``.
"""

from __future__ import annotations

from dataclasses import dataclass

from lxml import etree
from shapely.geometry import LinearRing
from shapely.geometry import LineString
from shapely.geometry import MultiPolygon
from shapely.geometry import Point
from shapely.geometry import Polygon
from shapely.geometry.base import BaseGeometry

from app.water_protection.errors import GeometryParseError
from app.water_protection.errors import SchemaMismatchError
from app.water_protection.wfs.xml_utils import child_elements
from app.water_protection.wfs.xml_utils import children
from app.water_protection.wfs.xml_utils import first_child
from app.water_protection.wfs.xml_utils import is_gml
from app.water_protection.wfs.xml_utils import is_nil
from app.water_protection.wfs.xml_utils import local_name
from app.water_protection.wfs.xml_utils import parse_xml_response

PropertyValue = str | None | list[str | None]

MALFORMED = "malformed_geometry"
UNSUPPORTED = "unsupported_geometry_type"

_GML_ID_ATTRIBUTES = (
    "{http://www.opengis.net/gml/3.2}id",
    "{http://www.opengis.net/gml}id",
    "fid",
)


@dataclass(frozen=True)
class RawGeometry:
    shape: BaseGeometry | None
    srs_name: str | None
    gml_type: str | None
    error_code: str | None = None
    error_detail: str | None = None
    dropped_z: bool = False


@dataclass(frozen=True)
class RawFeature:
    gml_id: str | None
    properties: dict[str, PropertyValue]
    geometry: RawGeometry


@dataclass(frozen=True)
class FeaturePage:
    features: tuple[RawFeature, ...]
    number_matched: int | None
    number_returned: int | None
    time_stamp: str | None
    collection_srs: str | None = None


# ==========================================================
# FeatureCollection
# ==========================================================


def parse_feature_collection(
    content: bytes,
    *,
    layer_local_name: str,
    geometry_property: str,
    operation: str = "GetFeature",
    url: str | None = None,
) -> FeaturePage:
    root = parse_xml_response(
        content,
        operation=operation,
        expected_roots=frozenset({"FeatureCollection"}),
        url=url,
    )

    feature_elements: list[etree._Element] = []
    for member in child_elements(root):
        name = local_name(member)
        if name in ("member", "featureMember", "featureMembers"):
            feature_elements.extend(child_elements(member))
        elif name == "truncatedResponse":
            raise SchemaMismatchError("server reported a truncated response", operation=operation, url=url)

    features: list[RawFeature] = []
    for element in feature_elements:
        if local_name(element) == "FeatureCollection":
            raise SchemaMismatchError(
                "nested FeatureCollection members are not supported", operation=operation, url=url
            )
        if local_name(element) != layer_local_name:
            raise SchemaMismatchError(
                f"unexpected feature type <{local_name(element)}>; requested {layer_local_name!r}",
                operation=operation,
                url=url,
            )
        features.append(_feature(element, geometry_property))

    return FeaturePage(
        features=tuple(features),
        number_matched=_count(root.get("numberMatched")),
        number_returned=_count(root.get("numberReturned") or root.get("numberOfFeatures")),
        time_stamp=root.get("timeStamp"),
        collection_srs=_collection_srs(root),
    )


def _count(value: str | None) -> int | None:
    # numberMatched may legitimately be "unknown" in WFS 2.0.
    if value is None or not value.strip().isdigit():
        return None
    return int(value)


def _collection_srs(root: etree._Element) -> str | None:
    bounded = first_child(root, "boundedBy")
    if bounded is None:
        return None
    for node in bounded.iter():
        if isinstance(node.tag, str) and node.get("srsName"):
            return node.get("srsName")
    return None


def _feature(element: etree._Element, geometry_property: str) -> RawFeature:
    gml_id = next((element.get(a) for a in _GML_ID_ATTRIBUTES if element.get(a)), None)
    properties: dict[str, PropertyValue] = {}
    geometry = RawGeometry(shape=None, srs_name=None, gml_type=None)
    geometry_seen = False

    for prop in child_elements(element):
        name = local_name(prop)
        if is_gml(prop):
            if name == "boundedBy":
                continue
            name = f"gml:{name}"
        if name == geometry_property:
            if geometry_seen:
                geometry = RawGeometry(
                    shape=None,
                    srs_name=None,
                    gml_type=None,
                    error_code=MALFORMED,
                    error_detail=f"geometry property {geometry_property!r} occurs more than once",
                )
                continue
            geometry_seen = True
            geometry = _geometry_property(prop)
            continue

        value = _property_value(prop)
        if name in properties:
            existing = properties[name]
            properties[name] = (existing if isinstance(existing, list) else [existing]) + [value]
        else:
            properties[name] = value

    return RawFeature(gml_id=gml_id, properties=properties, geometry=geometry)


def _property_value(prop: etree._Element) -> str | None:
    if is_nil(prop):
        return None
    if any(True for _ in child_elements(prop)):
        # Complex property: keep a faithful serialisation in the raw attributes.
        return etree.tostring(prop, encoding="unicode", with_tail=False)
    return prop.text if prop.text is not None else ""


def _geometry_property(prop: etree._Element) -> RawGeometry:
    if is_nil(prop):
        return RawGeometry(shape=None, srs_name=None, gml_type=None)
    elements = list(child_elements(prop))
    if not elements:
        return RawGeometry(shape=None, srs_name=None, gml_type=None)
    geom_el = elements[0]
    gml_type = local_name(geom_el)
    try:
        srs_name = _resolve_srs(geom_el)
        dimension = _srs_dimension(geom_el)
        shape = parse_geometry(geom_el, dimension)
    except GeometryParseError as exc:
        return RawGeometry(
            shape=None,
            srs_name=geom_el.get("srsName"),
            gml_type=gml_type,
            error_code=exc.reason_code,
            error_detail=exc.detail,
        )
    return RawGeometry(
        shape=shape,
        srs_name=srs_name,
        gml_type=gml_type,
        dropped_z=dimension == 3,
    )


def _resolve_srs(geom_el: etree._Element) -> str | None:
    names = {node.get("srsName") for node in geom_el.iter() if isinstance(node.tag, str) and node.get("srsName")}
    if len(names) > 1:
        raise GeometryParseError(MALFORMED, f"mixed srsName values within one geometry: {sorted(names)}")  # type: ignore[type-var]
    return names.pop() if names else None


def _srs_dimension(geom_el: etree._Element) -> int:
    dims = {
        node.get("srsDimension") for node in geom_el.iter() if isinstance(node.tag, str) and node.get("srsDimension")
    }
    if not dims:
        return 2
    if len(dims) > 1 or not dims <= {"2", "3"}:
        raise GeometryParseError(MALFORMED, f"unsupported srsDimension {sorted(dims)}")  # type: ignore[type-var]
    return int(dims.pop())  # type: ignore[arg-type]


# ==========================================================
# Geometry
# ==========================================================


def parse_geometry(element: etree._Element, dimension: int = 2) -> BaseGeometry:
    name = local_name(element)
    if name == "Polygon":
        return _polygon(element, dimension)
    if name in ("MultiSurface", "MultiPolygon"):
        return _multi_surface(element, dimension)
    if name == "Surface":
        polygons = _surface_patches(element, dimension)
        return polygons[0] if len(polygons) == 1 else MultiPolygon(polygons)
    if name == "Point":
        coords = _coordinates_of(element, dimension, minimum=1)
        return Point(coords[0])
    if name == "LineString":
        return LineString(_coordinates_of(element, dimension, minimum=2))
    raise GeometryParseError(UNSUPPORTED, f"GML geometry <{name}> is not supported")


def _multi_surface(element: etree._Element, dimension: int) -> MultiPolygon:
    polygons: list[Polygon] = []
    for member in child_elements(element):
        if local_name(member) not in ("surfaceMember", "surfaceMembers", "polygonMember"):
            continue
        for part in child_elements(member):
            part_geom = parse_geometry(part, dimension)
            if isinstance(part_geom, Polygon):
                polygons.append(part_geom)
            elif isinstance(part_geom, MultiPolygon):
                polygons.extend(part_geom.geoms)
            else:
                raise GeometryParseError(UNSUPPORTED, f"MultiSurface member <{local_name(part)}> is not a surface")
    if not polygons:
        raise GeometryParseError(MALFORMED, "MultiSurface without members")
    return MultiPolygon(polygons)


def _surface_patches(element: etree._Element, dimension: int) -> list[Polygon]:
    patches = first_child(element, "patches")
    if patches is None:
        raise GeometryParseError(MALFORMED, "Surface without patches")
    polygons: list[Polygon] = []
    for patch in child_elements(patches):
        if local_name(patch) != "PolygonPatch":
            raise GeometryParseError(UNSUPPORTED, f"surface patch <{local_name(patch)}> is not supported")
        polygons.append(_polygon(patch, dimension))
    if not polygons:
        raise GeometryParseError(MALFORMED, "Surface without polygon patches")
    return polygons


def _polygon(element: etree._Element, dimension: int) -> Polygon:
    # Explicit None checks: lxml element truthiness depends on child count.
    exterior_holder = first_child(element, "exterior")
    if exterior_holder is None:
        exterior_holder = first_child(element, "outerBoundaryIs")
    if exterior_holder is None:
        raise GeometryParseError(MALFORMED, "Polygon without exterior ring")
    interiors = children(element, "interior") + children(element, "innerBoundaryIs")
    shell = _ring(exterior_holder, dimension)
    holes = [_ring(holder, dimension) for holder in interiors]
    return Polygon(shell, holes)


def _ring(holder: etree._Element, dimension: int) -> list[tuple[float, float]]:
    rings = list(child_elements(holder))
    if len(rings) != 1:
        raise GeometryParseError(MALFORMED, "ring holder must contain exactly one ring")
    ring = rings[0]
    if local_name(ring) != "LinearRing":
        # gml:Ring built from curve segments (possibly arcs) is not
        # linearised silently; such features are quarantined.
        raise GeometryParseError(UNSUPPORTED, f"ring type <{local_name(ring)}> is not supported")
    coords = _coordinates_of(ring, dimension, minimum=4)
    if coords[0] != coords[-1]:
        raise GeometryParseError(MALFORMED, "LinearRing is not closed")
    try:
        LinearRing(coords)
    except ValueError as exc:  # pragma: no cover - shapely guards counts above
        raise GeometryParseError(MALFORMED, f"invalid LinearRing: {exc}") from exc
    return coords


def _coordinates_of(element: etree._Element, dimension: int, *, minimum: int) -> list[tuple[float, float]]:
    pos_list = first_child(element, "posList")
    if pos_list is not None:
        dim = int(pos_list.get("srsDimension") or dimension)
        values = _floats((pos_list.text or "").split())
        if len(values) % dim:
            raise GeometryParseError(MALFORMED, f"posList has {len(values)} values, not a multiple of dimension {dim}")
        coords = [(values[i], values[i + 1]) for i in range(0, len(values), dim)]
    else:
        positions = children(element, "pos")
        coordinates = first_child(element, "coordinates")
        if positions:
            coords = []
            for pos in positions:
                values = _floats((pos.text or "").split())
                if len(values) not in (2, 3):
                    raise GeometryParseError(MALFORMED, f"pos has {len(values)} values")
                coords.append((values[0], values[1]))
        elif coordinates is not None:
            coords = _gml2_coordinates(coordinates)
        else:
            raise GeometryParseError(MALFORMED, f"<{local_name(element)}> has no coordinates")
    if len(coords) < minimum:
        raise GeometryParseError(
            MALFORMED, f"<{local_name(element)}> has {len(coords)} positions, at least {minimum} required"
        )
    return coords


def _gml2_coordinates(element: etree._Element) -> list[tuple[float, float]]:
    decimal = element.get("decimal", ".")
    cs = element.get("cs", ",")
    ts = element.get("ts", " ")
    coords: list[tuple[float, float]] = []
    tuples = (element.text or "").split() if ts.isspace() else (element.text or "").split(ts)
    for item in tuples:
        if not item.strip():
            continue
        parts = item.strip().split(cs)
        if decimal != ".":
            parts = [p.replace(decimal, ".") for p in parts]
        values = _floats(parts)
        if len(values) not in (2, 3):
            raise GeometryParseError(MALFORMED, f"coordinate tuple {item!r} has {len(values)} values")
        coords.append((values[0], values[1]))
    return coords


def _floats(tokens: list[str]) -> list[float]:
    try:
        return [float(token) for token in tokens]
    except ValueError as exc:
        raise GeometryParseError(MALFORMED, f"non-numeric coordinate value: {exc}") from exc
