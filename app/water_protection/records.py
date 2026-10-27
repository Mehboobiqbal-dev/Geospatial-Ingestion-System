"""
Processed records handed from the pipeline to persistence and export.

``content_hash`` covers everything that is written as zone content:
normalised attributes, raw source properties, the normalised geometry
(rounded to millimetres so PROJ/GEOS floating-point noise does not
cause spurious updates) and the mapping identifier (so a corrected
mapping re-classifies existing rows). Run-specific values - timestamps,
run ids, paging parameters - are excluded, which is what makes a
repeated run report every row as unchanged.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np
import shapely
from shapely.geometry import MultiPolygon

from app.water_protection.geometry import NormalizedGeometry
from app.water_protection.mapping import MappedAttributes
from app.water_protection.wfs.gml import PropertyValue


@dataclass(frozen=True)
class StagedZone:
    attributes: MappedAttributes
    geometry: NormalizedGeometry
    gml_id: str | None
    source_attributes: dict[str, PropertyValue]
    source_layer: str
    source_url: str
    retrieved_at: datetime
    mapping_version: str
    content_hash: str

    @property
    def key(self) -> str:
        return self.attributes.source_zone_id

    @property
    def warnings(self) -> tuple[str, ...]:
        return self.attributes.warnings + self.geometry.warnings

    @property
    def validation_status(self) -> str:
        return "valid_with_warnings" if self.warnings else "valid"


@dataclass(frozen=True)
class QuarantinedFeature:
    state_code: str
    record_key: str
    record_key_kind: str  # source_zone_id | gml_id | content_sha256
    reason_code: str
    reason_detail: str
    evidence: dict[str, Any] | None
    source_zone_id: str | None
    gml_id: str | None
    source_attributes: dict[str, PropertyValue]
    source_geometry_wkb: bytes | None
    source_srs_name: str | None
    source_layer: str
    source_url: str


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()


def geometry_fingerprint(geometry: MultiPolygon) -> str:
    rounded = shapely.transform(geometry, lambda coords: np.round(coords, 3))
    return shapely.to_wkb(shapely.normalize(rounded), hex=True)


def compute_content_hash(
    attributes: MappedAttributes,
    source_attributes: dict[str, PropertyValue],
    geometry: NormalizedGeometry,
    mapping_version: str,
) -> str:
    payload = {
        "attributes": attributes.to_hashable_dict(),
        "source_attributes": source_attributes,
        "geometry": geometry_fingerprint(geometry.geometry),
        "geometry_repaired": geometry.repaired,
        "source_crs": geometry.source_crs,
        "mapping_version": mapping_version,
    }
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def raw_record_key(source_attributes: dict[str, PropertyValue], geometry_wkb: bytes | None) -> str:
    digest = hashlib.sha256(_canonical_json(source_attributes))
    if geometry_wkb:
        digest.update(geometry_wkb)
    return digest.hexdigest()
