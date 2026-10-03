# The ALM agents' images. One file, three targets:
#
#   api         the API and console only (ALM_WORKER_CONCURRENCY=0). No browser:
#               about half the size, and nothing in it can drive a web page.
#   worker      the run worker (python -m alm_agents.worker), with Chromium for
#               the evidence screenshots of JTS profile pages.
#   all-in-one  the API with workers inside it and Chromium - one service, as
#               the GCP Terraform deploys today. The default target.
#
#   docker build --build-arg CA_BUNDLE=certs/corporate-ca.pem --target api .
#
# The corporate CA is baked in rather than mounted: TLS verification is on by
# default in the cloud configuration, and a container that cannot verify the
# intranet certificates should fail to build, not fail open at runtime.
#
# Every target runs as a non-root user and works with a read-only root
# filesystem as long as /tmp is writable (docker run --read-only --tmpfs /tmp;
# readOnlyRootFilesystem in Kubernetes and ECS). Traces and screenshots go to
# /tmp; durable state lives in Postgres.
#
# The base image is pinned by digest: a tag can move under a build, a digest
# cannot. Refresh it monthly (and on a base-image CVE), then re-run the scan.

FROM python:3.13-slim@sha256:bb2988715db2cf7ace7b53f38f3cffbef7c7046a656bee66245eb0ed386e2e81 AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src \
    HOME=/tmp \
    ALM_CA_BUNDLE=/etc/ssl/certs/corporate-ca.pem \
    ALM_TRACE_DIR=/tmp \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright

WORKDIR /app

# --- corporate trust ---------------------------------------------------------
# Supply the bundle at build time; the build fails without it, by design.
ARG CA_BUNDLE=certs/corporate-ca.pem
COPY ${CA_BUNDLE} /usr/local/share/ca-certificates/corporate-ca.crt

RUN apt-get update \
 && apt-get upgrade -y \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && update-ca-certificates \
 && test -s /etc/ssl/certs/corporate-ca.pem \
 && rm -rf /var/lib/apt/lists/*
# update-ca-certificates links /etc/ssl/certs/corporate-ca.pem to the bundle,
# which is the file ALM_CA_BUNDLE names; the test fails the build if it did not.

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
 && rm -rf /tmp/wheels \
 && python -m pip uninstall -y pip \
 && rm -rf "$(python -c 'import ensurepip, os; print(os.path.dirname(ensurepip.__file__))')"
# pip is gone from the running image: nothing in it can install a package, and
# pip's own vendored libraries (which the app never imports) stop showing up
# in the vulnerability scan.

# --- application -------------------------------------------------------------
COPY src/ /app/src/

# Non-root: the container reaches EWM, JTS and a database, so it should not
# also own its own filesystem. Nothing under /app is writable by it.
RUN useradd --system --no-create-home --uid 10001 alm

# Cloud Run injects PORT and ignores EXPOSE, but declaring it keeps a local
# `docker run` honest.
ENV PORT=8080


# ============================================================== api
FROM base AS api

# An API without a browser must not run workers: a run's evidence step needs
# Chromium. Runs are left to the worker service.
ENV ALM_WORKER_CONCURRENCY=0
USER alm
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/healthz" || exit 1
# Shell form so ${PORT} expands. --proxy-headers because a load balancer (and
# IAP) sits in front, and console links are built from the forwarded host.
CMD exec uvicorn alm_api.main:app --host 0.0.0.0 --port ${PORT} \
    --proxy-headers --no-access-log


# ============================================================== worker
FROM base AS worker

RUN playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*
USER alm
# No port and no health endpoint: a worker that dies stops renewing its job
# leases, and another worker takes its runs over.
CMD ["python", "-m", "alm_agents.worker"]


# ============================================================== all-in-one
FROM worker AS all-in-one

USER alm
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/healthz" || exit 1
CMD exec uvicorn alm_api.main:app --host 0.0.0.0 --port ${PORT} \
    --proxy-headers --no-access-log
