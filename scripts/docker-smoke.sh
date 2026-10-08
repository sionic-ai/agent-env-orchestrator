#!/usr/bin/env bash
# End-to-end smoke test of the CLI against a real Docker daemon:
# build the demo images, validate and run the demo environment, check the reward and
# that the run left no containers or volumes behind.
set -euo pipefail
cd "$(dirname "$0")/.."

AEO="${AEO:-aeo}"
RUNS_DIR="$(mktemp -d)"
trap 'rm -rf "$RUNS_DIR"' EXIT

./scripts/build-demo-images.sh
"$AEO" catalog
"$AEO" validate environments/cpu-sum-demo
"$AEO" run environments/cpu-sum-demo --runs-dir "$RUNS_DIR" > "$RUNS_DIR/stdout.json"

python3 - "$RUNS_DIR/stdout.json" <<'PY'
import json, sys
result = json.load(open(sys.argv[1]))
assert result["status"] == "completed", result
assert result["reward"] == 1.0, result
assert result["cleanup"]["ok"], result["cleanup"]
print(f"smoke: run {result['run_id']} completed with reward {result['reward']}")
open(sys.argv[1] + ".run_id", "w").write(result["run_id"])
PY

RUN_ID="$(cat "$RUNS_DIR/stdout.json.run_id")"
"$AEO" result "$RUN_ID" --runs-dir "$RUNS_DIR" > /dev/null
leftover="$(docker ps -aq --filter "label=aeo.run_id=$RUN_ID")$(docker volume ls -q --filter "label=aeo.run_id=$RUN_ID")"
if [ -n "$leftover" ]; then
  echo "smoke: leftover Docker objects for $RUN_ID: $leftover" >&2
  exit 1
fi
echo "smoke: OK (no leftover containers or volumes)"
