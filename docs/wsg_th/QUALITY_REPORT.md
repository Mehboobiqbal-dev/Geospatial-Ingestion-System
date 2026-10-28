# Quality and error report: IRIS-WSG-TH (Task 076)

Status as of 2026-10-28. "Verified" below means a test or command was
actually executed with the stated result; the commands are listed at the end.

## 1. Live source verification

### Endpoint named in the assignment

| Check | Result |
|---|---|
| `GET https://geoportal.thueringen.de/wfs/wsg?SERVICE=WFS&REQUEST=GetCapabilities` | HTTP 200, `text/html`, 45 810 bytes, page title "Link11 - CAPTCHA" |
| Same with `VERSION=2.0.0` and `VERSION=1.1.0` | HTTP 200, `text/html`, CAPTCHA page |
| `DescribeFeatureType` with `TYPENAMES=th_wsg_zonen` | HTTP 200, `text/html`, CAPTCHA page |
| Same URL in a browser after passing the challenge | TYPO3 CMS "404 Page Not Found - the requested page does not exist" |
| `inspect` with `WSG_WFS_URL` set to this endpoint (2026-10-27 22:17, 2026-10-28 00:52 and 13:57 UTC) | exit 1, `UnexpectedContentError` on `GetCapabilities`: "received an HTML page instead of WFS XML (page title: 'Link11 - CAPTCHA')"; one request each, not retried |

The endpoint is behind bot detection and, behind it, does not serve a WFS at
this path. The challenge was not bypassed.

### Official TLUBN service for the same dataset

The Thuringian metadata catalogue GeoMIS.Th describes the dataset
"Wasserschutzgebiete (WSG), Heilquellenschutzgebiete (HQSG)" (record
`F64A2B67-4215-11D5-94EE-9291C676F748`, publisher TLUBN, revision 2026-10-13,
CC BY 4.0, "Es gelten keine Zugriffsbeschränkungen") and its WFS (record
`c030866f-e305-4ba0-b96d-768cb02b550c`):
`https://www.geoproxy.geoportal-th.de/geoproxy/services/wsg_hqsg_wfs`.

| Check (2026-10-28) | Result |
|---|---|
| GetCapabilities | WFS 2.0.0; one layer `tmlnu:WASSER_UND_HEILQUELLENSCHUTZGEBIETE`; DefaultCRS EPSG:25832; `ImplementsResultPaging` FALSE, `CountDefault` 25 000, sorting supported; GML 2.1.2 to 3.2.1 and JSON |
| DescribeFeatureType | 15 properties: `Gebiet_Nr`, `Gebiet`, `Sg_Id`, `Name`, `Gebietsart`, `Zone`, `Status`, `Sz_Num`, `Bemerkung`, `Symbolik`, `Url`, `Scale`, `Herkunft`, `Stand`, `geom` |
| `inspect` with the first mapping (assumed names) | all 9 assumed property names reported missing: the schema check works, and the assumptions were wrong |
| Attribute download of all features | 3 090 features, values profiled (section 2) |
| `validate` / `load` with the verified mapping | 3 090 retrieved, 3 090 accepted, 0 rejected, 1 repaired, 89 multipart; ~12 MB in one request |
| Publisher's open-data download `wsg_hqsg.zip` (shapefile, Last-Modified 2026-10-13) | 3 090 features; same zone numbers and the same geometry for all 3 090 (section 2) |

The mapping was rebuilt from this evidence and is marked
`schema_verified = true`. The service is the configured default; the
assignment's endpoint remains selectable through `WSG_WFS_URL`/`WSG_LAYER`.
Recorded responses (capabilities, schema, an 11-feature excerpt) are in
`tests/water_protection/fixtures` and are used by the tests.

## 2. What the live data contains

Profile of all 3 090 features (Stand 2026-10-13):

