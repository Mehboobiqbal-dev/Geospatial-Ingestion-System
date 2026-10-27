# IRIS-WSG-TH: Thuringia water protection zones

Ingests the official water protection zones (Wasserschutzgebietszonen) of
Thuringia from an OGC WFS into `iris_staging.water_protection_zones`
(PostGIS, EPSG:25832) for permitting and groundwater-restriction screening.

> **Live source status (2026-10-27):** the specified endpoint could not be
> inspected. Scripted requests receive an HTML bot-protection page (CAPTCHA)
> with HTTP 200; a browser that passes the check receives a TYPO3
> "404 Page Not Found". The pipeline is therefore delivered as a
> fixture-based demonstrator, as allowed by the task notes. The source
> property names in the mapping are **assumptions** and the mapping is marked
> `schema_verified = false`; loads refuse to run until it is verified or the
> operator overrides explicitly. Details: [QUALITY_REPORT.md](QUALITY_REPORT.md).

## How it works

```
WFS (GetCapabilities, DescribeFeatureType, GetFeature)
  -> wfs/http.py         timeouts, bounded retries + jitter, rate limit, size cap, redacted URLs
  -> wfs/capabilities.py version, layer, declared CRS, output formats, paging/sorting support
  -> wfs/schema.py       live schema checked against the mapping before any feature is fetched
  -> wfs/client.py       hits count, paged GetFeature, completeness checks
  -> wfs/gml.py          GML 3.2 / 3.1.1 / 2 parsing; per-feature geometry errors are captured
  -> mapping.py          TOML mapping: exact zone-code table, published values only
  -> geometry.py, crs.py validation, conservative repair, axis order, transform to EPSG:25832
  -> pipeline.py         de-duplication, quarantine, report
  -> loader.py           one transaction, batched idempotent upsert
  -> app/database/staging guard + SQLAlchemy models + repository (iris_staging only)
```

`spatial.py` provides the parcel checks used for screening (see below).
Everything is driven by `python -m app.water_protection` (`cli.py`).

The module lives alongside the existing NRW platform and reuses its stack
(SQLAlchemy 2, GeoAlchemy2, Alembic, GeoPandas, Shapely, pyproj, psycopg 3).
It deliberately does **not** reuse `app/database/session.py` or `DATABASE_URL`,
which point at the iris_core application database.

## Source and provenance

| Item | Value |
|---|---|
| Endpoint | `https://geoportal.thueringen.de/wfs/wsg` (`WSG_WFS_URL`) |
| Layer | `th_wsg_zonen` (`WSG_LAYER`) |
| Publisher | Thuringian State Office for Environment, Mining and Nature Conservation (TLUBN) |
| WFS versions | 2.0.0 preferred, 1.1.0 supported (`ACCEPTVERSIONS`) |
| Output formats | GML 3.2, GML 3.1.1, GML 2, chosen from what the service advertises |
| Source CRS | srsName on each geometry, falling back to the layer's declared DefaultCRS |

Every stored row carries `source_url` (canonical GetFeature request without
paging parameters, credentials redacted, or `file://...#sha256=` for replays),
`source_layer`, `source_feature_gml_id`, all raw properties in
`source_attributes`, `source_retrieved_at`, `source_modified_date` (if
published), `mapping_version` and the CRS provenance (`source_crs`,
`source_srs_name`, `crs_transformation`). Every load run, including failed and
empty ones, is logged in `iris_staging.water_protection_zone_ingestion_runs`
with its full JSON report.

### Inspecting the service and reproducing a retrieval

```bash
python -m app.water_protection inspect --output inspection.json
```

records the capabilities (versions, formats, paging, CRS, bounding box), the
DescribeFeatureType schema, whether it matches the mapping, and a few sample
features. Against the current endpoint it exits with code 1 and records the
bot-protection response. A GetFeature response saved by other legitimate means
can be processed reproducibly with `--source-file` (its SHA-256 is recorded).

## Source-to-destination mapping

The mapping is data: [`app/water_protection/mappings/th_wsg_zonen.toml`](../../app/water_protection/mappings/th_wsg_zonen.toml).
`""` means "not published by the source": the column stays NULL with an
explicit status, and the value is never derived from other attributes.

