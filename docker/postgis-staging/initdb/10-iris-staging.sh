#!/usr/bin/env bash
# Runs once, when the staging PostGIS volume is first initialised.
#
# Creates a least-privilege login role that owns only the iris_staging
# schema, plus a separate test database for integration tests. iris_core
# does not exist in this instance at all.
set -euo pipefail

: "${IRIS_STAGING_WRITER_PASSWORD:?IRIS_STAGING_WRITER_PASSWORD must be set}"
TEST_DB="${IRIS_STAGING_TEST_DB:-iris_staging_test}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v writer_password="$IRIS_STAGING_WRITER_PASSWORD" \
  -v test_db="$TEST_DB" <<'SQL'
CREATE ROLE iris_staging_writer LOGIN PASSWORD :'writer_password'
  NOSUPERUSER NOCREATEDB NOCREATEROLE;
SELECT format('CREATE DATABASE %I', :'test_db') \gexec
SQL

for db in "$POSTGRES_DB" "$TEST_DB"; do
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$db" <<'SQL'
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE SCHEMA IF NOT EXISTS iris_staging AUTHORIZATION iris_staging_writer;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
SQL
done