| Property | Finding | Consequence |
|---|---|---|
| `Sz_Num` | `xsd:double` in the WFS, written `5.53210009E8`; unique (3 090 distinct); a structured zone number (below) | zone key, stored as `553210009`; non-whole values are rejected |
| `Gebiet_Nr` | unique as well; equals `gml:name` | kept in `source_attributes` |
| `gml:id` | `ID_WSG_HQSG.<n>`, a row counter of the response | not used as a key |
| `Zone` | 14 codes: I 1 469, II 948, III 600, QUAL I 24, IIIA 14, IIIB 13, QUAL II 5, QUAL III 5, QUANT A 3, QUANT B 3, IIA 2, IIB 2, IIC 1, IIIC 1 | every code is in the classification table; 0 `UNKNOWN` |
| `Gebiet` / `Gebietsart` | WSG / Wasserschutzzone 3 050, HQSG / HeilquellenSchutzzone 40 | healing-spring zones get their own categories (`HQSG_*`) |
| `Status` | `festgesetzt` 2 346, `in Planung / in Verfahren` 744 | stored verbatim in `legal_status_source` |
| `Sg_Id` / `Name` | 643 distinct numbers; 21 zones have a placeholder `Name` (`SZ_NUM: ...`), 19 of them under numbers 693-699 and 2 under 204; `Sg_Id` 449 carries WSG-coded zones named "HQSG Bad Sulza" | stored verbatim, reported here |
| `Bemerkung` | empty for 2 440; `Symbol` 625; a few legal remarks (`Beschluss nichtig`, `kein Beschluss`, `nur gedr. Karte`, ...) | `Symbol` is flagged (below); other remarks kept raw |
| `Stand` | 2026-10-13 for every feature | `source_modified_date` (data currency, not a per-zone change date) |
| `Herkunft` | empty for all features | nothing to map |
| Extraction rate, restriction category, ordinance reference, designation date | not published | NULL with explicit status; never derived |
| Geometry | 3 001 Polygon, 89 MultiSurface, all EPSG:25832, inside the expected extent | no transformation needed |

**Zone key.** `Sz_Num` is the authority's zone number, not a number made up
per export:

| Check over all 3 090 zones | Result |
|---|---|
| Digits 1-4 name a TK25 map sheet the zone touches | 3 041; the other 49 name the neighbouring sheet |
| Digit 5 matches the zone type (1 = I, 2 = II, 3 = III, 6-8 = QUAL I-III, 9 = QUANT) | 3 090 |
| Digit 6 matches the sub-zone (0 = none, 1-3 = A-C) | 3 090 |
| Same number in the publisher's shapefile, where `SZ_NUM` is a 64-bit integer | 3 090 |
| Same geometry for that number in the shapefile (at most 1 % area difference) | 3 090 |

Only one release is published at a time, so stability across future releases
cannot be observed directly. The load protects against it: a renumbered zone
is never overwritten, and a zone that disappears while one with the same
geometry is inserted is reported with both numbers
(`database.possible_renumbering`; `test_loader.py` and a PostGIS integration
test).

**Map symbols.** All 625 zones with the remark `Symbol` are zone I or QUAL I,
and 598 of them are identical 20 m x 20 m squares (400 m²). They mark a well,
not the legal boundary of zone I. They are loaded with the warning
`geometry_is_map_symbol`, because a parcel next to a well can be "disjoint"
from such a square and still lie in the real zone I.

## 3. Rejected records

**Live data:** none. One geometry was repaired: zone `463320106` (WSG
Bretleben, zone II), "Ring Self-intersection[654977.8 5688617.769]", repaired
with `make_valid(method='structure')`, area change 0.000000 (validation query 8).

**Synthetic edge cases** (`getfeature_edge_cases.xml`): 20 features, 10
accepted, 9 rejected, 1 identical repeat collapsed (20 = 10 + 9 + 1).

