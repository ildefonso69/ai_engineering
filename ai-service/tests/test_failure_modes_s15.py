"""Session 15 — the Block 1 failure-mode fixtures, pinned.

`exercises/session-15/failure_modes/` holds five minimal reproductions of what
breaks when containerising the system. They are teaching artifacts, so they need
a guard of their own: a fixture that silently loses its defect teaches nothing,
and one that drifts from the real file teaches something false.

Each test asserts BOTH halves of the lesson:

    the broken fixture still contains the defect
    the real file in the repo does not

No Docker, no network, no YAML dependency beyond what ships with the suite.
"""

from __future__ import annotations

import re

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVICE_ROOT = REPO_ROOT / "ai-service"
FIXTURES = SERVICE_ROOT / "exercises" / "session-15" / "failure_modes"
REAL_COMPOSE = REPO_ROOT / "docker-compose.yml"


def load(name: str) -> str:
    path = FIXTURES / name
    assert path.exists(), f"missing failure-mode fixture: {path}"
    return path.read_text()


def load_yaml(name: str) -> dict:
    return yaml.safe_load(load(name))


@pytest.fixture(scope="module")
def real_compose() -> dict:
    assert REAL_COMPOSE.exists(), f"missing {REAL_COMPOSE}"
    return yaml.safe_load(REAL_COMPOSE.read_text())


def test_all_five_fixtures_are_present():
    """The Block 1 material is complete."""
    expected = {
        "01-image-does-not-build.Dockerfile",
        "02-wrong-boot-order.yml",
        "03-localhost-vs-service-name.yml",
        "04-ports-leak.yml",
        "05-token-mismatch.env",
        "README.md",
    }
    assert expected <= {p.name for p in FIXTURES.iterdir()}


# --- 1. the image does not build ------------------------------------------- #


def test_broken_dockerfile_copies_source_before_installing_dependencies():
    content = load("01-image-does-not-build.Dockerfile")
    copy_all = content.index("COPY . .")
    uv_sync = content.index("uv sync")
    assert copy_all < uv_sync, "the fixture must copy the source BEFORE installing"
    # The build-context defect: paths written as if the context were the repo
    # root, while compose builds with `build: ./ai-service`. Checked on the
    # INSTRUCTIONS, not the prose -- the header comment explains the trap and a
    # naive substring search would match its own documentation.
    copy_lines = [ln for ln in content.splitlines() if ln.startswith("COPY")]
    assert any("ai-service/" in ln for ln in copy_lines), (
        "the fixture must reference paths for the WRONG context -- that is the failure"
    )


def test_the_real_dockerfile_installs_dependencies_first():
    content = (SERVICE_ROOT / "Dockerfile").read_text()
    uv_sync = content.index("uv sync")
    # Match the INSTRUCTION, not one exact spelling of it. `COPY` takes flags
    # (`--chown`, `--chmod`, `--from`), and pinning the literal "COPY app/" made
    # a correct Dockerfile change fail a test that is about layer ORDER and
    # nothing else. The flag group is optional, and the source path must start
    # with `app/` -- otherwise this also matches `COPY --from=builder /app/.venv`.
    copy_app = re.search(r"^COPY\b(?:\s+--\S+)*\s+app/", content, re.MULTILINE)
    assert copy_app, "the real Dockerfile must copy app/ into the image"
    assert uv_sync < copy_app.start(), (
        "the real image must install dependencies before the source"
    )
    assert "uv.lock" in content
    # And its paths match the context compose actually gives it.
    copy_lines = [ln for ln in content.splitlines() if ln.startswith("COPY")]
    assert not any("ai-service/" in ln for ln in copy_lines), (
        "the real Dockerfile is built with context ./ai-service, so paths must be "
        "relative to it"
    )


def test_the_real_compose_builds_each_service_from_its_own_directory(real_compose):
    """The other half of the context lesson: the paths in each Dockerfile are
    only correct because compose hands it that directory as the context."""
    assert real_compose["services"]["ai-service"]["build"] == "./ai-service"
    assert real_compose["services"]["business-backend"]["build"] == "./business-backend"


# --- 2. boot order --------------------------------------------------------- #


def test_broken_compose_waits_for_creation_not_readiness():
    depends = load_yaml("02-wrong-boot-order.yml")["services"]["business-backend"]["depends_on"]
    # The list form is the defect: it carries no condition.
    assert isinstance(depends, list)


def test_the_real_compose_waits_for_health(real_compose):
    depends = real_compose["services"]["business-backend"]["depends_on"]
    assert isinstance(depends, dict)
    for service, spec in depends.items():
        assert spec["condition"] == "service_healthy", service
    # And every dependency it waits on actually defines a healthcheck, or the
    # condition could never be satisfied.
    for service in depends:
        assert "healthcheck" in real_compose["services"][service], service


# --- 3. localhost vs service name ------------------------------------------ #


def test_broken_compose_points_the_client_at_localhost():
    env = load_yaml("03-localhost-vs-service-name.yml")["services"]["business-backend"][
        "environment"
    ]
    assert "localhost" in env["ESTIMATOR_API_BASE_URL"]


def test_the_real_compose_uses_the_service_name(real_compose):
    url = real_compose["services"]["business-backend"]["environment"]["ESTIMATOR_API_BASE_URL"]
    assert url == "http://ai-service:8000"
    assert "localhost" not in url


# --- 4. the boundary ------------------------------------------------------- #


def test_broken_compose_publishes_the_ai_service_port():
    assert "ports" in load_yaml("04-ports-leak.yml")["services"]["ai-service"]


def test_the_real_compose_publishes_only_the_business_backend(real_compose):
    """The security property of the whole session, as an assertion.

    Not "the AI service has no ports" but "NOTHING except the business backend
    has ports" -- a leak on the vector database would be just as bad.
    """
    published = {
        name: spec["ports"]
        for name, spec in real_compose["services"].items()
        if spec.get("ports")
    }
    assert set(published) == {"business-backend"}, f"unexpectedly published: {published}"


# --- 5. the token ---------------------------------------------------------- #


def test_broken_env_has_two_different_token_values():
    lines = dict(
        line.split("=", 1)
        for line in load("05-token-mismatch.env").splitlines()
        if "=" in line and not line.startswith("#")
    )
    assert lines["AI_SERVICE_TOKEN"] != lines["BUSINESS_BACKEND_AI_SERVICE_TOKEN"]


def test_the_real_env_example_declares_one_shared_token():
    """Both services read the SAME variable from the SAME root .env -- which is
    what makes a mismatch impossible in the real setup."""
    content = (REPO_ROOT / ".env.example").read_text()
    assert content.count("\nAI_SERVICE_TOKEN=") == 1
