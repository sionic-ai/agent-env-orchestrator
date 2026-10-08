# Demo FIXTURE agent image: deterministic script, not an AI model.
FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f
LABEL org.opencontainers.image.title="aeo-demo-agent" \
      org.opencontainers.image.description="Deterministic fixture agent for aeo demos (not AI)" \
      org.opencontainers.image.licenses="Apache-2.0"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY --chmod=0555 fixture_agent.py /opt/aeo-agent/fixture_agent.py
USER 10001:10001
WORKDIR /workspace
CMD ["python3", "/opt/aeo-agent/fixture_agent.py", "solve"]