| Record key | Reason code | Detail |
|---|---|---|
| 990000008 | `repair_exceeds_tolerance` | bow-tie ring; `make_valid` would turn an area of 0 into 5 000 m², so the boundary is ambiguous |
| 990000009 | `missing_geometry` | geometry property is `xsi:nil` |
| ID_WSG_HQSG.10 | `missing_source_zone_id` | no `Sz_Num`; keyed by gml:id in quarantine |
| 990000011 | `unsupported_geometry_type` | LineString |
| 990000012 | `malformed_geometry` | posList has 5 values, not a multiple of 2 |
| 990000013 | `outside_plausibility_extent` | northing/easting swapped while labelled EPSG:25832 |
| 990000020.5 | `invalid_source_zone_id` | zone number is not a whole number |
| ID_WSG_HQSG.16, .17 | `conflicting_duplicate_source_id` | two different features share 990000016; both held back rather than choosing one |

Accepted with warnings: 990000006 (`unknown_zone_code`, published code
`III A` kept, category `UNKNOWN`), 990000007 (`geometry_repaired`, spike
removed, area change 0.0), 990000015 (`missing_zone_code`), 990000019
(`geometry_is_map_symbol`). 990000018 was delivered in
`urn:ogc:def:crs:EPSG::4326` (latitude first) and transformed to EPSG:25832
("Inverse of ETRS89 to WGS 84 (1) + UTM zone 32N", 1 m).

## 4. Geometry edge cases

| Case | Behaviour | Test |
|---|---|---|
| Polygon (3 001 live features) | stored as 1-part MultiPolygon, coordinates unchanged in 25832 | `test_geometry.py`, live excerpt |
| MultiSurface (89 live features), holes | parts and holes kept | `test_geometry.py`, `test_gml.py`, live excerpt |
| GML 3.2 posList / pos, GML 2 coordinates, srsDimension 3, geometry `gml:id`, inline comments | parsed; Z dropped with warning | `test_gml.py`, live excerpt |
| Unclosed ring, odd posList, non-numeric value, mixed srsName | `malformed_geometry` | `test_gml.py` |
| Arcs / Ring / Solid | `unsupported_geometry_type` (not linearised) | `test_gml.py` |
| Missing, nil, empty, NaN/Inf, Point/LineString | quarantined with reason | `test_geometry.py` |
| Spike / self-touching ring | repaired, evidence stored (one live zone) | `test_geometry.py`, live excerpt, integration |
| Bow-tie, overlapping parts | quarantined (area change above 0.1 %) | `test_geometry.py` |
| lat/lon URN vs `EPSG:4326` lon/lat | identical result after axis handling | `test_geometry.py` |
| Swapped axes / wrong CRS label | `outside_plausibility_extent` | `test_geometry.py` |
| UTM 32 projection | 9 deg E maps to exactly 500 000 m easting (independent check) | `test_crs.py` |
| Parcel inside, on edge, outside on edge, corner touch, partial, covering, disjoint, in hole, filling hole, across multipart gap | `within` / `boundary_contact` / `partial_overlap` / `disjoint` with exact overlap areas | `test_spatial.py`; PostGIS agrees (integration, query 12 on live data) |

## 5. Missing and unknown source attributes

* Zone codes not in the table (`III A`, `ii`, `Zone II`, `IIIa`, `III/A`,
  `qual I`, `QUAL  I`, `QUANT C`, `IV`) are stored as `UNKNOWN` with the raw
  value; a missing code is `UNKNOWN` with `zone_code_source` NULL. The live
  data has none of these.
* Extraction rates are not published, so every row has `extraction_rate`
  NULL and status `not_published`. Text such as "Foerdermenge ca. 500 m3/d" in
  a remark is ignored. If a rate property is ever mapped: numeric ->
  `published`, empty -> `missing`, `1.234,5` / `ca. 500` / negative ->
  `unparseable` (NULL + warning). A database CHECK enforces that a rate
  exists only with status `published`.
* Zone numbers: `5.53210009E8`, `553210009.0` and `553210009` give the same
  key; fractions, text, NaN/Infinity and values above 2^53 (which a double
  cannot hold exactly) reject the record.
* Unparseable dates become NULL with a warning; raw values remain in
  `source_attributes`.
* Source properties that are not mapped are reported as schema drift
  (`schema.unmapped_properties`: `Gebiet`, `Gebiet_Nr`, `Gebietsart`,
  `Herkunft`, `Scale`, `Symbolik`, `Url`) and kept in `source_attributes`.

