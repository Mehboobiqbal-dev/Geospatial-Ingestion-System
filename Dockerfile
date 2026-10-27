# Image for the Thuringia water protection zone ingestion (IRIS-WSG-TH).
#
#   docker build --target runtime -t iris-wsg-th .     # CLI image
#   docker build --target test -t iris-wsg-th:test .   # adds tests and tooling
#
# Only the ingestion package and its staging database layer are copied; the
# rest of the NRW platform (Streamlit UI, scrapers, iris_core models) is not
# part of this image.

FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Wheels for shapely, pyproj, pyogrio and psycopg bundle GEOS, PROJ, GDAL and
# libpq, so no system packages or compilers are needed.
COPY requirements/water-protection.txt requirements/
RUN pip install --root-user-action=ignore -r requirements/water-protection.txt

COPY app/__init__.py app/
COPY app/database/__init__.py app/database/
COPY app/database/staging app/database/staging
COPY app/water_protection app/water_protection
COPY alembic_staging alembic_staging
COPY alembic_staging.ini ./

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin iris
USER iris

ENTRYPOINT ["python", "-m", "app.water_protection"]
CMD ["--help"]


FROM runtime AS test

USER root
COPY requirements/water-protection-dev.txt requirements/
RUN pip install --root-user-action=ignore -r requirements/water-protection-dev.txt
COPY pyproject.toml ./
COPY tests tests
USER iris

ENTRYPOINT ["python", "-m", "pytest", "-p", "no:cacheprovider"]
CMD ["tests/water_protection"]
