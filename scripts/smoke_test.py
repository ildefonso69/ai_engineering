#!/usr/bin/env python3
"""Post-deploy smoke test for the three-layer system (Session 15).

**This is not part of CI.** It runs AFTER a deploy, against a real environment,
and it deliberately spends a few tokens: its whole job is to prove that the
three layers plus their two datastores actually talk to each other *in the place
where users will use them*. CI answers "does the code work"; this answers "is the
deployment alive".

Standard library only, so it runs anywhere: a laptop, a CI runner, or a shell
inside the platform.

    # Against the local docker compose stack
    python scripts/smoke_test.py --base-url http://localhost:3000

    # Against a deployed environment (public surface only)
    python scripts/smoke_test.py --base-url https://estimator.example.com

    # From inside the private network, adding the deep AI-service checks
    python scripts/smoke_test.py \
        --base-url https://estimator.example.com \
        --ai-url http://ai-service:8000

What it checks, and why each one earns its place:

  1. The business backend answers on its public URL.               (layer 2 up)
  2. Its health endpoint is green.                                 (layer 2 ready)
  3. The rendered page carries the active-model badge, which the layout can only
     print after a successful call to the AI service.        (layer 2 -> layer 3)
  4. Neither the AI service (8000) nor Rails (3000) is reachable from the public
     internet -- only the reverse proxy is.                          (the boundary)
  4b. HTTP redirects to HTTPS and the certificate is valid.                 (TLS)
  5. [--ai-url] Liveness answers without a token.                 (probe exempt)
  6. [--ai-url] Readiness is green, so the vector database and Redis are both
     reachable.                                            (layer 3 -> datastores)
  7. [--ai-url] An unauthenticated call is rejected with 401.          (the door)
  8. [--ai-url] A real estimation returns a well-formed result.  (the whole path)

Exit code 0 if every check passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# The badge the Rails layout renders only when the AI service answered.
MODEL_BADGE_MARKER = "Modelo primario activo del servicio IA"

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


class Results:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.skipped = 0

    def ok(self, name: str, detail: str = "") -> None:
        self.passed += 1
        print(f"  {GREEN}PASS{RESET}  {name}" + (f"  {DIM}{detail}{RESET}" if detail else ""))

    def fail(self, name: str, detail: str = "") -> None:
        self.failed += 1
        print(f"  {RED}FAIL{RESET}  {name}" + (f"  {DIM}{detail}{RESET}" if detail else ""))

    def skip(self, name: str, why: str) -> None:
        self.skipped += 1
        print(f"  {YELLOW}SKIP{RESET}  {name}  {DIM}{why}{RESET}")


def request(
    url: str,
    *,
    method: str = "GET",
    headers: dict | None = None,
    body: dict | None = None,
    timeout: float = 30.0,
) -> tuple[int, str]:
    """Return (status, body). An HTTP error status is a RESULT, not an exception —
    checks here assert on 401 and 503 as much as on 200."""
    data = json.dumps(body).encode() if body is not None else None
    hdrs = dict(headers or {})
    if data is not None:
        hdrs.setdefault("Content-Type", "application/json")

    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


# --- public surface -------------------------------------------------------- #


def check_business_backend(base_url: str, results: Results, timeout: float) -> None:
    print(f"\n{DIM}Business backend (public){RESET}")

    try:
        status, html = request(base_url, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        results.fail("business backend responds", f"{type(exc).__name__}: {exc}")
        return

    if status == 200:
        results.ok("business backend responds", f"GET / -> 200")
    else:
        results.fail("business backend responds", f"GET / -> {status}")

    try:
        status, _ = request(urllib.parse.urljoin(base_url + "/", "up"), timeout=timeout)
        if status == 200:
            results.ok("business backend health", "GET /up -> 200")
        else:
            results.fail("business backend health", f"GET /up -> {status}")
    except Exception as exc:  # noqa: BLE001
        results.fail("business backend health", f"{type(exc).__name__}: {exc}")

    # The layout prints the active model only after a successful AI-service call,
    # and swallows the error when it fails (a dead AI service must never block
    # page rendering). So the badge's presence IS the layer-2 -> layer-3 probe.
    if MODEL_BADGE_MARKER in html:
        results.ok("business backend reaches the AI service", "active-model badge rendered")
    else:
        results.fail(
            "business backend reaches the AI service",
            "active-model badge missing — check ESTIMATOR_API_BASE_URL and AI_SERVICE_TOKEN",
        )


# Ports that must never answer from the internet. On a single VM every service
# shares one public address, so a stray `ports:` entry in the production
# override is directly reachable -- which makes this check far more meaningful
# here than on a PaaS, where each service had its own hostname.
PRIVATE_PORTS = {
    8000: "AI service",
    3000: "Rails (must be reached only through the proxy)",
    5432: "PostgreSQL",
    6379: "Redis",
}


def check_private_ports(public_host: str, results: Results, entry_port: int | None) -> None:
    """No internal port may answer from outside.

    Only meaningful from outside the private network; when the runner IS inside
    it (--ai-url given), the checks are skipped rather than reported as passing.

    Two exclusions keep this honest rather than merely strict:

    * ``entry_port`` — the port the base URL itself uses is open BY DEFINITION.
      Against the local compose stack that is Rails on 3000; only in production,
      behind the proxy, does 3000 become private.
    * On localhost, the database ports are ambiguous: a developer machine
      usually runs its own Postgres and Redis, and failing the run because of
      them would teach the wrong lesson. They are reported as skipped.
    """
    print(f"\n{DIM}Security boundary{RESET}")

    # A hostname that does not resolve makes EVERY port look closed, which would
    # turn a typo into a full-marks security report. Resolve once, up front.
    try:
        socket.getaddrinfo(public_host, None)
    except socket.gaierror as exc:
        results.fail(
            f"resolve {public_host}",
            f"the hostname does not resolve ({exc.strerror or exc}) — "
            "every port check below would be meaningless",
        )
        return

    is_local = public_host in {"localhost", "127.0.0.1", "::1"}

    for port, label in sorted(PRIVATE_PORTS.items()):
        if port == entry_port:
            results.skip(
                f"{public_host}:{port} is closed ({label})",
                "this is the entry point of the URL under test",
            )
            continue
        if is_local and port in (5432, 6379):
            results.skip(
                f"{public_host}:{port} is closed ({label})",
                "ambiguous on a developer machine (your own service may answer)",
            )
            continue
        name = f"{public_host}:{port} is closed ({label})"
        try:
            with socket.create_connection((public_host, port), timeout=3):
                results.fail(name, "the port ACCEPTED a connection — the boundary is OPEN")
        except (socket.timeout, TimeoutError):
            results.ok(name, "no answer (dropped by the firewall)")
        except (ConnectionRefusedError, OSError):
            results.ok(name, "connection refused, as it should be")


def check_tls(base_url: str, results: Results, timeout: float) -> None:
    """HTTP must redirect to HTTPS, and the certificate must be valid.

    Skipped for a plain-HTTP base URL (the local compose stack), where there is
    no proxy and nothing to assert.
    """
    if not base_url.startswith("https://"):
        results.skip("HTTP redirects to HTTPS", "--base-url is not https (local stack)")
        return

    host = urllib.parse.urlparse(base_url).hostname or ""

    # Do NOT follow the redirect: we want to see the 301/308 itself.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):  # noqa: D102
            return None

    opener = urllib.request.build_opener(NoRedirect)
    try:
        opener.open(f"http://{host}/", timeout=timeout)
        results.fail("HTTP redirects to HTTPS", "plain HTTP answered 200 instead of redirecting")
    except urllib.error.HTTPError as exc:
        if exc.code in (301, 302, 307, 308):
            results.ok("HTTP redirects to HTTPS", f"-> {exc.code}")
        else:
            results.fail("HTTP redirects to HTTPS", f"-> {exc.code}")
    except Exception as exc:  # noqa: BLE001
        results.fail("HTTP redirects to HTTPS", f"{type(exc).__name__}: {exc}")

    # A default SSL context verifies the chain and the hostname, so a successful
    # handshake IS the assertion. An expired or self-signed cert raises here.
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                cert = tls.getpeercert()
        issuer = dict(x[0] for x in cert.get("issuer", ())).get("organizationName", "?")
        results.ok("TLS certificate is valid", f"issued by {issuer}, until {cert.get('notAfter')}")
    except Exception as exc:  # noqa: BLE001
        results.fail("TLS certificate is valid", f"{type(exc).__name__}: {exc}")


# --- private surface (only from inside the network) ------------------------ #


def check_ai_service(ai_url: str, token: str | None, results: Results, timeout: float) -> None:
    print(f"\n{DIM}AI service (private network){RESET}")
    ai_url = ai_url.rstrip("/")

    try:
        status, body = request(f"{ai_url}/health", timeout=timeout)
        if status == 200:
            results.ok("liveness answers without a token", f"/health -> 200 {body[:60]}")
        else:
            results.fail("liveness answers without a token", f"/health -> {status}")
    except Exception as exc:  # noqa: BLE001
        results.fail("liveness answers without a token", f"{type(exc).__name__}: {exc}")
        return

    try:
        status, body = request(f"{ai_url}/health/ready", timeout=timeout)
        if status == 200:
            results.ok("readiness: vector DB and Redis reachable", "/health/ready -> 200")
        else:
            results.fail("readiness: vector DB and Redis reachable", f"-> {status} {body[:160]}")
    except Exception as exc:  # noqa: BLE001
        results.fail("readiness: vector DB and Redis reachable", f"{type(exc).__name__}: {exc}")

    # The door: without the token nothing but the probes may answer.
    if token:
        status, _ = request(f"{ai_url}/api/v1/estimate", method="POST", body={}, timeout=timeout)
        if status == 401:
            results.ok("unauthenticated calls are rejected", "-> 401")
        else:
            results.fail(
                "unauthenticated calls are rejected",
                f"-> {status} (expected 401; is AI_SERVICE_TOKEN set on the service?)",
            )
    else:
        results.skip("unauthenticated calls are rejected", "no --service-token given")


def check_estimation(ai_url: str, token: str | None, results: Results, timeout: float) -> None:
    """The end-to-end one: AI service -> vector DB -> LLM -> validated schema.

    This is the check that spends tokens, and the reason this script is not part
    of CI.
    """
    print(f"\n{DIM}End-to-end estimation (spends tokens){RESET}")
    headers = {"X-Service-Token": token} if token else {}
    payload = {
        "description": (
            "Internal web application for managing customer support tickets, with "
            "user authentication, a searchable ticket list, email notifications and "
            "a basic reporting dashboard."
        ),
        # Must be one of the ProjectType enum values the schema accepts:
        # mobile_app | web_saas | internal_tool | data_pipeline.
        "project_type": "internal_tool",
        "detail_level": "medium",
        "output_format": "phases_table",
    }

    started = time.monotonic()
    try:
        status, body = request(
            f"{ai_url.rstrip('/')}/api/v1/estimate",
            method="POST",
            headers=headers,
            body=payload,
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        results.fail("estimation completes", f"{type(exc).__name__}: {exc}")
        return

    elapsed = time.monotonic() - started
    if status != 200:
        results.fail("estimation completes", f"-> {status} {body[:200]}")
        return

    try:
        parsed = json.loads(body)
        result = parsed["result"]
        phases = result["phases"]
        total = result["total_cost_eur"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        results.fail("estimation has the expected shape", f"{type(exc).__name__}: {exc}")
        return

    results.ok("estimation completes", f"{elapsed:.1f}s, cached={parsed.get('cached')}")

    if phases and total and total > 0:
        results.ok(
            "estimation has the expected shape",
            f"{len(phases)} phases, total={total} EUR",
        )
    else:
        results.fail("estimation has the expected shape", f"phases={len(phases)} total={total}")

    # The schema validator guarantees this, so a mismatch means the response did
    # not come from the validated path at all.
    phase_sum = sum(p.get("cost_eur", 0) for p in phases)
    if abs(phase_sum - total) < 1:
        results.ok("phase costs sum to the total", f"{phase_sum} == {total}")
    else:
        results.fail("phase costs sum to the total", f"{phase_sum} != {total}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--base-url", required=True, help="Public URL of the business backend")
    parser.add_argument(
        "--ai-url",
        default=None,
        help="AI service base URL. Only reachable from inside the private network; "
        "enables the deep checks and the end-to-end estimation.",
    )
    parser.add_argument(
        "--service-token",
        default=os.environ.get("AI_SERVICE_TOKEN"),
        help="Shared service token (defaults to $AI_SERVICE_TOKEN)",
    )
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument(
        "--skip-estimation",
        action="store_true",
        help="Run every check except the one that spends tokens",
    )
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    print(f"Smoke test — {base_url}")

    results = Results()
    check_business_backend(base_url, results, args.timeout)

    # Probe the AI service on the business backend's own host: if the platform
    # leaked its port, this is where it shows.
    parsed_base = urllib.parse.urlparse(base_url)
    check_tls(base_url, results, args.timeout)

    if args.ai_url:
        print(f"\n{DIM}Security boundary{RESET}")
        results.skip(
            "internal ports are closed",
            "running from inside the private network (--ai-url given)",
        )
    else:
        check_private_ports(parsed_base.hostname or "localhost", results, parsed_base.port)

    if args.ai_url:
        check_ai_service(args.ai_url, args.service_token, results, args.timeout)
        if args.skip_estimation:
            results.skip("end-to-end estimation", "--skip-estimation")
        else:
            check_estimation(args.ai_url, args.service_token, results, args.timeout)
    else:
        print(f"\n{DIM}AI service (private network){RESET}")
        results.skip("AI service deep checks", "no --ai-url given (run from inside the network)")

    print(
        f"\n{results.passed} passed, {results.failed} failed, {results.skipped} skipped"
    )
    if results.failed:
        print(f"{RED}SMOKE TEST FAILED{RESET}")
        return 1
    print(f"{GREEN}SMOKE TEST PASSED{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
