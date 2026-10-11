# olah-cache-manager builds directly on the olah image so it can reuse olah's own
# cache-eviction primitives (olah.utils.disk_utils). Pinned to the same digest the
# olah mirror deployment runs; Renovate keeps it current.
# renovate: datasource=docker depName=xiahan2019/olah
FROM docker.io/xiahan2019/olah:0.5.1@sha256:da77e6de1aa50bf5ec2e2c2d66a36f35b33645ce4ed6d802179882dd64803bec

LABEL org.opencontainers.image.title="olah-cache-manager" \
      org.opencontainers.image.description="Web UI to list and delete olah HuggingFace mirror cache entries" \
      org.opencontainers.image.source="https://github.com/niki-on-github/olah-cache-manager" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MANAGER_REPOS_PATH=/data/repos \
    MANAGER_DELETE_ENABLED=false

WORKDIR /app/manager
COPY app.py ./app.py
COPY templates ./templates

EXPOSE 8091
# The olah base runs as root; the manager only needs to read/write the cache tree,
# which the pod's fix-permissions initContainer chowns to 1000:1000.
USER 1000:1000

# The olah base sets ENTRYPOINT ["olah-cli"]; replace it so we run our own app.
ENTRYPOINT ["python", "-m", "uvicorn"]
CMD ["app:app", "--host", "0.0.0.0", "--port", "8091"]
