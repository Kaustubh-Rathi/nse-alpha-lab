# syntax=docker/dockerfile:1
#
# Multi-stage image for the NSE rotation research lab.
#
# Storage is embedded DuckDB + parquet and backtests never touch the network, so
# there is deliberately NO Postgres and NO Redis here (measured: zero imports of
# psycopg / sqlalchemy / redis / celery in src/).
#
# Build from the REPOSITORY ROOT (the build context must include pyproject.toml
# and uv.lock):
#
#   docker build -f docker/Dockerfile -t nse-alpha-lab .                   # published image
#   docker build -f docker/Dockerfile --target test -t nse-alpha-lab:test .
#   docker build -f docker/Dockerfile --build-arg UV_EXTRA=pdf -t nse-alpha-lab:pdf .
#
# The LAST stage (`runtime`) is the lean published image: no pytest, no build
# tooling. The `test` stage is a separate image used only for the test lane.
#
# Pin the runtime interpreter with:
#   --build-arg PYTHON_BASE=python:3.12-slim

ARG PYTHON_BASE=python:3.14-slim

# ---------------------------------------------------------------------------
# Stage 1 - builder: resolve and install the locked runtime environment
# ---------------------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS builder

# Byte-compile at install time and copy (never hard-link) files into the venv so
# the venv survives the COPY into the runtime stage.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_PROGRESS=1 \
    UV_PYTHON_DOWNLOADS=never

# Optional extra from [project.optional-dependencies]: "pdf" adds pypdf +
# pymupdf (AGPL-3.0), "lab" adds streamlit for the UI service, "fetch" adds the
# Kaggle / Hugging Face clients behind `python -m nse_pipeline fetch`.
ARG UV_EXTRA=

WORKDIR /app

# Dependencies alone. This layer is cached until pyproject.toml / uv.lock change.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project --no-dev ${UV_EXTRA:+--extra $UV_EXTRA}

# Source, research artefacts and docs. There is no importable project to install
# ([tool.uv] package = false), so nothing else needs to be copied into the venv.
COPY src/ ./src/
COPY scripts/ ./scripts/
COPY research/ ./research/
COPY docs/ ./docs/
COPY examples/ ./examples/
COPY apps/ ./apps/

# Internal-only research artefacts (*.internal.csv: 130 columns including the
# internal gate vocabulary, versus 124 public columns) must never be published.
# .dockerignore keeps them out of the build context; assert that the exclusion
# actually held, so a future .dockerignore edit fails the build loudly instead
# of silently baking internal data into a public image.
RUN if find research -name '*.internal.csv' -print -quit | grep -q .; then \
        echo "ERROR: internal-only CSV reached the build context; see .dockerignore" >&2; \
        find research -name '*.internal.csv' >&2; \
        exit 1; \
    fi

# ---------------------------------------------------------------------------
# Stage 2 - test: builder + the dev dependency group. NOT the published image.
# ---------------------------------------------------------------------------
FROM builder AS test

# Redeclared so an explicit --build-arg UV_EXTRA=... also reaches this stage
# (ARG values do not inherit across stages). Compose leaves it empty here.
ARG UV_EXTRA=

# CI's dependency set: base + [dependency-groups] dev (pytest). The pdf-only
# tests self-skip when pypdf is absent, exactly as they do in CI, so the AGPL
# pdf extra is deliberately not installed here.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project --dev ${UV_EXTRA:+--extra $UV_EXTRA}

# Put the project venv first so `python` / `pytest` resolve to the locked
# environment (the uv base image has its own interpreter on PATH).
ENV VIRTUAL_ENV=/app/.venv \
    PYTHONPATH=/app/src \
    NSE_DATA_DIR=/app/data \
    PYTHONUNBUFFERED=1 \
    HOME=/app \
    PATH=/app/.venv/bin:$PATH

# tests/ is public and small, so the test image carries it: the suite runs with
# no bind mount. Nothing from this stage is copied into the runtime image.
COPY tests/ ./tests/

RUN mkdir -p /app/data

# Default command of the test image: the CI's pytest invocation.
CMD ["python", "-m", "pytest", "-q"]

# ---------------------------------------------------------------------------
# Stage 3 - runtime: minimal published image, no build tooling, non-root uid 1000
# ---------------------------------------------------------------------------
FROM ${PYTHON_BASE} AS runtime

ARG APP_UID=1000

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    NSE_DATA_DIR=/app/data \
    VIRTUAL_ENV=/app/.venv \
    PATH=/app/.venv/bin:$PATH \
    HOME=/app \
    MPLBACKEND=Agg \
    # Streamlit: container-safe defaults (no browser, headless, no CORS prompt).
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_SERVER_ADDRESS=0.0.0.0 \
    STREAMLIT_SERVER_PORT=8501 \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

# Non-root account. Debian/Ubuntu reserve all system UIDs/GIDs below 1000 and
# give `nobody` 65534, so UID/GID 1000 is free in every python:slim variant.
# Created in its own layer so the account is independent of the venv COPY and
# `docker run -u <uid>:<gid>` still works.
RUN groupadd --gid "${APP_UID}" app \
 && useradd --uid "${APP_UID}" --gid app --create-home --shell /usr/sbin/nologin app

WORKDIR /app

# The virtualenv is the only dependency payload; copy it whole (uv venvs are
# relocatable when UV_LINK_MODE=copy was used). pytest is not in this stage.
COPY --from=builder /app/.venv /app/.venv

COPY --from=builder --chown=app:app /app/src /app/src
COPY --from=builder --chown=app:app /app/scripts /app/scripts
COPY --from=builder --chown=app:app /app/research /app/research
COPY --from=builder --chown=app:app /app/docs /app/docs
COPY --from=builder --chown=app:app /app/examples /app/examples
COPY --from=builder --chown=app:app /app/apps /app/apps

# Data volume. The bind mount at compose level replaces this directory; the
# mkdir + chown matters for anonymous volumes and for `docker run` without a mount.
RUN mkdir -p /app/data && chown -R app:app /app

USER app:app

VOLUME ["/app/data"]

# Healthcheck: prove the interpreter, PYTHONPATH and CLI entry point are wired.
# The package ships __main__.py and cli.py's argparse `--help` exits 0 (verified
# from a source checkout), so this exercises the real CLI instead of a bare
# import. The console script `nse-alpha` is intentionally not installed
# ([tool.uv] package = false), hence the `python -m` form.
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD ["python", "-m", "nse_pipeline", "--help"]

# Default: the research UI. compose.yaml overrides `command:` per service.
EXPOSE 8501
CMD ["python", "-m", "streamlit", "run", "/app/apps/lab/app.py"]
