ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends socat \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir 'uv==0.8.22'
COPY .runtime/e2e/build/agent-bus /opt/src/agent-bus
WORKDIR /opt/src/agent-bus
RUN uv sync --frozen --no-dev --no-editable
ENV PATH=/opt/src/agent-bus/.venv/bin:$PATH PYTHONUNBUFFERED=1 \
    AGENT_BUS_CONFIG=/opt/e2e/agent-bus.yaml
COPY tests/e2e/services/agent-bus.yaml /opt/e2e/agent-bus.yaml
COPY tests/e2e/services/agent_bus_entrypoint.py /opt/e2e/agent_bus_entrypoint.py
COPY tests/e2e/services/probe.py /opt/e2e/probe.py
EXPOSE 7470
HEALTHCHECK --interval=5s --timeout=5s --start-period=15s --retries=12 \
    CMD ["python", "/opt/e2e/probe.py", "bus-health"]
ENTRYPOINT ["python", "/opt/e2e/agent_bus_entrypoint.py"]
