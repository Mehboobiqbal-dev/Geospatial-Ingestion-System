# Quality and error report: IRIS-WSG-TH (Task 076)

Status as of 2026-10-27. "Verified" below means a test or command was
actually executed with the stated result; the commands are listed at the end.

## 1. Live source verification

| Check | Result |
|---|---|
| `GET https://geoportal.thueringen.de/wfs/wsg?SERVICE=WFS&REQUEST=GetCapabilities` | HTTP 200, `text/html`, 45 810 bytes, page title "Link11 - CAPTCHA" |
| Same with `VERSION=2.0.0` and `VERSION=1.1.0` | HTTP 200, `text/html`, CAPTCHA page |
| `DescribeFeatureType` with `TYPENAMES=th_wsg_zonen` | HTTP 200, `text/html`, CAPTCHA page |
| Same URL in a browser after passing the challenge | TYPO3 CMS "404 Page Not Found - the requested page does not exist" |
| `python -m app.water_protection inspect` (2026-10-27 22:17 UTC) | exit 1, `UnexpectedContentError` on `GetCapabilities`: "received an HTML page instead of WFS XML (page title: 'Link11 - CAPTCHA')"; one request, not retried |

Conclusion: the endpoint is protected by bot detection and, behind it, does
not serve a WFS at this path. The challenge was not bypassed. Not confirmed
live, therefore: WFS versions, output formats, paging support, declared CRS,
property names, zone codes, whether extraction rates are published, and
whether `zone_id` (or another property) is a stable identifier. Public
catalogue searches did not turn up a confirmed alternative WFS URL for this
layer.

Consequences in the implementation:

* the mapping file is marked `schema_verified = false`; `load` refuses to run
  unless `--allow-unverified-mapping` is passed, and the report says so;
* `inspect` records exactly this situation (exit code 1,
  `UnexpectedContentError` naming the HTML page title), and the same detection
  protects every WFS request;
* all fixtures are synthetic and labelled as such
  (`tests/water_protection/fixtures/README.md`).

## 2. Rejected records (fixture demonstrator)

`getfeature_edge_cases.xml`: 18 features, 9 accepted, 8 rejected, 1 identical
repeat collapsed (18 = 9 + 8 + 1).

| Record key | Reason code | Detail |
|---|---|---|
| TEST-Z-008 | `repair_exceeds_tolerance` | bow-tie ring; `make_valid` would turn an area of 0 into 5 000 m², so the boundary is ambiguous |
| TEST-Z-009 | `missing_geometry` | geometry property is `xsi:nil` |
| th_wsg_zonen.10 | `missing_source_zone_id` | no `zone_id`; keyed by gml:id in quarantine |
| TEST-Z-011 | `unsupported_geometry_type` | LineString |
| TEST-Z-012 | `malformed_geometry` | posList has 5 values, not a multiple of 2 |
| TEST-Z-013 | `outside_plausibility_extent` | northing/easting swapped while labelled EPSG:25832 |
| th_wsg_zonen.16, .17 | `conflicting_duplicate_source_id` | two different features share TEST-Z-016; both held back rather than choosing one |

Accepted with warnings: TEST-Z-006 (`unknown_zone_code`, published code
`III A` kept, category `UNKNOWN`), TEST-Z-007 (`geometry_repaired`, spike
removed, area change 0.0), TEST-Z-015 (`missing_zone_code`). TEST-Z-018 was
delivered in `urn:ogc:def:crs:EPSG::4326` (latitude first) and transformed to
EPSG:25832 ("Inverse of ETRS89 to WGS 84 (1) + UTM zone 32N", 1 m).

Loaded twice into PostGIS: run 1 inserted 9 / quarantined 8, run 2 inserted 0,
updated 0, unchanged 9, quarantine still 8 rows with `occurrences = 2`.

## 3. Geometry edge cases

