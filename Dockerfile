FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir --root-user-action=ignore . \
 && mkdir -p /data && chown 1000:1000 /data

ENV RIGOL_TRANSPORT=http \
    RIGOL_HTTP_PORT=8790 \
    RIGOL_DATA_DIR=/data \
    PYTHONUNBUFFERED=1

# Runs as a non-root user; compose.yaml sets it to your own UID/GID so files in ./data are yours.
USER 1000:1000
EXPOSE 8790
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8790/health', timeout=3)"
ENTRYPOINT ["rigol-mcp"]
