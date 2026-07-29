"""Static regression checks for CI/CD and container hardening files."""

from pathlib import Path


def _read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def test_ci_supports_pull_requests_manual_runs_and_postgres():
    workflow = _read(".github/workflows/ci.yml")
    assert "pull_request:" in workflow
    assert "workflow_dispatch:" in workflow
    assert "postgres:16-alpine" in workflow
    assert "pytest -q" in workflow
    assert "pip-audit --strict" in workflow
    assert "bandit -r" in workflow
    assert "Container build and smoke test" in workflow
    assert "/_stcore/health" in workflow


def test_release_only_publishes_after_verification():
    workflow = _read(".github/workflows/release.yml")
    assert "needs: verify" in workflow
    assert "packages: write" in workflow
    assert "ghcr.io/artixcore/artixcore-contentpilot" in workflow
    assert "push: true" in workflow
    assert "attest-build-provenance" in workflow
    assert "sbom: true" in workflow


def test_codeql_is_enabled_for_pull_requests_and_manual_runs():
    workflow = _read(".github/workflows/codeql.yml")
    assert "pull_request:" in workflow
    assert "workflow_dispatch:" in workflow
    assert "security-events: write" in workflow
    assert "security-extended" in workflow


def test_container_runs_as_non_root_with_healthcheck():
    dockerfile = _read("Dockerfile")
    assert "USER 10001:10001" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "/_stcore/health" in dockerfile
    assert "tini" in dockerfile
    assert "PIP_NO_CACHE_DIR=1" in dockerfile


def test_docker_context_excludes_secrets_and_runtime_data():
    dockerignore = _read(".dockerignore")
    assert ".env" in dockerignore
    assert "data/" in dockerignore
    assert "logs/" in dockerignore
    assert ".git" in dockerignore
