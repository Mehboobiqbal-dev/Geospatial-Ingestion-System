"""Small helpers for writing synthetic WFS 2.0 / GML 3.2 responses in tests."""

from __future__ import annotations

from collections.abc import Iterable
from collections.abc import Sequence
from xml.sax.saxutils import escape

SRS_25832 = "urn:ogc:def:crs:EPSG::25832"
SRS_4326 = "urn:ogc:def:crs:EPSG::4326"

Coords = Sequence[tuple[float, float]]

LAYER = "WASSER_UND_HEILQUELLENSCHUTZGEBIETE"

NAMESPACES = (
    'xmlns:wfs="http://www.opengis.net/wfs/2.0" '
    'xmlns:gml="http://www.opengis.net/gml/3.2" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
    'xmlns:tmlnu="http://www.geoproxy.geoportal-th.de/tmlnu"'
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
            parts.append(f'<tmlnu:{name} xsi:nil="true"/>')
        else:
            parts.append(f"<tmlnu:{name}>{escape(value)}</tmlnu:{name}>")
    if nil_geometry:
        parts.append('<tmlnu:geom xsi:nil="true"/>')
    elif geometry is not None:
        parts.append(f"<tmlnu:geom>{geometry}</tmlnu:geom>")
    return f'<tmlnu:{LAYER} gml:id="{gml_id}">{"".join(parts)}</tmlnu:{LAYER}>'


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
    sz_num: str | None,
    zone: str | None,
    *,
    sg_id: str = "99001.0",
    name: str = "Synthetisches Testgebiet Nord",
    **extra: str | None,
) -> dict[str, str | None]:
    """Properties in the order of the live TLUBN schema; synthetic values."""
    props: dict[str, str | None] = {
        "Gebiet": extra.pop("Gebiet", "WSG"),
        "Sg_Id": sg_id,
        "Name": name,
        "Gebietsart": extra.pop("Gebietsart", "Wasserschutzzone"),
        "Zone": zone,
        "Status": extra.pop("Status", "festgesetzt"),
    }
    if sz_num is not None:
        props["Sz_Num"] = sz_num
    props["Bemerkung"] = extra.pop("Bemerkung", None)
    props["Stand"] = extra.pop("Stand", "2019-03-01")
    props.update(extra)
    return props
