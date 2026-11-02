# circle-mcp — streamable-HTTP container.
#   podman build --format docker -t localhost/circle-mcp:latest .
# (--format docker keeps the HEALTHCHECK; podman's default OCI format drops it.)
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    CIRCLE_MCP_TRANSPORT=streamable-http \
    CIRCLE_MCP_HOST=0.0.0.0 \
    CIRCLE_MCP_PORT=8080 \
    CIRCLE_SECRETS_FILE=/config/secrets.conf

RUN useradd --uid 10001 --create-home --home-dir /app --shell /usr/sbin/nologin circle
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src/ src/
RUN pip install . && rm -rf /src

USER circle
WORKDIR /app
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4)" || exit 1

# Fails closed: refuses to start unless CIRCLE_MCP_KEYS holds at least one key.
ENTRYPOINT ["circle-mcp"]
