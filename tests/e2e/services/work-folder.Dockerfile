ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
COPY .runtime/e2e/build/katana/mcp/shared /opt/src/shared
COPY .runtime/e2e/build/katana/mcp/kernel /opt/src/kernel
COPY .runtime/e2e/build/katana/mcp/work-folder /opt/src/work-folder
RUN pip install --no-cache-dir 'fastmcp==3.2.4' /opt/src/shared /opt/src/kernel /opt/src/work-folder
COPY .runtime/e2e/build/agent-knowledge /opt/src/agent-knowledge
# canonical 服务原生支持 keyword fallback；不安装向量依赖，不联系外部 embedding。
RUN pip install --no-cache-dir fastapi uvicorn 'watchdog>=3' \
    && pip install --no-cache-dir --no-deps /opt/src/agent-knowledge
COPY tests/e2e/services/work_folder_entrypoint.py /opt/e2e/work_folder_entrypoint.py
COPY tests/e2e/services/work_folder_search.py /opt/e2e/work_folder_search.py
COPY tests/e2e/services/probe.py /opt/e2e/probe.py
ENV PYTHONUNBUFFERED=1 E2E_WORK_FOLDER_ROOT=/data/work-folder VAULT_SEARCH_DISABLE_VECTOR=1
EXPOSE 5602
HEALTHCHECK --interval=5s --timeout=10s --start-period=30s --retries=12 \
    CMD ["python", "/opt/e2e/probe.py", "work-folder-health"]
ENTRYPOINT ["python", "/opt/e2e/work_folder_entrypoint.py"]
