#!/usr/bin/env bash
# Smoke check for the root docker-compose.yml's security-critical override:
# `estimator.ports: !override []` is supposed to make `estimator` reachable
# ONLY on the internal Compose network when the stack is launched from the
# repo root (see docker-compose.yml). That property previously had zero
# automated verification -- only a YAML comment -- so a `!override` typo, a
# Compose merge-precedence change, or a Compose-version incompatibility
# could silently reopen the port without anything failing.
#
# This does a config-parse-time check only (no live containers required):
# it renders the merged compose config and asserts the `estimator` service
# has no `ports` key at all.
#
# Usage: ./scripts/check-estimator-not-exposed.sh   (run from anywhere)
set -euo pipefail
cd "$(dirname "$0")/.."

python3 - <<'PY'
import json
import subprocess
import sys

result = subprocess.run(
    ["docker", "compose", "config", "--format", "json"],
    capture_output=True,
    text=True,
)
if result.returncode != 0:
    sys.stderr.write(result.stderr)
    sys.exit(result.returncode)

config = json.loads(result.stdout)
ports = config["services"]["estimator"].get("ports")
if ports:
    sys.stderr.write(f"FAIL: estimator service has published ports: {ports!r}\n")
    sys.exit(1)

print("OK: estimator has no published ports in the merged root compose config")
PY
