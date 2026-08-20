#!/usr/bin/env python
"""Session 15 — cross-layer contract check.

Asserts that every route the business backend calls actually exists in the AI
service's OpenAPI document, with the right verb.

Why this is a *contract* test and not just another unit test: the two layers are
deployed independently. Nothing stops someone renaming an endpoint in the AI
service, keeping its own suite green, and only discovering at runtime that the
Rails client now 404s. This closes that gap in CI, cheaply and without network
-- it imports the FastAPI app and reads the schema it generates from the very
same Pydantic models that serve the traffic.

It also verifies that the two health probes stay unauthenticated: if `/health`
ever fell behind the service token, every container would report unhealthy and
compose would refuse to start the stack.

Usage:
    uv run python scripts/check_contract.py
    uv run python scripts/check_contract.py --contract ../docs/contract/....json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]   # ai-service/
REPO_ROOT = SERVICE_ROOT.parent                      # monorepo root
DEFAULT_CONTRACT = REPO_ROOT / "docs" / "contract" / "business-backend-consumed-routes.json"

# Run as `python scripts/check_contract.py` from ai-service/, so `app` is only
# importable once its parent is on the path (same trick as run_graph_s13.py).
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))


def load_openapi() -> dict:
    """Import the app and render its OpenAPI schema. No server, no network."""
    from app.main import app

    return app.openapi()


def check(contract_path: Path) -> int:
    contract = json.loads(contract_path.read_text())
    schema = load_openapi()
    paths = schema.get("paths", {})

    failures: list[str] = []
    checked = 0

    for route in contract["routes"]:
        path, method, client = route["path"], route["method"].lower(), route["client"]
        checked += 1
        if path not in paths:
            failures.append(f"{client}: {method.upper()} {path} -- path absent from OpenAPI")
        elif method not in paths[path]:
            available = ", ".join(sorted(paths[path])).upper() or "none"
            failures.append(
                f"{client}: {method.upper()} {path} -- path exists but not that verb "
                f"(available: {available})"
            )

    # The probes must exist AND stay exempt from the service token, otherwise
    # the container healthcheck can never pass.
    from app.api.service_token import EXEMPT_PATHS

    for probe in contract["probes"]:
        path, method = probe["path"], probe["method"].lower()
        checked += 1
        if path not in paths or method not in paths.get(path, {}):
            failures.append(f"probe: {method.upper()} {path} -- absent from OpenAPI")
        elif probe.get("unauthenticated") and path not in EXEMPT_PATHS:
            failures.append(
                f"probe: {path} is no longer exempt from the service token -- "
                "the Docker healthcheck cannot carry a secret"
            )

    if failures:
        print(f"CONTRACT BROKEN -- {len(failures)} of {checked} checks failed:\n")
        for f in failures:
            print(f"  x {f}")
        print(
            "\nOne of the two layers moved without the other. Either restore the "
            "endpoint in ai-service, or update the Ruby client AND "
            f"{contract_path.relative_to(REPO_ROOT)} together."
        )
        return 1

    print(f"Contract OK -- {checked} checks passed ({len(contract['routes'])} consumed routes).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    args = parser.parse_args()

    if not args.contract.exists():
        print(f"Contract file not found: {args.contract}", file=sys.stderr)
        return 2
    return check(args.contract)


if __name__ == "__main__":
    raise SystemExit(main())