| Case | Behaviour | Test |
|---|---|---|
| Polygon | stored as 1-part MultiPolygon, coordinates unchanged in 25832 | `test_geometry.py` |
| MultiPolygon with holes | parts and holes kept | `test_geometry.py`, `test_gml.py` |
| GML 3.2 posList / pos, GML 2 coordinates, srsDimension 3 | parsed; Z dropped with warning | `test_gml.py` |
| Unclosed ring, odd posList, non-numeric value, mixed srsName | `malformed_geometry` | `test_gml.py` |
| Arcs / Ring / Solid | `unsupported_geometry_type` (not linearised) | `test_gml.py` |
| Missing, nil, empty, NaN/Inf, Point/LineString | quarantined with reason | `test_geometry.py` |
| Spike / self-touching ring | repaired, evidence stored | `test_geometry.py`, integration |
| Bow-tie, overlapping parts | quarantined (area change above 0.1 %) | `test_geometry.py` |
| lat/lon URN vs `EPSG:4326` lon/lat | identical result after axis handling | `test_geometry.py` |
| Swapped axes / wrong CRS label | `outside_plausibility_extent` | `test_geometry.py` |
| UTM 32 projection | 9 deg E maps to exactly 500 000 m easting (independent check) | `test_crs.py` |
| Parcel inside, on edge, outside on edge, corner touch, partial, covering, disjoint, in hole, filling hole, across multipart gap | `within` / `boundary_contact` / `partial_overlap` / `disjoint` with exact overlap areas | `test_spatial.py`; PostGIS agrees (integration) |

## 4. Missing and unknown source attributes

* Zone codes not in the table (`III A`, `ii`, `Zone II`, `IIIa`, `III/A`,
  `IV`, Heilquellenschutzzone) are stored as `UNKNOWN` with the raw value;
  a missing code is `UNKNOWN` with `zone_code_source` NULL.
* Extraction rates: the current mapping publishes none, so every row has
  `extraction_rate` NULL and status `not_published`. Text such as
  "Foerdermenge ca. 500 m3/d" in a remark is ignored. If a rate property is
  mapped: numeric -> `published`, empty -> `missing`, `1.234,5` / `ca. 500` /
  negative -> `unparseable` (NULL + warning). A database CHECK enforces that a
  rate exists only with status `published`.
* Unparseable dates become NULL with a warning; raw values remain in
  `source_attributes`.
* Source properties that are not mapped are reported as schema drift
  (`schema.unmapped_properties`) and kept in `source_attributes`.

## 5. Retrieval failures

| Failure | Behaviour | Test |
|---|---|---|
| Connect/read timeout, connection error | bounded retries (exponential backoff with jitter), then `HttpRequestError` | `test_http.py` |
| 429/502/503/504, plain 500 | retried, `Retry-After` honoured within the cap | `test_http.py` |
| OWS exception (any status, including 200) | `WfsServiceException` with code/locator, not retried | `test_http.py`, `test_client.py` |
| HTML instead of XML (bot protection) | `UnexpectedContentError` with the page title, not retried | `test_capabilities.py`, `test_client.py` |
| Malformed XML, wrong root, XXE entity | rejected; entities never resolved | `test_capabilities.py` |
| Oversized response | `ResponseTooLargeError` | `test_http.py` |
| Layer not advertised, no GML format, unsupported version | `CapabilitiesError` listing what is available | `test_capabilities.py` |
| Live schema differs from mapping | `SchemaMismatchError` before any GetFeature | `test_pipeline.py` |
| Missing page, numberReturned mismatch, numberMatched changing, repeated features | `IncompleteRetrievalError`, nothing loaded | `test_client.py` |
| Feature/page limits | `RetrievalLimitExceeded` (checked from `hits` before downloading) | `test_client.py` |
| Valid empty collection | `succeeded_empty`, exit 0 (distinct from failure) | `test_pipeline.py` |
| Database error in a later batch | whole load rolled back, run logged as failed | `test_loader.py`, integration |

## 6. Coverage limitations

* No real data could be retrieved, so completeness, feature counts and the
  actual geographic coverage of `th_wsg_zonen` are unknown.
* Zones of Heilquellenschutzgebiete or other schemes would currently be
  `UNKNOWN` until codes are verified and added to the mapping.
* The plausibility screen is a lon/lat box, not the Thuringia boundary;
  zones slightly outside the state (cross-border catchments) pass.
* Positional accuracy of the official zones is unknown; parcel results near a
  boundary should be reviewed, and `boundary_contact` is reported separately.

## 7. Acceptance criteria

From the task PDF:

| Criterion | Status | Evidence |
|---|---|---|
| Official zone classifications are traceable to source attributes and not inferred from a fragile substring rule | **Verified** | exact table lookup; `test_mapping.py` (5 codes mapped, 7 variants stay UNKNOWN); validation query 4 shows `zone_code_source` and raw `source_attributes ->> 'zone'` per row |
| Geospatial and duplicate-load validation succeed on representative fixtures | **Verified** | `test_geometry.py`, `test_spatial.py`, `test_pipeline.py`; PostGIS integration: second load 0 inserted / 9 unchanged, geometries SRID 25832, valid, equal to input |