| Destination column | Source property (assumed) | Rule |
|---|---|---|
| `source_zone_id` | `zone_id` | required, trimmed; missing -> record quarantined |
| `state_code` | - | `TH` from the mapping (Thuringia-only layer) |
| `protection_area_id`, `protection_area_name` | `wsg_nr`, `wsg_name` | verbatim |
| `zone_code_source` | `zone` | verbatim, as published |
| `zone_category`, `zone_legal_description`, `zone_classification_basis` | via `zone` | exact lookup in `[[zone_classification]]`; otherwise `UNKNOWN` + NULL |
| `zone_label_source` | `zone_bez` | verbatim |
| `restriction_category_source` | - | not published -> NULL |
| `legal_status_source`, `legal_basis_source` | `status`, `rechtsgrundlage` | verbatim |
| `legal_effective_date`, `source_modified_date` | `datum_festsetzung`, `aenderungsdatum` | xsd:date / dateTime; otherwise NULL + warning |
| `extraction_rate`, `extraction_rate_unit` | - | not published -> NULL, `extraction_rate_status = 'not_published'` |
| `source_attributes` | all properties | raw, including unmapped ones |

Zone classification table (DVGW W 101 zone types; the legal effect of an
individual zone comes from its own ordinance under Sections 51 and 52 WHG):

| Published code | `zone_category` | `zone_legal_description` |
|---|---|---|
| `I` | `ZONE_I` | Schutzzone I (Fassungsbereich) |
| `II` | `ZONE_II` | Schutzzone II (Engere Schutzzone) |
| `III` | `ZONE_III` | Schutzzone III (Weitere Schutzzone) |
| `IIIA` | `ZONE_IIIA` | Schutzzone III A (Weitere Schutzzone, Teilzone A) |
| `IIIB` | `ZONE_IIIB` | Schutzzone III B (Weitere Schutzzone, Teilzone B) |
| anything else | `UNKNOWN` | NULL (raw code kept in `zone_code_source`) |

Matching is exact after trimming outer whitespace: no case folding, no
substring or similarity rules. `III A`, `ii` or `Zone II` stay `UNKNOWN`
until a verified row is added to the table. If an extraction-rate property is
published, map it in `[fields]`; only an xsd-numeric value is accepted
(`published`), an empty one becomes `missing` and text such as `1.234,5` or
`ca. 500` becomes `unparseable` (raw value kept, warning raised).

**Verifying the mapping against the live service:** run `inspect`, correct the
property names and codes in the TOML file, set `schema_verified = true`, bump
`version`. Any edit changes `mapping_version`, so the next load re-classifies
affected rows as updates.

## CRS and geometry decisions

* Features are requested in the layer's declared CRS (no `SRSNAME`) and
  transformed locally with pyproj (`always_xy=True`, `allow_ballpark=False`).
  Data already in EPSG:25832 is not touched. The PROJ operation and its
  accuracy are stored per row and listed in the report.
* Axis order follows the srsName form: `urn:ogc:def:crs:EPSG::4326` and
  `http://www.opengis.net/def/crs/EPSG/0/...` use EPSG order (lat/lon,
  northing/easting for Gauss-Krueger 31468), `EPSG:xxxx` and
  `.../epsg.xml#xxxx` use x/y, CRS84 is lon/lat.
* Missing, empty, non-finite, non-polygonal, unparseable or arc-based
  geometries and unknown CRSs are quarantined with a reason code.
* Invalid polygons are repaired with `make_valid(method="structure")` only if
  the result is a valid (Multi)Polygon and the area changes by at most
  `WSG_MAX_REPAIR_AREA_CHANGE_RATIO` (default 0.1 %). Typical accepted case:
  a spike or self-touching ring. Bow-ties and overlapping parts change the
  area materially and are quarantined for manual review, because the legally
  correct boundary cannot be decided automatically. Repairs store the GEOS
  reason, method, areas and original WKB in `geometry_repair_evidence`.
* After transformation the geometry is re-validated and must lie inside a
  generous lon/lat box around Thuringia (9.5-13.0 E, 49.9-52.0 N). This catches
  swapped axes and mislabelled CRSs; it is a plausibility screen, not a clip.
* Everything is stored as `MultiPolygon` (SRID 25832); a CHECK constraint
  enforces `ST_IsValid` in the database as well.

### Parcel screening

`app/water_protection/spatial.py`:

* `parcel_within_zone(parcel, zone)`: the whole parcel is inside
  (`zone.covers(parcel)`; sharing the zone edge still counts as inside);
* `parcel_intersects_zone(parcel, zone)`: shared **area** (DE-9IM
  `T********`); touching only along an edge or at a corner is not an
  intersection. `include_boundary_contact=True` gives plain `intersects`;
