# syntax=docker/dockerfile:1.7
# ============================================================
# SHMAX production image — multi-stage
#
# Stage 1 (frontend-builder): Node 22 builds the React UI and
# emits static assets into /app/frontend/dist.
# Stage 2 (backend-builder): Python 3.12 installs the SHMAX
# backend with all deps.
# Stage 3 (runtime): python:3.12-slim + non-root user.
# Static frontend assets are copied into /app/static and
# served by FastAPI.
#
# Application is disposable. User data is persistent (mounted
# into /data at runtime via compose.yaml).
# ============================================================

ARG PY_VERSION=3.12
ARG NODE_VERSION=22
ARG SHMAX_VERSION=0.1.2
ARG SHMAX_UID=8787
ARG SHMAX_GID=8787

# --- Frontend build ---------------------------------------------
FROM --platform=$BUILDPLATFORM node:${NODE_VERSION}-bookworm-slim AS frontend-builder
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install --no-audit --no-fund --prefer-offline
COPY frontend/ ./
RUN npm run build

# --- Backend build ----------------------------------------------
FROM python:${PY_VERSION}-slim AS backend-builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build

# Build-stage native toolchain. armv7 in particular may need to
# compile Python packages with C extensions; install the toolchain
# here so the slim runtime stage stays slim.
#
# Pillow source-build needs the dev headers of every image codec
# it can link against. Without them the build fails with
# ``The headers or library files could not be found for zlib``
# (or ``libjpeg``, ``libpng`` …). On armv7 no upstream wheel
# exists for Pillow, so the source path is mandatory. We install
# the canonical Pillow prerequisite set: zlib, libjpeg,
# libpng, libtiff, libwebp, freetype, lcms2. Each is named on
# https://pillow.readthedocs.io/en/stable/installation/building-from-source.html
# and the audit found the bare set (just zlib) trips on the
# very first dependency Pillow reaches for. Adding the full set
# is still a fraction of the ``python:slim`` base image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        pkg-config \
        libssl-dev \
        libffi-dev \
        rustc \
        cargo \
        zlib1g-dev \
        libjpeg62-turbo-dev \
        libpng-dev \
        libtiff-dev \
        libwebp-dev \
        libfreetype6-dev \
        liblcms2-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps from the lock file first (faster cache),
# then copy source for the editable install.
COPY backend/requirements.lock ./requirements.lock
RUN pip install --prefix=/install --no-cache-dir -r requirements.lock

COPY backend/pyproject.toml ./pyproject.toml
COPY backend/shmax ./shmax
COPY backend/alembic ./alembic
COPY backend/alembic.ini ./alembic.ini
RUN pip install --prefix=/install --no-cache-dir --no-deps .

