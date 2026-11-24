# Build stage: install the package into a virtualenv.
FROM python:3.13-alpine AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m venv /venv \
 && /venv/bin/pip install --no-cache-dir . \
 && /venv/bin/pip uninstall -y pip \
 && find /venv -name '__pycache__' -prune -exec rm -rf {} +

# Runtime stage: Python + the virtualenv, no pip or build tools.
FROM python:3.13-alpine
RUN rm -rf /usr/local/lib/python3.13/site-packages/pip /usr/local/bin/pip* \
 && mkdir -p /data && chown 1000:1000 /data
COPY --from=build /venv /venv

ENV PATH=/venv/bin:$PATH \
    RIGOL_TRANSPORT=http \
    RIGOL_HTTP_PORT=8790 \
    RIGOL_DATA_DIR=/data \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Runs as a non-root user; compose.yaml sets it to your own UID/GID so files in ./data are yours.
USER 1000:1000
EXPOSE 8790
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8790/health', timeout=3)"
ENTRYPOINT ["rigol-mcp"]
