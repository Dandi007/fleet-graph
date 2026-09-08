ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
COPY tests/e2e/services/git_remote_entrypoint.sh /opt/e2e/git_remote_entrypoint.sh
EXPOSE 9418
HEALTHCHECK --interval=5s --timeout=5s --start-period=5s --retries=12 \
    CMD git ls-remote git://127.0.0.1:9418/work-folder.git >/dev/null
ENTRYPOINT ["sh", "/opt/e2e/git_remote_entrypoint.sh"]