Additional checklist:

| Item | Status |
|---|---|
| Endpoint and layer inspected, or limitation documented | Limitation documented (section 1) |
| Field mappings verified against the actual schema | **Not verifiable**: live schema unavailable; mapping marked unverified, `inspect` + schema check provided |
| Missing/unpublished extraction rates stay unknown | Verified (tests + DB CHECK + query 5) |
| Geometries validated and transformed to EPSG:25832 | Verified |
| Multipart polygons and parcel containment/intersection tested | Verified |
| Idempotent persistence keyed by source zone ID and state code | Verified on PostGIS 3.6 / PostgreSQL 17 |
| Writes guarded to the staging destination | Verified (`test_staging_guard.py`, `test_staging_sql.py`, least-privilege role) |
| Docker, pinned dependencies, reproducible instructions | Verified: images built from exact pins, compose flow and in-container test run (section 8) |
| No secrets or production writes | `.env` is git-ignored, `.env.example` holds placeholders only; no production target was ever configured |

## 8. Verification log

Local machine: Windows 10, Python 3.12.10, GEOS 3.14.1, PROJ 9.8.1.
Database for integration tests: PostgreSQL 17.6 + PostGIS 3.6.2 (local,
disposable), initialised with `docker/postgis-staging/initdb/10-iris-staging.sh`.

| Command | Result |
|---|---|
| `ruff check app/water_protection app/database/staging alembic_staging/env.py tests` | All checks passed |
| `ruff format --check ...` (same paths) | 50 files already formatted |
| `mypy app/water_protection app/database/staging` | no issues in 28 source files |
| `python -m pytest tests/water_protection` (no database) | 211 passed, integration module skipped with reason |
| same with `IRIS_TEST_DATABASE_URL` set | 218 passed (211 + 7 PostGIS integration), 93 % line coverage of the new code |
| `python -m app.water_protection init-db` (twice) | migration applied; second run no-op |
| `alembic -c alembic_staging.ini check` | No new upgrade operations detected (models = database) |
| `python -m app.water_protection inspect` (live endpoint) | exit 1, bot-protection page detected (section 1) |

Docker (Docker Desktop 29.9.2, Compose v5.6.1):

| Command | Result |
|---|---|
| `docker compose config` / `--profile wsg config` | valid; without the profile only the existing `postgres` and `pgadmin` services start |
| `docker compose --profile wsg build wsg-ingest wsg-tests` | images `iris-wsg-th:local` (713 MB) and `iris-wsg-th:test` built from the pinned requirements |
| `docker run --entrypoint id iris-wsg-th:local` | `uid=10001(iris)`: runs as non-root |
| `docker compose --profile wsg up -d postgis-staging` | init script created role, test database, PostGIS and schemas; healthy only after init completed |
| `docker compose --profile wsg run --rm wsg-ingest init-db` / `check-db` | migration applied as `iris_staging_writer`; PostGIS 3.5.2 |
| `wsg-ingest load --source-file /fixtures/getfeature_edge_cases.xml --allow-unverified-mapping` (twice) | exit 3; 9 inserted / 8 quarantined, then 0 inserted / 0 updated / 9 unchanged ([example_report.json](example_report.json)) |
| `docker compose --profile wsg run --rm wsg-tests` | 218 passed, 0 skipped (integration against PostgreSQL 17.5 / PostGIS 3.5.2) |
| `psql -f docs/wsg_th/validation_queries.sql` | all 10 queries as expected ([example_validation_output.txt](example_validation_output.txt)) |

Defects found by running against real PostGIS and Docker, all fixed and covered:

* `None` in JSONB columns was stored as JSON `null`, violating the
  repair-evidence CHECK; the load rolled back as designed. Fixed with
  `JSONB(none_as_null=True)`; integration tests compare stored rows.
* The staging Alembic environment joined a transaction auto-begun by its
  pre-checks, so the migration was rolled back on close; it now commits the
  pre-check transaction first.
* `CREATE SCHEMA IF NOT EXISTS` needs database-level CREATE even when the
  schema exists; the least-privilege role does not have it, so the schema is
  now created only when missing.
* The init script was checked out with CRLF on Windows and failed in the
  Linux container; `.gitattributes` keeps container files LF, and the health
  check now proves that initialisation completed.
* The mapping identifier hashed raw file bytes and so differed between CRLF
  and LF checkouts; it now hashes the parsed mapping (regression test added).
