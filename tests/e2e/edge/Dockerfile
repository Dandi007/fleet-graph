FROM python:3.12.11-slim-bookworm
COPY tests/e2e/edge/proxy.py /opt/edge/proxy.py
USER 65534:65534
ENTRYPOINT ["python", "-u", "/opt/edge/proxy.py"]
