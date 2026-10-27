"""Small helpers for writing synthetic WFS 2.0 / GML 3.2 responses in tests."""

from __future__ import annotations

from collections.abc import Iterable
from collections.abc import Sequence
from xml.sax.saxutils import escape

SRS_25832 = "urn:ogc:def:crs:EPSG::25832"
SRS_4326 = "urn:ogc:def:crs:EPSG::4326"

Coords = Sequence[tuple[float, float]]

NAMESPACES = (
    'xmlns:wfs="http://www.opengis.net/wfs/2.0" '
    'xmlns:gml="http://www.opengis.net/gml/3.2" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
    'xmlns:wsg="https://example.invalid/wsg"'
)


def square(x: float, y: float, size: float) -> list[tuple[float, float]]:
    return [(x, y), (x + size, y), (x + size, y + size), (x, y + size), (x, y)]


def pos_list(coords: Coords) -> str:
    return " ".join(f"{a:g} {b:g}" for a, b in coords)


def polygon(exterior: Coords, holes: Iterable[Coords] = (), srs: str | None = None) -> str:
    srs_attr = f' srsName="{srs}"' if srs else ""
    interiors = "".join(
        f"<gml:interior><gml:LinearRing><gml:posList>{pos_list(h)}</gml:posList></gml:LinearRing></gml:interior>"
        for h in holes
    )
    return (
        f"<gml:Polygon{srs_attr}><gml:exterior><gml:LinearRing><gml:posList>{pos_list(exterior)}"
        f"</gml:posList></gml:LinearRing></gml:exterior>{interiors}</gml:Polygon>"
    )


def multisurface(*polygons: str, srs: str = SRS_25832) -> str:
    members = "".join(f"<gml:surfaceMember>{p}</gml:surfaceMember>" for p in polygons)
    return f'<gml:MultiSurface srsName="{srs}" srsDimension="2">{members}</gml:MultiSurface>'


def feature(
    gml_id: str,
    properties: dict[str, str | None],
    geometry: str | None,
    *,
    nil_geometry: bool = False,
) -> str:
    parts = []
    for name, value in properties.items():
        if value is None:
            parts.append(f'<wsg:{name} xsi:nil="true"/>')
        else:
            parts.append(f"<wsg:{name}>{escape(value)}</wsg:{name}>")
    if nil_geometry:
        parts.append('<wsg:geom xsi:nil="true"/>')
    elif geometry is not None:
        parts.append(f"<wsg:geom>{geometry}</wsg:geom>")
    return f'<wsg:th_wsg_zonen gml:id="{gml_id}">{"".join(parts)}</wsg:th_wsg_zonen>'


def collection(
    features: Sequence[str],
    *,
    number_matched: int | str | None = None,
    number_returned: int | None = None,
    time_stamp: str = "2026-10-27T12:00:00Z",
) -> bytes:
    matched = len(features) if number_matched is None else number_matched
    returned = len(features) if number_returned is None else number_returned
    members = "\n".join(f"  <wfs:member>{f}</wfs:member>" for f in features)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<wfs:FeatureCollection {NAMESPACES} numberMatched="{matched}" '
        f'numberReturned="{returned}" timeStamp="{time_stamp}">\n{members}\n</wfs:FeatureCollection>\n'
    ).encode()


def zone_properties(
    zone_id: str | None,
    zone: str | None,
    *,
    wsg_nr: str = "TEST-WSG-01",
    name: str = "Synthetisches Testgebiet Nord",
    **extra: str | None,
) -> dict[str, str | None]:
    props: dict[str, str | None] = {}
    if zone_id is not None:
        props["zone_id"] = zone_id
    props.update(
        {
            "wsg_nr": wsg_nr,
            "wsg_name": name,
            "zone": zone,
            "zone_bez": extra.pop("zone_bez", "Synthetische Zone"),
            "status": extra.pop("status", "festgesetzt"),
            "rechtsgrundlage": extra.pop("rechtsgrundlage", "Testverordnung Nr. 1 (synthetisch)"),
            "datum_festsetzung": extra.pop("datum_festsetzung", "2001-05-14"),
            "aenderungsdatum": extra.pop("aenderungsdatum", "2019-03-01"),
            "bemerkung": extra.pop("bemerkung", None),
        }
    )
    props.update(extra)
    return props