# --- Runtime ----------------------------------------------------
# ----------------------------------------------------------------------
# Pinned Xray-core (surgical acceptance). NO "latest" at runtime:
# the release is pinned and the artifact SHA256 is verified at build.
# linux-64 (amd64) AND linux-arm32-v7a (ODROID-XU4) are covered via
# TARGETARCH mapping.
# ----------------------------------------------------------------------
FROM --platform=$BUILDPLATFORM debian:bookworm-slim AS xray-downloader
ARG TARGETARCH=amd64
ENV XRAY_VERSION=v25.3.6
# Verified SHA256 of the release zips (computed from the official
# XTLS/Xray-core v25.3.6 release artifacts).
RUN set -eux; \
    case "$TARGETARCH" in \
      amd64)  artifact="Xray-linux-64.zip"; \
              sha256="82d4be3a5ed8bd2621df9c9913c3a2761b86a42ae8485da836f7447ff2ec3d4d" ;; \
      arm)    artifact="Xray-linux-arm32-v7a.zip"; \
              sha256="f184c8344941a4ac0bc198174f3eba08cfebfdc6588a03887ca1bdb53100e6f7" ;; \
      arm64)  artifact="Xray-linux-arm64-v8a.zip"; \
              sha256="1595f446b3d3a2bfe4a737e3cdb4c189c8c4e271322a7a2a0f5ea735b9511e80" ;; \
      *) echo "unsupported arch: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    apt-get update && apt-get install -y --no-install-recommends curl unzip ca-certificates && \
    curl -fsSL --retry 3 -o /tmp/xray.zip \
      "https://github.com/XTLS/Xray-core/releases/download/${XRAY_VERSION}/${artifact}" && \
    echo "${sha256}  /tmp/xray.zip" | sha256sum -c - && \
    unzip -o /tmp/xray.zip xray -d /out && \
    chmod 0755 /out/xray && \
    rm -rf /var/lib/apt/lists/*

FROM python:${PY_VERSION}-slim AS runtime

ARG SHMAX_VERSION
ARG SHMAX_UID
ARG SHMAX_GID
ENV SHMAX_VERSION=${SHMAX_VERSION} \
    SHMAX_HOST=0.0.0.0 \
    SHMAX_PORT=8787 \
    SHMAX_DATA_DIR=/data \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --system --gid ${SHMAX_GID} shmax \
    && useradd --system --uid ${SHMAX_UID} --gid shmax --no-create-home --shell /usr/sbin/nologin shmax

# Runtime libraries that the compiled Pillow wheel (and other
# C-extension wheels we build in the builder stage) dynamically
# link against. The wheel cannot start without these on armv7
# because no manylinux wheel ships for that architecture. Each
# ``-dev`` package from the builder stage is paired here with
# the runtime library that ships the actual ``.so`` files.
#
# IMPORTANT: Debian 13 (trixie) renamed a small number of runtimes
# as part of the time_t (t64) transition. The pre-trixie names
# are flagged by ``apt-get`` as "no installation candidate".
# The renamed names (``*t64``) are the actual current package
# names on trixie. ARMv7 remains experimental; physical ODROID-XU4
# acceptance has not been completed. Runtime package-name mapping:
#
#     package                 pre-trixie -> trixie actual
#     libpng16-16             libpng16-16t64
#     libjpeg62-turbo         unchanged
#     libtiff6                unchanged
#     libwebp7                unchanged (NOT a t64 rename;
#                                       ``libwebp-dev`` still
#                                       depends on ``libwebp7``)
#     libfreetype6            unchanged
#     liblcms2-2              unchanged
#     zlib1g                  unchanged
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libjpeg62-turbo \
        libpng16-16t64 \
        libtiff6 \
        libwebp7 \
        libfreetype6 \
        liblcms2-2 \
        zlib1g \
    && rm -rf /var/lib/apt/lists/*

# Install runtime dependencies from the builder prefix.
COPY --from=backend-builder /install /usr/local

# Copy the SHMAX package.
WORKDIR /app
COPY --chown=root:root backend/pyproject.toml ./pyproject.toml
COPY --chown=root:root backend/shmax ./shmax
COPY --chown=root:root backend/alembic ./alembic
COPY --chown=root:root backend/alembic.ini ./alembic.ini

# Static frontend assets (Stage 1B minimal landing; the React
# UI is wired in via FastAPI static files in Stage 2).
COPY --chown=root:root --from=frontend-builder /build/frontend/dist /app/frontend/dist

# Pinned Xray-core binary (verified checksum above). Runs as the
# unprivileged shmax user; its SOCKS listens on 127.0.0.1 only.
COPY --from=xray-downloader /out/xray /usr/local/bin/xray

# Ephemeral Xray runtime config dir (0600 file, never on LAN).
RUN mkdir -p /run/shmax/xray && chown shmax:shmax /run/shmax/xray

# Persistent mount point. compose.yaml bind-mounts /var/lib/shmax.
RUN mkdir -p /data && chown -R shmax:shmax /data
# Release extraction may use umask 077. COPY preserves nested source modes;
# normalize application code before dropping privileges (never persistent data).
RUN find /app -type d -exec chmod 0755 {} + \
    && find /app -type f -exec chmod 0644 {} +
USER shmax

EXPOSE 8787

# /health is the only signal Docker needs. Telegram / MAX being
# NOT_CONFIGURED must not flip Docker into an unhealthy restart
# loop.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=3).status == 200 else 1)"

CMD ["sh", "-c", "exec uvicorn shmax.main:app --host ${SHMAX_HOST} --port ${SHMAX_PORT} --log-level ${SHMAX_LOG_LEVEL:-info}"]
