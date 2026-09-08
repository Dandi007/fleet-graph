FROM python:3.11.13-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends git make ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir fastmcp==3.4.7 jsonschema==4.26.0
COPY tests/e2e/contract /harness/contract
COPY tests/e2e/runner /harness/runner
COPY tests/e2e/services/probe.py /harness/services/probe.py
COPY .runtime/e2e/build/source-manifest.json /harness/source-manifest.json
ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /harness
ENTRYPOINT ["python", "/harness/runner/run.py"]
