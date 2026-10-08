# Trusted demo verifier image. Holds the expected answer; never given to the agent.
FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f
LABEL org.opencontainers.image.title="aeo-demo-verifier" \
      org.opencontainers.image.description="Trusted verifier for the aeo cpu-sum-demo environment" \
      org.opencontainers.image.licenses="Apache-2.0"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# verify.py first: BuildKit applies --chmod to the parent directory it creates.
COPY --chmod=0555 verify.py /opt/aeo-verifier/verify.py
COPY --chmod=0444 expected.json /opt/aeo-verifier/expected.json
USER 10001:10001
WORKDIR /workspace
CMD ["python3", "/opt/aeo-verifier/verify.py"]
