"""
Coordinate reference system handling.

Axis order is the classic WFS pitfall. GML coordinates follow the
axis order of the CRS *as identified by the srsName form*:

* ``urn:ogc:def:crs:EPSG::<code>`` and
  ``http://www.opengis.net/def/crs/EPSG/0/<code>`` use the EPSG
  authority order, e.g. latitude/longitude for EPSG:4326 and
  northing/easting for EPSG:31468 (Gauss-Krueger zone 4);
* ``EPSG:<code>`` and ``http://www.opengis.net/gml/srs/epsg.xml#<code>``
  use the traditional x/y (easting/longitude first) order;
* ``CRS84`` is always longitude/latitude.

Geometries are brought into x/y order first and then transformed with
an explicit pyproj ``Transformer`` (``always_xy=True``). Coordinates
are never relabelled with a different SRID.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from pyproj import CRS
from pyproj import Transformer
from pyproj.exceptions import CRSError

TARGET_EPSG = 25832
TARGET_CRS_LABEL = f"EPSG:{TARGET_EPSG}"

_AUTHORITY_ORDER_PATTERNS = (
    re.compile(r"^urn:ogc:def:crs:EPSG:[\d.]*:(\d+)$", re.IGNORECASE),
    re.compile(r"^urn:x-ogc:def:crs:EPSG:[\d.]*:(\d+)$", re.IGNORECASE),
    re.compile(r"^https?://www\.opengis\.net/def/crs/EPSG/[\d.]+/(\d+)$", re.IGNORECASE),
)
_XY_ORDER_PATTERNS = (
    re.compile(r"^EPSG:(\d+)$", re.IGNORECASE),
    re.compile(r"^https?://www\.opengis\.net/gml/srs/epsg\.xml#(\d+)$", re.IGNORECASE),
)
_CRS84_PATTERNS = (
    re.compile(r"^urn:ogc:def:crs:OGC:1\.3:CRS84$", re.IGNORECASE),
    re.compile(r"^https?://www\.opengis\.net/def/crs/OGC/1\.3/CRS84$", re.IGNORECASE),
)


class UnsupportedCrsError(ValueError):
    pass


@dataclass(frozen=True)
class CrsReference:
    srs_name: str
    epsg: int
    authority_axis_order: bool

    @property
    def label(self) -> str:
        return f"EPSG:{self.epsg}"

    @property
    def needs_axis_swap(self) -> bool:
        return self.authority_axis_order and first_axis_is_northing(self.epsg)


def parse_srs_name(srs_name: str) -> CrsReference:
    value = srs_name.strip()
    for pattern in _CRS84_PATTERNS:
        if pattern.match(value):
            return CrsReference(srs_name=value, epsg=4326, authority_axis_order=False)
    for pattern in _AUTHORITY_ORDER_PATTERNS:
        match = pattern.match(value)
        if match:
            return _checked(value, int(match.group(1)), authority=True)
    for pattern in _XY_ORDER_PATTERNS:
        match = pattern.match(value)
        if match:
            return _checked(value, int(match.group(1)), authority=False)
    raise UnsupportedCrsError(f"unrecognised srsName {srs_name!r}")


def _checked(srs_name: str, epsg: int, *, authority: bool) -> CrsReference:
    try:
        _crs(epsg)
    except CRSError as exc:
        raise UnsupportedCrsError(f"unknown EPSG code in srsName {srs_name!r}") from exc
    return CrsReference(srs_name=srs_name, epsg=epsg, authority_axis_order=authority)


@lru_cache(maxsize=32)
def _crs(epsg: int) -> CRS:
    return CRS.from_epsg(epsg)


@lru_cache(maxsize=32)
def first_axis_is_northing(epsg: int) -> bool:
    axes = _crs(epsg).axis_info
    return bool(axes) and axes[0].direction.lower() in ("north", "south")


@lru_cache(maxsize=32)
def get_transformer(source_epsg: int) -> Transformer | None:
    """
    Transformer from ``source_epsg`` to EPSG:25832, or ``None`` when the
    source already is EPSG:25832 (no-op, so coordinates stay bit-identical).

    ``allow_ballpark=False`` refuses transformations that silently ignore
    a datum change; if PROJ only has a ballpark option the transformation
    fails loudly instead.
    """

    if source_epsg == TARGET_EPSG:
        return None
    return Transformer.from_crs(
        _crs(source_epsg),
        _crs(TARGET_EPSG),
        always_xy=True,
        allow_ballpark=False,
    )


def transformation_description(source_epsg: int) -> str:
    transformer = get_transformer(source_epsg)
    if transformer is None:
        return f"none (source CRS is {TARGET_CRS_LABEL})"
    accuracy = transformer.accuracy
    suffix = f"; accuracy {accuracy} m" if accuracy is not None and accuracy >= 0 else ""
    return f"{transformer.description}{suffix}"


@lru_cache(maxsize=1)
def target_bounds_from_lonlat(
    bounds: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    transformer = Transformer.from_crs(_crs(4326), _crs(TARGET_EPSG), always_xy=True)
    minx, miny, maxx, maxy = transformer.transform_bounds(*bounds, densify_pts=21)
    return (minx, miny, maxx, maxy)
