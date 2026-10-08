FROM python:3.12-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build
COPY backend/pyproject.toml ./
COPY backend/app ./app
COPY tools/__init__.py ./tools/__init__.py
COPY tools/valuation_inventory ./tools/valuation_inventory

# Keep pip's download cache in BuildKit's cache mount while keeping it out of
# the final image.  The runtime image only receives installed dependencies.
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --prefix=/install --no-compile .

FROM python:3.12-slim-bookworm

ENV APP_ENV=production \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl --fail --silent --show-error --location \
       https://www.postgresql.org/media/keys/ACCC4CF8.asc \
       --output /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    && printf '%s\n' 'deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main' \
       > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install --yes --no-install-recommends postgresql-client-16 \
    && pg_dump --version \
    && pg_restore --version \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 fundapp \
    && useradd --system --uid 10001 --gid fundapp --home-dir /nonexistent \
       --shell /usr/sbin/nologin fundapp

WORKDIR /app/backend
COPY --from=builder /install /usr/local
COPY --chown=fundapp:fundapp backend/pyproject.toml ./
COPY --chown=fundapp:fundapp backend/app ./app
COPY --chown=fundapp:fundapp tools/__init__.py ./tools/__init__.py
COPY --chown=fundapp:fundapp tools/valuation_inventory ./tools/valuation_inventory
COPY --chown=fundapp:fundapp backend/alembic.ini ./
COPY --chown=fundapp:fundapp backend/alembic ./alembic

RUN mkdir --parents \
      /var/lib/fund-dashboard/tmp/uploads \
      /var/lib/fund-dashboard/source-files \
      /var/lib/fund-dashboard/backups \
    && chown --recursive fundapp:fundapp /var/lib/fund-dashboard

USER fundapp:fundapp
