# syntax=docker/dockerfile:1.7

# ---------- build stage: compile wheels once, keep the runtime image clean ----
FROM python:3.15-slim AS builder

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY backend/requirements.txt .
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install -r requirements.txt

# ---------- runtime stage ----------------------------------------------------
FROM python:3.15-slim AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Unprivileged user; the image carries no compiler and no package index cache.
RUN groupadd --gid 10001 edid \
 && useradd --uid 10001 --gid edid --create-home --shell /usr/sbin/nologin edid \
 && apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

WORKDIR /srv
COPY --chown=edid:edid backend/app ./backend/app
COPY --chown=edid:edid backend/pyproject.toml ./backend/pyproject.toml
COPY --chown=edid:edid frontend ./frontend

WORKDIR /srv/backend
USER edid
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/api/v1/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
