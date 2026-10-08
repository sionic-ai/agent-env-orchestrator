#!/usr/bin/env bash
# Build the two local demo images used by environments/cpu-sum-demo.
set -euo pipefail
cd "$(dirname "$0")/.."
docker build --quiet -t aeo-demo-agent:0.1.0 images/demo-agent
docker build --quiet -t aeo-demo-verifier:0.1.0 images/demo-verifier