## 6. Retrieval failures

| Failure | Behaviour | Test |
|---|---|---|
| Connect/read timeout, connection error | bounded retries (exponential backoff with jitter), then `HttpRequestError` | `test_http.py` |
| 429/502/503/504, plain 500 | retried, `Retry-After` honoured within the cap | `test_http.py` |
| OWS exception (any status, including 200) | `WfsServiceException` with code/locator, not retried | `test_http.py`, `test_client.py` |
| HTML instead of XML (bot protection) | `UnexpectedContentError` with the page title, not retried (observed live) | `test_capabilities.py`, `test_client.py` |
| Malformed XML, wrong root, XXE entity | rejected; entities never resolved | `test_capabilities.py` |
| Oversized response | `ResponseTooLargeError` | `test_http.py` |
| Layer not advertised, no GML format, unsupported version | `CapabilitiesError` listing what is available (observed live for `th_wsg_zonen`) | `test_capabilities.py` |
| Live schema differs from mapping | `SchemaMismatchError` before any GetFeature (observed live with the assumed names) | `test_pipeline.py` |
| Missing page, numberReturned mismatch, numberMatched changing, repeated features | `IncompleteRetrievalError`, nothing loaded | `test_client.py` |
| Service without paging capped below numberMatched, with or without a `hits` count | `IncompleteRetrievalError` instead of a silently truncated load | `test_client.py` |
| Feature limits | `RetrievalLimitExceeded` (checked from `hits` before downloading) | `test_client.py` |
| Valid empty collection | `succeeded_empty`, exit 0 (distinct from failure) | `test_pipeline.py` |
| Database unreachable | connection attempt times out after 10 s | `test_staging_guard.py` |
| Database error in a later batch | whole load rolled back, run logged as failed | `test_loader.py`, integration |

## 7. Coverage limitations

* The layer covers all of Thuringia (WGS 84 bounding box 9.80-12.89 E,
  50.14-51.76 N; 3 090 zones). Zones of cross-border catchments are included
  only as far as TLUBN publishes them (e.g. remark "Festsetzung in Hessen").
* 744 zones are planned, not designated; 625 zone I / QUAL I geometries are
  map symbols. Screening results must take `legal_status_source` and
  `validation_warnings` into account.
* Positional accuracy: digitised at 1:25 000 on the DTK25 (dataset lineage);
  parcel results near a boundary should be reviewed, and `boundary_contact`
  is reported separately.
* The plausibility screen is a lon/lat box, not the Thuringia boundary.

## 8. Acceptance criteria

From the task PDF:

| Criterion | Status | Evidence |
|---|---|---|
| Official zone classifications are traceable to source attributes and not inferred from a fragile substring rule | **Verified** | exact table lookup over the published `Zone` codes listed in the GeoMIS dataset description; `test_mapping.py` (14 codes mapped, 9 variants stay UNKNOWN); validation query 4 on the live data: one row per code, `zone_code_source = source_attributes ->> 'Zone'` for all 3 090 rows |
| Geospatial and duplicate-load validation succeed on representative fixtures | **Verified** | synthetic edge cases and a recorded excerpt of 11 real features; PostGIS integration tests; live data: 3 090 valid MULTIPOLYGONs in SRID 25832, second load 0 inserted / 0 updated / 3 090 unchanged |

Additional checklist:

| Item | Status |
|---|---|
| WFS capabilities, layer names, permissions and actual schema verified | Verified for the TLUBN service (section 1); the assignment's endpoint is documented as unusable, not bypassed |
| Field mappings verified against the actual schema | Verified against DescribeFeatureType and all 3 090 features |
| Missing/unpublished extraction rates stay unknown | Verified (tests + DB CHECK + query 5: 0 rows) |
| Geometries validated and transformed to EPSG:25832 | Verified |
| Multipart polygons and parcel containment/intersection tested | Verified (89 live multipart zones; query 12 on live data) |
| Idempotent persistence keyed by source zone ID and state code | Verified on PostGIS 3.5.2 / PostgreSQL 17 with the live data |
| Writes guarded to the staging destination | Verified (`test_staging_guard.py`, `test_staging_sql.py`, least-privilege role) |
| Docker, pinned dependencies, reproducible instructions | Verified: images built from exact pins, compose flow and in-container test run (section 9) |
| No secrets or production writes | No credentials in any tracked file (all tracked files scanned; the pgAdmin login of the base compose file moved to `.env`); `.env` is git-ignored, `.env.example` holds placeholders only; no production target was ever configured |