* `assess_parcel()` returns `within`, `partial_overlap`, `boundary_contact` or
  `disjoint` with overlap area and ratio; holes and multipart zones are
  respected;
* `screen_parcels(parcels_gdf, zones_gdf, ...)` does the same for whole
  GeoDataFrames with a spatial-index join, reprojecting parcels to EPSG:25832.

The same classification in SQL is query 10 in
[validation_queries.sql](validation_queries.sql).

## Database schema and idempotency

Created by the separate Alembic environment `alembic_staging/` (version table in
`iris_staging`; the iris_core chain in `alembic/` is untouched):

* `iris_staging.water_protection_zones`: one row per zone;
  `UNIQUE (state_code, source_zone_id)`; GiST index on `geom`
  (`geometry(MultiPolygon, 25832)`); CHECK constraints for categories,
  extraction-rate consistency, repair evidence, validity and the key format.
* `iris_staging.water_protection_zones_quarantine`: rejected features with
  reason, evidence, raw attributes and raw geometry;
  `UNIQUE (state_code, record_key, reason_code)` with an `occurrences` count.
* `iris_staging.water_protection_zone_ingestion_runs`: one row per load run.

A load runs in **one transaction** under an advisory lock and writes in
batches (`IRIS_STAGING_BATCH_SIZE`). Each row has a `content_hash` over the
normalised attributes, raw properties, geometry (rounded to millimetres) and
mapping version. The upsert is
`INSERT ... ON CONFLICT (state_code, source_zone_id) DO UPDATE ... WHERE content_hash IS DISTINCT FROM excluded.content_hash`,
so unchanged rows are never rewritten. Counts are reconciled per batch
(inserted / updated / unchanged must match the rows returned); any mismatch
or error rolls the whole load back. Nothing is ever deleted: zones missing
from the source, or whose source feature was rejected in this run, are
reported (`absent_from_source`, `stale_after_rejection`) for review.

## Safety boundary

Writes are only possible after `app/database/staging/guard.py` accepts the
target, before any connection is opened:

* `IRIS_ENVIRONMENT` must be `development`, `test` or `staging`;
* the schema must be `iris_staging` (`iris_core` and `public` are refused);
* only `postgresql+psycopg` URLs with a database name; names or hosts that look
  like production (`prod`, `production`, `live`, `prd`) are refused;
* optional host allowlist `IRIS_STAGING_ALLOWED_HOSTS`;
* inside the write transaction the server's `current_database()` and the
  PostGIS/schema presence are checked again.

The Docker setup adds a least-privilege role (`iris_staging_writer`) that owns
only the `iris_staging` schema and cannot create objects in `public`; the
staging PostGIS instance contains no iris_core schema at all.

## Configuration

Copy [`.env.example`](../../.env.example) to `.env` and replace the
`change-me` values. The ingestion uses only these variables:

| Variable | Purpose |
|---|---|
| `IRIS_ENVIRONMENT` | `development` / `test` / `staging` (required for writes) |
| `IRIS_STAGING_DATABASE_URL` | `postgresql+psycopg://user:pass@host:port/db` (staging only) |
| `IRIS_STAGING_ALLOWED_HOSTS` | optional comma-separated host allowlist |
| `IRIS_STAGING_BATCH_SIZE` | rows per upsert statement (default 500) |
| `IRIS_TEST_DATABASE_URL` | integration tests; database name must end in `_test` |
| `WSG_*` | WFS URL, layer, timeouts, retries, paging, limits, repair tolerance (defaults in `.env.example`) |

No credentials are needed to install dependencies, run unit tests or run
`validate`.

## Running it

### Docker (recommended)

```bash
cp .env.example .env                                      # set the change-me values
docker compose --profile wsg up -d postgis-staging        # isolated staging PostGIS (localhost:5434)
docker compose --profile wsg run --rm wsg-ingest init-db  # apply staging migrations
docker compose --profile wsg run --rm wsg-ingest check-db # read-only target check
docker compose --profile wsg run --rm wsg-tests           # full test suite incl. PostGIS integration tests

# live source
docker compose --profile wsg run --rm wsg-ingest inspect
docker compose --profile wsg run --rm wsg-ingest validate                 # dry run, no DB writes

# fixture demonstrator
docker compose --profile wsg run --rm -v "$PWD/tests/water_protection/fixtures:/fixtures:ro" \
  wsg-ingest validate --source-file /fixtures/getfeature_edge_cases.xml
docker compose --profile wsg run --rm -v "$PWD/tests/water_protection/fixtures:/fixtures:ro" \
  wsg-ingest load --source-file /fixtures/getfeature_edge_cases.xml --allow-unverified-mapping

docker compose --profile wsg stop postgis-staging         # stop; data is kept in the volume
```

