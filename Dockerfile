# Orchestrator + API image, built for Cloud Run.
#
# The corporate CA is baked in rather than mounted: TLS verification is on by
# default in the cloud configuration, and a container that cannot verify the
# intranet certificates should fail to build, not fail open at runtime.
#
# Chromium is installed because the evidence step screenshots JTS profile pages
# from inside this container. That is the one heavy dependency here; if evidence
# capture is ever moved to the Windows worker, drop the two playwright layers
# and the image halves in size.

FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src \
    ALM_CA_BUNDLE=/etc/ssl/certs/corporate-ca.pem \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright

WORKDIR /app

# --- corporate trust ---------------------------------------------------------
# Supply the bundle at build time:
#   docker build --build-arg CA_BUNDLE=certs/corporate-ca.pem .
# The build fails without it, which is the intended behaviour.
ARG CA_BUNDLE=certs/corporate-ca.pem
COPY ${CA_BUNDLE} /usr/local/share/ca-certificates/corporate-ca.crt

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && update-ca-certificates \
 && cp /usr/local/share/ca-certificates/corporate-ca.crt /etc/ssl/certs/corporate-ca.pem \
 && rm -rf /var/lib/apt/lists/*

# requests and httpx honour these; so does anything else using the system store.
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/corporate-ca.pem \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

# --- dependencies ------------------------------------------------------------
# Copied first so a source change does not invalidate the dependency layer.
COPY requirements.txt requirements-cloud.txt ./
# wheels/ is the offline fallback for an air-gapped build; it is optional, and
# the pattern below tolerates its absence.
COPY wheels* /tmp/wheels/
RUN if [ -n "$(ls -A /tmp/wheels 2>/dev/null)" ]; then \
        pip install --no-index --find-links /tmp/wheels -r requirements-cloud.txt; \
    else \
        pip install -r requirements-cloud.txt; \
    fi \
 && rm -rf /tmp/wheels

RUN playwright install --with-deps chromium

# --- application -------------------------------------------------------------
COPY src/ /app/src/

# Non-root: the container has network access to EWM, JTS and a database, so it
# should not also have root inside its own filesystem.
RUN useradd --system --create-home --uid 10001 alm \
 && mkdir -p /app/out /opt/playwright \
 && chown -R alm:alm /app /opt/playwright
USER alm

# Cloud Run injects PORT and ignores EXPOSE, but declaring it keeps a local
# `docker run` honest.
ENV PORT=8080
EXPOSE 8080

# Cloud Run runs its own startup and liveness probes (see infra/run.tf); this
# is for local runs and any other runtime that honours it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/healthz" || exit 1

# Shell form so ${PORT} expands. --proxy-headers because the internal load
# balancer and IAP sit in front, and approval links must be built from the
# forwarded host rather than the container's own.
CMD exec uvicorn alm_api.main:app --host 0.0.0.0 --port ${PORT} \
    --proxy-headers --no-access-log