## 9. Verification log

Local machine: Windows 10, Python 3.12.10, GEOS 3.14.1, PROJ 9.8.1. Database
for integration tests: the compose `postgis-staging` service (PostgreSQL 17,
PostGIS 3.5.2), database `iris_staging_test`.

| Command | Result |
|---|---|
| `ruff check app/water_protection app/database/staging alembic_staging/env.py tests` | All checks passed |
| `ruff format --check ...` (same paths) | 50 files already formatted |
| `mypy app/water_protection app/database/staging alembic_staging/env.py` | no issues in 29 source files |
| `python -m pytest tests/water_protection` (no database) | 251 passed, integration module skipped with reason |
| same with `IRIS_TEST_DATABASE_URL` set | 260 passed (251 + 9 PostGIS integration), 94 % line coverage of the module |
| `alembic -c alembic_staging.ini downgrade -1`, `upgrade head`, `current`, `check` | revision `f6c54b9016bf` reversible; head; no new upgrade operations (models = database) |
| `python -m app.water_protection validate --export zones.gpkg` (live) | succeeded; 3 090 accepted; GeoPackage with 3 090 valid features |

Docker (Docker Desktop 29.9.2, Compose 5.6.1), starting from an empty volume:

| Command | Result |
|---|---|
| `docker compose config` / `--profile wsg config` | valid, no warnings; without the profile only `postgres` and `pgadmin` |
| `docker compose --profile wsg build wsg-ingest wsg-tests` | images `iris-wsg-th:local` and `iris-wsg-th:test` built from the pinned requirements |
| `docker run --entrypoint id iris-wsg-th:local` | `uid=10001(iris)`: runs as non-root |
| `docker compose --profile wsg up -d --wait postgis-staging` | init script created role, test database, PostGIS and schema; healthy |
| `wsg-ingest init-db` / `check-db` | both revisions applied as `iris_staging_writer`; PostGIS 3.5.2, 0 rows |
| `wsg-ingest inspect` | exit 0; mapping matches the live schema |
| `wsg-ingest inspect` with the assignment's endpoint | exit 1, CAPTCHA page detected |
| `wsg-ingest load` (live, twice) | exit 0; 3 090 inserted in 7 batches, then 0 inserted / 0 updated / 3 090 unchanged ([example_report.json](example_report.json)) |
| `docker compose --profile wsg run --rm wsg-tests` | 260 passed, 0 skipped |
| `psql -f docs/wsg_th/validation_queries.sql` (read-only session) | all 12 queries as expected ([example_validation_output.txt](example_validation_output.txt)) |

Defects found by running against real PostGIS, Docker and the live service,
all fixed and covered by tests:

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
* The first mapping's property names were assumptions; the live schema
  showed all of them wrong and 9 of 14 zone codes missing. The mapping was
  rebuilt from the live schema and data.
* Zone numbers arrive as `xsd:double` in exponent notation; they are now
  normalised to whole-number text with a precision guard.
* Database connections had no timeout; with `localhost` resolving to IPv6
  first while compose publishes on 127.0.0.1, a connection could hang
  indefinitely. Connections now time out after 10 s, and `.env.example`
  uses 127.0.0.1.
* A service without paging that caps its answer was only detected when a
  `hits` count was available; the single response's `numberMatched` is now
  checked as well.
* The base `docker-compose.yml` contained a hard-coded pgAdmin login and the
  obsolete `version` key (Compose printed a warning on every command). The
  login now comes from `.env` and the key was removed.

Data © GDI-Th, licensed under CC BY 4.0.