The profile keeps a plain `docker compose up` (platform PostGIS + pgAdmin)
unchanged. The image runs as a non-root user and contains only this module.

### Local Python

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements/water-protection-dev.txt   # or: uv sync --group dev

python -m app.water_protection validate --source-file tests/water_protection/fixtures/getfeature_edge_cases.xml
python -m app.water_protection validate --export zones.gpkg   # live source, GeoPackage for review
python -m app.water_protection init-db
python -m app.water_protection load --report reports/run.json
```

`load` exits with code 2 while the mapping is still marked unverified; add
`--allow-unverified-mapping` to load anyway (the report and run log record it).

### Tests and checks

```bash
python -m pytest tests/water_protection                       # offline: unit, CLI, mocked WFS
IRIS_TEST_DATABASE_URL=postgresql+psycopg://iris_staging_writer:...@localhost:5434/iris_staging_test \
  python -m pytest tests/water_protection/integration          # real PostGIS
ruff check app/water_protection app/database/staging alembic_staging/env.py tests
ruff format --check app/water_protection app/database/staging alembic_staging/env.py tests
mypy app/water_protection app/database/staging
alembic -c alembic_staging.ini check                           # models and database in sync
```

Integration tests skip themselves unless `IRIS_TEST_DATABASE_URL` is set, and
refuse any database whose name does not end in `_test` (they truncate the
staging tables of that database).

## Output and how to read the report

Every `validate`/`load` prints a JSON report (and writes it with `--report`).
A full example from the fixture run is in
[example_report.json](example_report.json); the validation queries and their
output on that data are in [validation_queries.sql](validation_queries.sql)
and [example_validation_output.txt](example_validation_output.txt).

| Field | Meaning |
|---|---|
| `status` | `succeeded`, `succeeded_empty` (valid empty collection), `succeeded_with_rejections`, `failed` |
| `source` | endpoint, layer, WFS version, format, declared CRS, pages, numberMatched, request URL or file SHA-256 |
| `mapping` | mapping identifier and whether it is verified |
| `schema` | live schema check: unmapped source properties (drift) and warnings |
| `counts` | `retrieved = accepted + rejected + duplicates_collapsed`; repaired, multipart, with_warnings |
| `rejections_by_reason`, `rejected_samples` | why features were quarantined |
| `warnings_by_code` | accepted with warnings (`unknown_zone_code`, `geometry_repaired`, ...) |
| `transformations` | CRS operations used |
| `database` | `inserted`, `updated`, `unchanged`, `quarantined`, `absent_from_source`, `stale_after_rejection` |
| `error` | type and message of a failed run (operation names included, credentials never) |

Exit codes: `0` success, `1` failure, `2` refused (configuration or safety
guard), `3` completed with rejected features.

## Limitations and assumptions

* The live endpoint was not reachable (bot protection / HTTP 404); property
  names, zone codes, the published CRS and the identifier's stability are
  unverified. See [QUALITY_REPORT.md](QUALITY_REPORT.md).
* Only GML output is supported; GeoJSON-only services would need a parser.
* Curved geometries (arcs) are quarantined rather than linearised.
* Transformations from WGS 84 use the EPSG null transformation to ETRS89
  (about 1 m accuracy); DHDN sources need the BeTA2007 grid for best accuracy.
  Thuringia publishes in ETRS89/UTM, where no transformation happens.
* The plausibility extent is a coarse lon/lat box, not the state boundary.
* `zone_legal_description` names the zone type; it is not a list of
  restrictions. Restrictions come from each protection-area ordinance.

## Recovery

* **Retrieval failed** (timeout, HTML, exception report, incomplete paging):
  nothing was written; the run is logged as `failed` with the operation name.
  Re-run later; adjust `WSG_*` timeouts or page size if the service is slow.
* **Schema mismatch:** run `inspect`, update the mapping, re-run.
* **Database error:** the load transaction was rolled back completely; fix
  the cause (see `error.message`) and re-run. Repeated runs are idempotent.
* **Rejected features:** review `iris_staging.water_protection_zones_quarantine`
  (reason, evidence, raw geometry); fix at the source or adjust the mapping or
  repair tolerance deliberately, then re-run.
