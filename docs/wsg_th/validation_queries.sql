-- Validation queries for iris_staging.water_protection_zones (IRIS-WSG-TH).
-- All queries are read-only. Run with:
--   psql "$IRIS_STAGING_PSQL_URL" -f docs/wsg_th/validation_queries.sql

\echo '1. Rows per zone category and validation status'
SELECT state_code, zone_category, validation_status, count(*) AS zones
FROM iris_staging.water_protection_zones
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3;

\echo '2. Natural key is unique (expect 0 rows)'
SELECT state_code, source_zone_id, count(*)
FROM iris_staging.water_protection_zones
GROUP BY 1, 2
HAVING count(*) > 1;

\echo '3. Geometry: type, SRID and validity (expect one row: MULTIPOLYGON, 25832, all valid)'
SELECT GeometryType(geom) AS geometry_type,
       ST_SRID(geom) AS srid,
       count(*) AS zones,
       count(*) FILTER (WHERE ST_IsValid(geom)) AS valid,
       count(*) FILTER (WHERE ST_NumGeometries(geom) > 1) AS multipart
FROM iris_staging.water_protection_zones
GROUP BY 1, 2;

\echo '4. Every classification is traceable to the published code (expect one row per code)'
SELECT zone_code_source, zone_category, zone_legal_description,
       bool_and(zone_code_source = source_attributes ->> 'Zone') AS matches_raw_attribute,
       bool_and(zone_classification_basis IS NOT NULL) AS has_basis,
       count(*) AS zones
FROM iris_staging.water_protection_zones
GROUP BY 1, 2, 3
ORDER BY 2;

\echo '5. Extraction rates are only present when published (expect 0 rows)'
SELECT source_zone_id, extraction_rate, extraction_rate_status
FROM iris_staging.water_protection_zones
WHERE (extraction_rate IS NOT NULL) <> (extraction_rate_status = 'published');

\echo '6. Legal status as published (planned zones are not yet in force)'
SELECT legal_status_source, count(*) AS zones
FROM iris_staging.water_protection_zones
GROUP BY 1
ORDER BY 2 DESC;

\echo '7. Validation warnings (geometry_is_map_symbol: a 20 m symbol, not the legal boundary)'
SELECT warning, count(*) AS zones
FROM iris_staging.water_protection_zones, unnest(validation_warnings) AS warning
GROUP BY 1
ORDER BY 2 DESC;

\echo '8. Repaired geometries with their evidence'
SELECT source_zone_id,
       geometry_repair_evidence ->> 'invalid_reason' AS invalid_reason,
       geometry_repair_evidence ->> 'method' AS method,
       round((geometry_repair_evidence ->> 'area_change_ratio')::numeric, 6) AS area_change_ratio
FROM iris_staging.water_protection_zones
WHERE geometry_repaired;

\echo '9. CRS provenance and data currency'
SELECT source_crs, crs_transformation, source_modified_date, count(*)
FROM iris_staging.water_protection_zones
GROUP BY 1, 2, 3;

\echo '10. Quarantined features by reason'
SELECT reason_code, count(*) AS features, max(occurrences) AS max_runs_seen
FROM iris_staging.water_protection_zones_quarantine
GROUP BY 1
ORDER BY 1;

\echo '11. Recent ingestion runs (including failed and empty ones)'
SELECT run_id, status, features_retrieved, features_accepted, features_rejected,
       rows_inserted, rows_updated, rows_unchanged, error_type, finished_at
FROM iris_staging.water_protection_zone_ingestion_runs
ORDER BY finished_at DESC
LIMIT 10;

\echo '12. Parcel screening example: a 60 m parcel on the zone II / III boundary of WSG Neustaedt'
WITH parcel AS (
    SELECT ST_GeomFromText(
        'POLYGON((576964 5649631, 577024 5649631, 577024 5649691, 576964 5649691, 576964 5649631))',
        25832) AS geom
)
SELECT z.source_zone_id,
       z.zone_category,
       z.protection_area_name,
       CASE
           WHEN ST_Covers(z.geom, p.geom) THEN 'within'
           WHEN ST_Relate(p.geom, z.geom, 'T********') THEN 'partial_overlap'
           ELSE 'boundary_contact'
       END AS relation,
       round(ST_Area(ST_Intersection(p.geom, z.geom))::numeric, 1) AS overlap_m2
FROM iris_staging.water_protection_zones z
CROSS JOIN parcel p
WHERE ST_Intersects(z.geom, p.geom)
ORDER BY z.zone_category;
